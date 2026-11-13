"""Deterministic rules — checks with known, high precision.

Design rule for this module: a deterministic rule only earns a place here when its
match condition is cheap to evaluate and its false-positive rate makes
an engineer trust it without double-checking. Everything fuzzier belongs
in the AI-candidate stage. The CLI currently uses a stub for that stage,
but the distinction remains: rule findings behave like linter output
(skip at your own risk); AI-stage findings are suggestions for a human.

Each rule sees the parsed diff (files + changed lines with real line
numbers), never raw regex-over-the-world, so grounding is inherited from
the parser instead of re-solved per rule.
"""

from __future__ import annotations

import re

from review_agent.diffparse import FileDiff
from review_agent.findings import Finding

# Python-focused to start; the catalogue is meant to grow per language.


class Rule:
    """Base class: subclass and set ID/SEVERITY/CATEGORY, implement check()."""

    ID: str = "rule"
    SEVERITY: str = "minor"
    CATEGORY: str = "general"

    def check(self, filediff: FileDiff) -> list[Finding]:
        raise NotImplementedError

    def _finding(
        self,
        change,
        message: str,
        suggestion: str | None = None,
        severity: str | None = None,
    ) -> Finding:
        return Finding(
            file=change.file,
            line=change.new_lineno or change.old_lineno,
            severity=severity or self.SEVERITY,
            category=self.CATEGORY,
            message=message,
            suggestion=suggestion,
            confidence=1.0,
            source="rule",
        ).validate()


class BareExcept(Rule):
    """``except: pass`` / ``except Exception: pass`` swallows failures.

    Classic silent-failure pattern: the diff being reviewed adds exactly
    this to billing code. The match is intentionally narrow: only a handler
    followed by a silent body earns a finding, because broad exception
    handling alone can be legitimate. The pipeline deduplicates a similar
    AI-stage candidate instead of presenting the same issue twice.
    """

    ID = "bare-except-swallow"
    SEVERITY = "major"
    CATEGORY = "error-handling"

    PATTERN = re.compile(
        r"except\s*(?::|Exception\s*(?:as\s+\w+)?\s*:|"
        r"BaseException\s*(?:as\s+\w+)?\s*:)\s*(?:#.*)?$"
    )

    def check(self, filediff: FileDiff) -> list[Finding]:
        if not filediff.path.endswith(".py"):
            return []
        findings: list[Finding] = []
        for hunk in filediff.hunks:
            # Stay inside one hunk: the next hunk's opening lines belong to a
            # different part of the file, so peeking past the boundary would
            # anchor findings on unrelated code.
            changes = hunk.changes
            for i, change in enumerate(changes):
                if change.kind != "add":
                    continue
                body = change.content.strip()
                if not self.PATTERN.match(body):
                    continue
                # Look ahead at the code this PR adds: is the handler body a
                # pass/...? Removed lines are the old version (often exactly
                # the pass being fixed away); a blank line is not a body.
                upcoming = [
                    c
                    for c in changes[i + 1 : i + 3]
                    if c.kind == "add"
                ]
                if any(
                    c.content.strip() in ("pass", "...", "return None")
                    for c in upcoming
                ):
                    findings.append(
                        self._finding(
                            change,
                            "Exception handler swallows the error silently "
                            "(except + pass). Failures in this path become "
                            "invisible.",
                            suggestion="Log, narrow the exception type, or "
                            "let it propagate - silent pass hides real "
                            "defects.",
                        )
                    )
        return findings


