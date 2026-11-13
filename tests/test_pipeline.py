"""Pipeline tests: grounding, dedup, severity caps, degradation, verdict.

The pipeline's job is to be a trustworthy middle layer: these tests pin
the trust boundaries that filter AI-stage candidates before reporting.
"""

from __future__ import annotations

import pytest

from review_agent.backends import LLMBackendError, StubLLMBackend
from review_agent.findings import Finding, ReviewResult, SEVERITIES
from review_agent.pipeline import PipelineConfig, PipelineError, ReviewPipeline
from review_agent.report import render_verdict


class ExplodingLLMBackend(StubLLMBackend):
    """LLM stage that always fails — exercises the degradation path."""

    def generate(self, diff_text, context):
        raise LLMBackendError("simulated outage")


def test_rules_fire_on_sample(sample_diff):
    result = ReviewPipeline().run(sample_diff)
    categories = {f.category for f in result.findings if f.source == "rule"}
    assert "error-handling" in categories
    assert "tests" in categories  # the no-tests comment


def test_stub_llm_catalog_trips_every_guard(sample_diff):
    """The stub is designed adversarially: every guard gets exercised."""
    result = ReviewPipeline().run(sample_diff)
    stats = result.stats
    assert stats["model_ungrounded"] == 1  # hallucinated file dropped
    assert stats["model_deduped"] == 1  # duplicate of the bare-except rule
    assert stats["model_kept"] == 3
    assert stats["model_capped"] == 0  # catalog fits under the cap
    assert stats["llm_error"] is None


def test_unknown_severity_collapses_to_minor(sample_diff):
    result = ReviewPipeline().run(sample_diff)
    # The stub's "critical" tax finding must land as a minor advisory.
    tax = [f for f in result.findings if "Tax applied" in f.message]
    assert len(tax) == 1
    assert tax[0].severity == "minor"
    assert all(f.severity in SEVERITIES for f in result.findings)


def test_model_cannot_mint_blocker(sample_diff):
    result = ReviewPipeline().run(sample_diff)
    model_blockers = [
        f
        for f in result.findings
        if f.source == "model" and f.severity == "blocker"
    ]
    assert model_blockers == []


def test_out_of_range_line_is_unanchored_not_misanchored(sample_diff):
    result = ReviewPipeline().run(sample_diff)
    assert len(result.unanchored) == 1
    # The unanchored observation is published WITHOUT a line number —
    # never re-anchored to a plausible-looking wrong line.
    assert result.unanchored[0].line is None


def test_model_duplicate_of_rule_is_dropped(sample_diff):
    result = ReviewPipeline().run(sample_diff)
    # The stub ships a same-defect candidate (line 33 vs rule line 35, same
    # category): only the rule's phrasing survives for the bare-except issue.
    msgs = [f.message for f in result.findings]
    assert sum("swallows" in m for m in msgs) == 1
    assert sum("silently vanishes" in m for m in msgs) == 0
    assert result.stats["model_deduped"] == 1


def test_unanchored_candidate_still_dedups_against_rule(sample_diff):
    """File-wide candidates cannot slip past dedup by dropping their line."""

    class UnanchoredDupBackend(StubLLMBackend):
        def generate(self, diff_text, context):
            return [
                Finding(
                    file="billing/invoice.py",
                    line=None,
                    severity="major",
                    category="error-handling",
                    message="The swallowed exception on discounts hides bugs.",
                    confidence=0.9,
                )
            ]

    result = ReviewPipeline(
        PipelineConfig(llm_backend=UnanchoredDupBackend())
    ).run(sample_diff)
    assert result.stats["model_deduped"] == 1
    assert all(
        "swallowed exception on discounts" not in f.message
        for f in result.findings
    )


def test_findings_cap_is_counted_not_silently_dropped(sample_diff):
    class ManyBackend(StubLLMBackend):
        def generate(self, diff_text, context):
            return [
                Finding(
                    file="billing/invoice.py",
                    line=30,
                    severity="minor",
                    category="style",
                    message=f"observation number {i}",
                    confidence=0.5,
                )
                for i in range(9)
            ]

    result = ReviewPipeline(PipelineConfig(llm_backend=ManyBackend())).run(
        sample_diff
    )
    stats = result.stats
    # Nine candidates = five kept (the configured cap) + capped remainder;
    # every candidate is accounted for in exactly one bucket.
    kept = stats["model_kept"]
    assert kept == 5
    assert stats["model_capped"] == stats["model_raw"] - kept


