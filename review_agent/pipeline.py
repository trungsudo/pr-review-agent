"""The pipeline: deterministic parse/rules -> AI candidates -> deterministic gate.

Where the trust boundary sits (the judgement this repo is built around):

- Rule findings are grounded by construction (they quote a real changed
  line) and high-precision by curation, so they may gate a review.
- AI-stage candidates are suggestions, not authority. They are validated,
  grounded, de-duplicated against rules, capped at "major", and count-limited.
  The CLI uses a deterministic stub for this stage; the same gate would apply
  if a real LLM were connected later.

Every decision that changed the finding list is recorded in ``stats``,
so a review can always answer "what did the bot throw away, and why".
"""

from __future__ import annotations

from dataclasses import dataclass, field

from review_agent.backends import LLMBackend, LLMBackendError, StubLLMBackend
from review_agent.diffparse import DiffParseError, DiffParser, FileDiff
from review_agent.findings import Finding, ReviewResult, SEVERITIES
from review_agent.rules import run_rules

MAX_MODEL_FINDINGS = 5
# Model severity is uncalibrated, so keep its findings advisory. Without this
# cap, a candidate could be labeled "blocker" and look authoritative in the
# report, even though only deterministic rule blockers can fail the CI gate.
MAX_MODEL_SEVERITY = "major"
DEDUP_LINE_WINDOW = 3  # AI candidate within N lines of a rule finding


class PipelineError(Exception):
    """Unrecoverable diff-input problem, separate from an AI-stage failure."""


@dataclass
class PipelineConfig:
    # Keep the AI-driven stage offline and repeatable by default.
    llm_backend: LLMBackend = field(default_factory=StubLLMBackend)
    max_model_findings: int = MAX_MODEL_FINDINGS
    dedup_window: int = DEDUP_LINE_WINDOW
    fail_on_llm_error: bool = False  # False -> degrade to rules-only


