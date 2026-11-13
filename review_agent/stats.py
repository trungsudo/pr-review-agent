"""Read the decision log back — the missing reader half of the loop.

``--interactive`` appends one JSON object per reviewed finding to
``decisions.jsonl``. This module turns that history into the measurements
the README promises: acceptance rate by source (rules vs AI-stage) and by
category, plus the most-rejected messages. Those numbers are the evidence
a maintainer uses to tune rules, prompts, and how much authority the
AI-stage may carry.

The numbers are only as meaningful as the humans behind them: a handful of
self-reviewed runs measure nothing beyond one person's taste. The command
exists so the loop is closeable from day one, not to bless tiny samples.
"""

from __future__ import annotations

import json
from collections import Counter
from pathlib import Path

from review_agent.findings import Finding

LOG_NAME = "decisions.jsonl"
SCHEMA_VERSION = 1


class DecisionLogError(Exception):
    """The decision log exists but cannot be used."""


def load_decisions(path: Path) -> list[dict]:
    """Read and schema-check the decision log; missing file is empty history.

    Malformed lines abort with a clear error rather than being skipped:
    a silently partial history would understate rejection counts and make
    the rates look better than they are.
    """
    if not path.exists():
        return []
    records: list[dict] = []
    with path.open(encoding="utf-8") as fh:
        for lineno, line in enumerate(fh, start=1):
            line = line.strip()
            if not line:
                continue
            try:
                record = json.loads(line)
            except json.JSONDecodeError as exc:
                raise DecisionLogError(
                    f"{path}:{lineno}: not valid JSON: {exc}"
                ) from exc
            if record.get("schema_version") != SCHEMA_VERSION:
                raise DecisionLogError(
                    f"{path}:{lineno}: unsupported schema_version "
                    f"{record.get('schema_version')!r} "
                    f"(expected {SCHEMA_VERSION}); refusing to mix formats"
                )
            if record.get("decision") not in ("accepted", "rejected"):
                raise DecisionLogError(
                    f"{path}:{lineno}: unknown decision "
                    f"{record.get('decision')!r}"
                )
            records.append(record)
    return records


def summarize(records: list[dict]) -> dict:
    """Aggregate decisions into acceptance counts by source and category."""
    by_source: Counter = Counter()
    by_category: Counter = Counter()
    accepted_source: Counter = Counter()
    accepted_category: Counter = Counter()
    rejected_messages: Counter = Counter()

    for record in records:
        finding = record.get("final_finding") or record.get("finding") or {}
        source = finding.get("source", "unknown")
        category = finding.get("category", "unknown")
        decision = record["decision"]
        by_source[source] += 1
        by_category[category] += 1
        if decision == "accepted":
            accepted_source[source] += 1
            accepted_category[category] += 1
        else:
            rejected_messages[finding.get("message", "")] += 1

    def rates(keys: Counter, accepted: Counter) -> list[dict]:
        return [
            {
                "key": key,
                "decisions": count,
                "accepted": accepted[key],
                "rate": accepted[key] / count,
            }
            for key, count in sorted(keys.items())
        ]

    return {
        "total": len(records),
        "by_source": rates(by_source, accepted_source),
        "by_category": rates(by_category, accepted_category),
        "top_rejected": [
            {"message": msg, "count": count}
            for msg, count in rejected_messages.most_common(5)
        ],
    }


def render_stats(records: list[dict], log_name: str = LOG_NAME) -> str:
    """Render a summary as human-readable Markdown."""
    summary = summarize(records)
    if not records:
        return (
            f"No decisions logged yet ({log_name} not found).\n"
            "Run an interactive review first: "
            "python -m review_agent --interactive <diff_file>"
        )

    lines = [f"## Decision log — {summary['total']} decision(s)\n"]

    def table(title: str, rows: list[dict]) -> None:
        lines.append(f"### {title}\n")
        lines.append("| Key | Decisions | Accepted | Rate |")
        lines.append("|---|---:|---:|---:|")
        for row in rows:
            lines.append(
                f"| {row['key']} | {row['decisions']} | "
                f"{row['accepted']} | {row['rate']:.0%} |"
            )
        lines.append("")

    table("Acceptance by source (rules vs AI-stage)", summary["by_source"])
    table("Acceptance by category", summary["by_category"])

    if summary["top_rejected"]:
        lines.append("### Most-rejected messages\n")
        for item in summary["top_rejected"]:
            message = " ".join(item["message"].split())
            if len(message) > 90:
                message = message[:87] + "..."
            lines.append(f"- x{item['count']} {message}")
        lines.append("")

    lines.append(
        "*Rates measure the reviewer's judgment on logged findings. "
        "Small or self-reviewed samples say little — act on them only "
        "when the numbers come from real reviews.*"
    )
    return "\n".join(lines)


def finding_from_record(record: dict) -> Finding | None:
    """Rebuild the final finding of a record (helper for callers/tests)."""
    data = record.get("final_finding")
    if data is None:
        return None
    return Finding(**data)