class SecretInDiff(Rule):
    """Hard-coded credentials/keys added by a PR.

    review_bot.py itself shipped with a pasted API key; this rule makes
    that class of mistake a deterministic blocker rather than something
    we hope a model notices. Blocking is reserved for recognizable secret
    patterns; uncertain security concerns stay advisory.
    """

    ID = "secret-in-diff"
    SEVERITY = "blocker"
    CATEGORY = "security"

    PATTERNS = (
        re.compile(r"sk-[A-Za-z0-9]{16,}"),
        re.compile(r"(?i)api[_-]?key\s*=\s*[\"'][^\"']{12,}[\"']"),
        re.compile(r"-----BEGIN (?:RSA )?PRIVATE KEY-----"),
        re.compile(r"(?i)password\s*=\s*[\"'][^\"']{6,}[\"']"),
    )

    def check(self, filediff: FileDiff) -> list[Finding]:
        findings: list[Finding] = []
        for hunk in filediff.hunks:
            for change in hunk.changes:
                if change.kind != "add":
                    continue
                for pattern in self.PATTERNS:
                    m = pattern.search(change.content)
                    if m:
                        findings.append(
                            self._finding(
                                change,
                                "Possible hard-coded secret committed in "
                                "this line; secrets must come from the "
                                "environment or a vault.",
                                suggestion="Remove the secret, rotate the "
                                "credential if it ever ran anywhere, and "
                                "read it from env/config instead.",
                            )
                        )
                        break
        return findings


class DebugLeftBehind(Rule):
    """print/pprint debugging left in production paths."""

    ID = "debug-print"
    SEVERITY = "nit"
    CATEGORY = "hygiene"

    PATTERN = re.compile(r"^\s*(?:print|pprint)\s*\(")

    def check(self, filediff: FileDiff) -> list[Finding]:
        if not filediff.path.endswith(".py"):
            return []
        findings: list[Finding] = []
        for hunk in filediff.hunks:
            for change in hunk.changes:
                if (
                    change.kind == "add"
                    and self.PATTERN.match(change.content)
                ):
                    findings.append(
                        self._finding(
                            change,
                            "Debug print left in the diff; prefer the "
                            "project logger.",
                        )
                    )
        return findings


class NoTestsForLogic(Rule):
    """Logic-only Python change with zero test-file changes.

    Deliberately a *comment*, not an error: absence of test changes is easy
    to detect but does not prove a defect. It prompts the author without
    turning a heuristic into merge authority.
    """

    ID = "no-tests"
    SEVERITY = "comment"
    CATEGORY = "tests"

    def check_fileless(self, files: list[FileDiff]) -> Finding | None:
        code_files = [
            f
            for f in files
            if f.path.endswith(".py")
            and not f.skipped_reason
            and f.added_lines
        ]
        test_files = [
            f
            for f in files
            if not f.skipped_reason and self._is_test_path(f.path)
        ]
        if code_files and not test_files:
            main = max(code_files, key=lambda f: len(f.added_lines))
            return Finding(
                file=main.path,
                line=main.added_lines[0].new_lineno,
                severity="comment",
                category="tests",
                message=(
                    "This PR changes logic in "
                    f"{len(code_files)} file(s) without touching any test "
                    "file. Fine if intentional; worth a line in the "
                    "description otherwise."
                ),
                confidence=1.0,
                source="rule",
            ).validate()
        return None

    @staticmethod
    def _is_test_path(path: str) -> bool:
        # A path segment named test/tests (not a substring: "latest.js" is
        # not a test file) or a test_*.py / *_test.py filename.
        name = path.rsplit("/", 1)[-1].lower()
        if name.startswith("test_") or name.endswith("_test.py"):
            return True
        return any(
            seg in ("test", "tests") for seg in path.lower().split("/")
        )


# Keep the blocking rule set small and explainable. Broader, less certain
# concerns belong in advisory findings, not automatic merge policy.
RULES: list[Rule] = [BareExcept(), SecretInDiff(), DebugLeftBehind()]


def run_rules(files: list[FileDiff]) -> list[Finding]:
    """Run every per-line rule over every reviewable file."""
    findings: list[Finding] = []
    for filediff in files:
        if filediff.skipped_reason:
            continue
        for rule in RULES:
            findings.extend(rule.check(filediff))

    no_tests = NoTestsForLogic().check_fileless(files)
    if no_tests:
        findings.append(no_tests)
    return findings
