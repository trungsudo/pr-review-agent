"""Render a ReviewResult into a report and a machine-facing verdict.

Two shapes from one result:

- ``render_markdown`` — a human-readable report. Deterministic rule
  findings and mocked AI-stage observations are visually separated because
  they carry different trust levels.
- ``render_verdict`` — the machine-facing gate. Deterministic policy,
  applied to deterministic findings only: rule blockers request changes,
  AI-stage suggestions can recommend review but never block.
"""

from __future__ import annotations

from review_agent.findings import Finding, ReviewResult

SEVERITY_BADGES = {
    "blocker": "!!",
    "major": "!",
    "minor": "~",
    "nit": "i",
    "comment": "-",
}


def render_markdown(result: ReviewResult) -> str:
    parts: list[str] = []

    if result.stats.get("llm_error"):
        parts.append(
            "> **Note:** AI candidate generation failed "
            f"(`{result.stats['llm_error']}`). This review contains "
            "deterministic findings only.\n"
        )

    if result.skipped:
        parts.append("**Not reviewed:** " + ", ".join(result.skipped) + "\n")

    if "human_accepted" in result.stats:
        parts.append(
            f"**Human review:** accepted {result.stats['human_accepted']}, "
            f"rejected {result.stats['human_rejected']}.\n"
        )

    rules = [f for f in result.findings if f.source == "rule"]
    ai_findings = [f for f in result.findings if f.source == "model"]

    # Provenance is visible in the report so reviewers can calibrate trust:
    # a deterministic match and an AI-stage suggestion should not look equivalent.
    parts.append(f"## Automated review - {len(result.findings)} finding(s)\n")

    if rules:
        parts.append("### Checked automatically (high confidence)\n")
        parts.extend(_render_finding(f) for f in rules)

    if ai_findings:
        parts.append("\n### AI-stage observations (verify before acting)\n")
        parts.extend(_render_finding(f) for f in ai_findings)
        unanchored = [f for f in ai_findings if f.line is None]
        if unanchored:
            parts.append(
                f"\n*{len(unanchored)} AI-stage observation(s) could not be "
                "anchored to a changed line and are shown file-wide.*"
            )

    verdict = render_verdict(result)
    parts.append(f"\n---\n**Verdict: {verdict}**")

    stats = result.stats
    parts.append(
        "<details><summary>run stats</summary>\n\n"
        f"```\nllm_backend={stats.get('llm_backend')} "
        f"files={stats.get('files')} rule={stats.get('rule_findings')} "
        f"model_raw={stats.get('model_raw')} "
        f"dropped[invalid={stats.get('model_invalid')} "
        f"ungrounded={stats.get('model_ungrounded')} "
        f"deduped={stats.get('model_deduped')}] "
        f"model_kept={stats.get('model_kept')}\n```\n</details>"
    )
    return "\n".join(parts)


def _render_finding(f: Finding) -> str:
    badge = SEVERITY_BADGES.get(f.severity, "-")
    where = f"{f.file}:{f.line}" if f.line else f"{f.file} (file-wide)"
    lines = [
        f"- {badge} **[{f.severity}]** `{where}` - {f.message}"
    ]
    if f.suggestion:
        lines.append(f"  - *Suggestion:* {f.suggestion}")
    return "\n".join(lines)


def render_verdict(result: ReviewResult) -> str:
    """Only a curated deterministic rule may request changes.

    The stub's confidence values are illustrative, not calibrated evidence.
    Therefore AI-stage suggestions ask for human attention rather than
    blocking a merge.
    """
    rule_blockers = [
        f
        for f in result.findings
        if f.source == "rule" and f.severity == "blocker"
    ]
    if rule_blockers:
        return (
            "REQUEST CHANGES - "
            f"{len(rule_blockers)} blocking rule violation(s)"
        )

    if result.skipped:
        return (
            "INCOMPLETE REVIEW - "
            f"{len(result.skipped)} file(s) skipped; see Not reviewed above"
        )

    if not result.findings:
        if result.stats.get("human_rejected", 0):
            return "ALL FINDINGS REJECTED - human reviewer rejected all findings"
        return "NO ISSUES FLAGGED - absence of findings is not proof"

    ai_major = [
        f
        for f in result.findings
        if f.source == "model" and f.severity == "major"
    ]
    if ai_major:
        return (
            "REVIEW RECOMMENDED - AI-stage findings include a possible major "
            "issue; human decides"
        )
    return "NON-BLOCKING FINDINGS - no blocker found"
