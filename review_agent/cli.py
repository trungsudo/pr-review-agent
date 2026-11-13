"""Run the deterministic rules and stubbed AI stage against one diff file.

There is deliberately no provider-selection option: the exercise evaluates
the pipeline's judgment, not credentials, network access, or a live LLM
integration. ``--interactive`` lets a human inspect each finding's diff line
and select which findings to include in the final report.
"""

from __future__ import annotations

import json
import sys
from datetime import datetime, timezone
from pathlib import Path

from review_agent.diffparse import DiffParser
from review_agent.findings import Finding, SEVERITIES
from review_agent.pipeline import PipelineError, ReviewPipeline
from review_agent.report import render_markdown, render_verdict
from review_agent.stats import (
    DecisionLogError,
    load_decisions,
    render_stats,
)

EXIT_REQUEST_CHANGES = 1
EXIT_INPUT_ERROR = 2

USAGE = "usage: python -m review_agent [--interactive] <diff_file> | --stats"


class InteractiveReviewError(Exception):
    """The requested human review could not be completed."""


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)

    if args.count("--stats"):
        if len(args) != 1 or args[0] != "--stats":
            print(USAGE, file=sys.stderr)
            return EXIT_INPUT_ERROR
        return _run_stats()

    interactive = args.count("--interactive") == 1
    if args.count("--interactive") > 1:
        print(USAGE, file=sys.stderr)
        return EXIT_INPUT_ERROR
    if interactive:
        args.remove("--interactive")
    if len(args) != 1:
        print(USAGE, file=sys.stderr)
        return EXIT_INPUT_ERROR

    if interactive and not sys.stdin.isatty():
        print("error: --interactive requires a terminal", file=sys.stderr)
        return EXIT_INPUT_ERROR

    try:
        diff_text = Path(args[0]).read_text(encoding="utf-8")
    except OSError as exc:
        print(f"error: cannot read {args[0]}: {exc}", file=sys.stderr)
        return EXIT_INPUT_ERROR

    try:
        result = ReviewPipeline().run(diff_text)
    except PipelineError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return EXIT_INPUT_ERROR

    if interactive:
        original_count = len(result.findings)
        try:
            result.findings = _select_findings(result.findings, diff_text)
        except InteractiveReviewError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return EXIT_INPUT_ERROR
        result.stats["human_accepted"] = len(result.findings)
        result.stats["human_rejected"] = original_count - len(result.findings)
        result.unanchored = [
            f for f in result.findings if f.source == "model" and f.line is None
        ]

    print(render_markdown(result))

    if render_verdict(result).startswith("REQUEST CHANGES"):
        return EXIT_REQUEST_CHANGES
    return 0


def _run_stats() -> int:
    """Print measurements from the decision log (no diff, no model)."""
    try:
        records = load_decisions(Path("decisions.jsonl"))
    except DecisionLogError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return EXIT_INPUT_ERROR
    print(render_stats(records))
    return 0


def _select_findings(findings: list[Finding], diff_text: str) -> list[Finding]:
    """Review, optionally edit, and record each finding decision."""
    accepted = []
    change_locations = {
        (change.file, change.new_lineno): (hunk, index)
        for filediff in DiffParser().parse(diff_text)
        for hunk in filediff.hunks
        for index, change in enumerate(hunk.changes)
        if change.new_lineno is not None
    }

    for index, finding in enumerate(findings, 1):
        location = f"{finding.file}:{finding.line}" if finding.line else finding.file
        source = "RULE" if finding.source == "rule" else "AI-STAGE"
        print(
            f"\n[{index}/{len(findings)}] [{source}] "
            f"[{finding.severity}] {location}\n{finding.message}"
        )
        location = change_locations.get((finding.file, finding.line))
        if location:
            hunk, anchor_index = location
            print("Diff context (the finding's line is marked with >):")
            for change in hunk.changes[
                max(0, anchor_index - 2) : anchor_index + 3
            ]:
                marker = {
                    "add": "+",
                    "remove": "-",
                    "context": " ",
                }[change.kind]
                line_number = change.new_lineno or change.old_lineno
                current = ">" if change is hunk.changes[anchor_index] else " "
                print(
                    f"{current} {line_number:>4} "
                    f"{marker}{change.content}"
                )
        else:
            print("Diff context: (no changed-line anchor)")
        if finding.suggestion:
            print(f"Suggestion: {finding.suggestion}")

        original_finding = _finding_for_log(finding)
        # A blocker defaults to Accept so an accidental Enter cannot drop a
        # merge-blocking finding; everything else defaults to Reject.
        default = "a" if finding.severity == "blocker" else "r"
        while True:
            choice = _read_review_input(
                f"Action [A]ccept / [R]eject / [E]dit "
                f"(default: {default.upper()}): "
            ).strip().lower()
            if not choice:
                choice = default
            if choice in ("e", "edit"):
                _edit_finding(finding)
                default = "a" if finding.severity == "blocker" else "r"
                print(
                    f"Updated: [{finding.severity}] {finding.message}"
                )
                continue
            if choice in ("r", "reject"):
                decision = "rejected"
                break
            if choice in ("a", "accept"):
                decision = "accepted"
                break
            print("Please answer a, r, or e.")

        _record_decision(decision, original_finding, finding)
        if decision == "accepted":
            accepted.append(finding)
    return accepted


def _edit_finding(finding: Finding) -> None:
    while True:
        severity = _read_review_input(
            f"Severity [{finding.severity}] "
            f"({', '.join(SEVERITIES)}; Enter keeps current): "
        ).strip().lower()
        if not severity:
            break
        if severity in SEVERITIES:
            finding.severity = severity
            break
        print(f"Choose one of: {', '.join(SEVERITIES)}.")

    message = _read_review_input(
        "Description (Enter keeps current): "
    ).strip()
    if message:
        finding.message = message


def _read_review_input(prompt: str) -> str:
    try:
        return input(prompt)
    except EOFError as exc:
        raise InteractiveReviewError(
            "interactive review ended before all findings were decided"
        ) from exc


def _finding_for_log(finding: Finding) -> dict:
    record = finding.to_dict()
    record.pop("snippet", None)
    return record


def _record_decision(
    decision: str, original_finding: dict, final_finding: Finding
) -> None:
    record = {
        "schema_version": 1,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "decision": decision,
        "original_finding": original_finding,
        "final_finding": _finding_for_log(final_finding),
    }
    try:
        with Path("decisions.jsonl").open("a", encoding="utf-8") as log_file:
            log_file.write(json.dumps(record, ensure_ascii=False) + "\n")
    except OSError as exc:
        raise InteractiveReviewError(
            f"cannot append decision to decisions.jsonl: {exc}"
        ) from exc


if __name__ == "__main__":
    sys.exit(main())