def test_none_message_is_counted_invalid_not_crash(sample_diff):
    class NoneMessageBackend(StubLLMBackend):
        def generate(self, diff_text, context):
            return [
                Finding(
                    file="billing/invoice.py",
                    line=30,
                    severity="minor",
                    category="general",
                    message=None,
                )
            ]

    result = ReviewPipeline(
        PipelineConfig(llm_backend=NoneMessageBackend())
    ).run(sample_diff)
    assert result.stats["model_invalid"] == 1
    assert all(f.message for f in result.findings)


def test_non_string_severity_is_counted_invalid(sample_diff):
    class BadSeverityBackend(StubLLMBackend):
        def generate(self, diff_text, context):
            return [
                Finding(
                    file="billing/invoice.py",
                    line=30,
                    severity=7,
                    category="general",
                    message="numeric severity word",
                )
            ]

    result = ReviewPipeline(
        PipelineConfig(llm_backend=BadSeverityBackend())
    ).run(sample_diff)
    assert result.stats["model_invalid"] == 1


def test_llm_failure_degrades_to_rules_only(sample_diff):
    config = PipelineConfig(llm_backend=ExplodingLLMBackend())
    result = ReviewPipeline(config).run(sample_diff)
    assert result.stats["llm_error"] == "simulated outage"
    assert result.stats["model_kept"] == 0
    assert all(f.source == "rule" for f in result.findings)
    assert render_verdict(result).startswith("NON-BLOCKING FINDINGS")


def test_llm_failure_can_fail_hard_when_configured(sample_diff):
    config = PipelineConfig(
        llm_backend=ExplodingLLMBackend(), fail_on_llm_error=True
    )
    with pytest.raises(LLMBackendError):
        ReviewPipeline(config).run(sample_diff)


def test_garbage_input_is_input_error_not_crash():
    with pytest.raises(PipelineError):
        ReviewPipeline().run("not a diff at all\n")


def test_verdict_recommends_human_review_for_model_major(sample_diff):
    result = ReviewPipeline().run(sample_diff)
    # Model majors recommend review but do not block without a rule blocker.
    assert render_verdict(result).startswith("REVIEW RECOMMENDED")


def test_verdict_marks_skipped_files_as_incomplete():
    diff = (
        "diff --git a/logo.png b/logo.png\n"
        "index 123..456 100644\n"
        "Binary files a/logo.png and b/logo.png differ\n"
    )
    result = ReviewPipeline().run(diff)
    assert render_verdict(result).startswith("INCOMPLETE REVIEW")


def test_verdict_distinguishes_empty_and_all_rejected_results():
    assert render_verdict(ReviewResult()).startswith("NO ISSUES FLAGGED")
    rejected = ReviewResult(stats={"human_rejected": 2})
    assert render_verdict(rejected).startswith("ALL FINDINGS REJECTED")


class _BlockerMintingLLMBackend(StubLLMBackend):
    def generate(self, diff_text, context):
        return [
            Finding(
                file="billing/invoice.py",
                line=15,
                severity="blocker",
                category="security",
                message="model-only imagined blocker",
                confidence=0.99,
            )
        ]


def test_custom_llm_backend_can_be_injected(sample_diff):
    config = PipelineConfig(llm_backend=_BlockerMintingLLMBackend())
    result = ReviewPipeline(config).run(sample_diff)
    model_findings = [f for f in result.findings if f.source == "model"]
    assert len(model_findings) == 1
    # severity cap does its job on injected backends too
    assert model_findings[0].severity == "major"


def test_invalid_model_line_is_dropped(sample_diff):
    class InvalidLineLLMBackend(StubLLMBackend):
        def generate(self, diff_text, context):
            return [
                Finding(
                    file="billing/invoice.py",
                    line="not-a-number",
                    severity="minor",
                    category="general",
                    message="Invalid line type",
                )
            ]

    result = ReviewPipeline(
        PipelineConfig(llm_backend=InvalidLineLLMBackend())
    ).run(sample_diff)

    assert result.stats["model_invalid"] == 1
    assert all(f.message != "Invalid line type" for f in result.findings)


def test_secret_rule_blocks(tmp_path, sample_diff):
    """A rule blocker flips the verdict — proven with a real diff, not a mock."""
    diff_with_secret = sample_diff + (
        "diff --git a/config.py b/config.py\n"
        "--- a/config.py\n"
        "+++ b/config.py\n"
        "@@ -1,2 +1,3 @@\n"
        " import os\n"
        "+API_KEY = \"sk-0123456789abcdefghij\"\n"
        "+password = \"hunter2secret\"\n"
    )
    result = ReviewPipeline().run(diff_with_secret)
    assert any(
        f.category == "security" and f.severity == "blocker"
        for f in result.findings
    )
    assert render_verdict(result).startswith("REQUEST CHANGES")