class ReviewPipeline:
    def __init__(self, config: PipelineConfig | None = None):
        self.config = config or PipelineConfig()

    # ------------------------------------------------------------------ run

    def run(self, diff_text: str) -> ReviewResult:
        # ------------------------------------------------------ parse diff
        # Parse once and use this structure as the source of truth for line
        # locations. If parsing fails, later findings could not be grounded
        # reliably, so treat malformed input as an input error.
        try:
            files = DiffParser().parse(diff_text)
        except DiffParseError as exc:
            raise PipelineError(f"cannot parse diff: {exc}") from exc

        # ------------------------------------------------- deterministic rules
        # These curated checks run deterministically and remain useful even
        # when AI candidate generation raises an error.
        rule_findings = run_rules(files)

        # -------------------------------------------------- AI candidates
        # This is the AI-driven stage. The exercise uses a deterministic stub
        # here so the trust policy can be evaluated without a live model.
        raw_model: list[Finding] = []
        llm_error: str | None = None
        try:
            # Sharing rule findings may reduce duplicates, but can introduce
            # anchoring bias and hide independent concerns. Output stays
            # untrusted and is deduplicated; the stub ignores this context.
            raw_model = self.config.llm_backend.generate(
                diff_text, self._model_context(files, rule_findings)
            )
        except LLMBackendError as exc:
            # Degrade to the deterministic result instead of losing the
            # whole review. Keep the error visible so callers can distinguish
            # "no AI-stage candidates" from "candidate generation failed".
            llm_error = str(exc)
            if self.config.fail_on_llm_error:
                raise

        # --------------------------------------------------- trust gate
        # Treat generated candidates as untrusted data, even though the
        # current source is a local stub. This keeps the gate identical for
        # the mock catalog and any future live LLM output.
        model_findings, dropped = self._ground_and_dedup(
            raw_model, files, rule_findings
        )

        # --------------------------------------------------- build result
        result = ReviewResult()
        result.findings = rule_findings + model_findings
        result.unanchored = [f for f in model_findings if f.line is None]
        result.skipped = [
            f"{f.path} ({f.skipped_reason})"
            for f in files
            if f.skipped_reason
        ]
        # Preserve the gate's accounting. A short final list can mean either
        # "few issues found" or "many candidates were rejected", which are
        # different outcomes for anyone evaluating the reviewer.
        result.stats = {
            "llm_backend": self.config.llm_backend.name,
            "llm_error": llm_error,
            "files": len(files),
            "rule_findings": len(rule_findings),
            "model_raw": len(raw_model),
            "model_invalid": dropped["invalid"],
            "model_ungrounded": dropped["ungrounded"],
            "model_deduped": dropped["deduped"],
            "model_capped": dropped["capped"],
            "model_kept": len(model_findings),
            "model_unanchored": len(result.unanchored),
        }
        return result

    # ---------------------------------------------------------- grounding

    def _ground_and_dedup(
        self,
        raw: list[Finding],
        files: list[FileDiff],
        rule_findings: list[Finding],
    ) -> tuple[list[Finding], dict]:
        by_path = {f.path: f for f in files}
        dropped = {
            "invalid": 0,
            "ungrounded": 0,
            "deduped": 0,
            "capped": 0,
        }
        candidates: list[Finding] = []

        for cand in raw:
            try:
                # Normalize bounded metadata, but discard malformed records
                # rather than allowing one bad candidate to abort the review.
                cand.validate()
            except (TypeError, ValueError):
                dropped["invalid"] += 1
                continue

            filediff = by_path.get(cand.file)
            if filediff is None:
                # Hallucinated file: the one grounding check that can never
                # be waved through, because nothing about it is verifiable.
                dropped["ungrounded"] += 1
                continue

            if cand.line is not None and not self._in_hunks(filediff, cand.line):
                # Keep a potentially useful observation, but remove a
                # location we cannot verify. A false clickable anchor is
                # more misleading than an explicit file-wide comment.
                cand.line = None

            if SEVERITIES.index(cand.severity) > SEVERITIES.index(
                MAX_MODEL_SEVERITY
            ):
                # Severity is merge authority. A live LLM's confidence would
                # be self-reported, so AI-stage output may advise but cannot
                # create a blocker (the stub exercises the same limit).
                cand.severity = MAX_MODEL_SEVERITY

            if self._duplicates_rule(cand, rule_findings):
                dropped["deduped"] += 1
                continue

            candidates.append(cand)

        # Prefer higher-confidence suggestions, then severity, and cap the
        # list so speculative output cannot overwhelm the human reviewer.
        candidates.sort(
            key=lambda f: (-f.confidence, -SEVERITIES.index(f.severity))
        )
        kept = candidates[: self.config.max_model_findings]
        dropped["capped"] = len(candidates) - len(kept)
        return kept, dropped

    def _duplicates_rule(self, cand: Finding, rule_findings: list[Finding]) -> bool:
        """An unanchored candidate still duplicates a rule finding when it
        targets the same file and defect category: presenting both would
        show the same issue twice with different levels of trust."""
        return any(
            r.file == cand.file
            and r.category == cand.category
            and (
                r.line is None
                or cand.line is None
                or abs(r.line - cand.line) <= self.config.dedup_window
            )
            for r in rule_findings
        )

    # ------------------------------------------------------------ helpers

    def _in_hunks(self, filediff: FileDiff, line: int) -> bool:
        for hunk in filediff.hunks:
            if not any(c.new_lineno for c in hunk.changes):
                # Deletion-only hunk: there is no new-side line to anchor
                # to, so no line number can be grounded here.
                continue
            if hunk.new_start <= line <= self._hunk_end(hunk):
                return True
        return False

    @staticmethod
    def _hunk_end(hunk) -> int:
        new_lines = [c.new_lineno for c in hunk.changes if c.new_lineno]
        return max(new_lines) if new_lines else hunk.new_start

    @staticmethod
    def _model_context(files: list[FileDiff], rules: list[Finding]) -> dict:
        """Build context for an LLM; the current deterministic stub ignores it."""
        return {
            "changed_files": [f.path for f in files],
            "rule_findings": [f.to_dict() for f in rules],
        }
