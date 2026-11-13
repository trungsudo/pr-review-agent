"""Decision-log reader tests: load, schema check, aggregation, rendering."""

from __future__ import annotations

import json

from review_agent.stats import (
    DecisionLogError,
    load_decisions,
    render_stats,
    summarize,
)


def _record(
    source: str,
    category: str,
    decision: str,
    message: str = "m",
) -> dict:
    finding = {
        "file": "a.py",
        "line": 1,
        "severity": "minor",
        "category": category,
        "message": message,
        "suggestion": None,
        "confidence": 1.0,
        "source": source,
        "snippet": None,
    }
    return {
        "schema_version": 1,
        "timestamp": "2026-01-01T00:00:00+00:00",
        "decision": decision,
        "original_finding": dict(finding),
        "final_finding": dict(finding),
    }


def test_missing_log_is_empty_history(tmp_path):
    assert load_decisions(tmp_path / "decisions.jsonl") == []


def test_render_stats_without_data_explains_how_to_create_it(
    tmp_path, monkeypatch, capsys
):
    import review_agent.cli as cli

    monkeypatch.chdir(tmp_path)
    assert cli.main(["--stats"]) == 0
    out = capsys.readouterr().out
    assert "No decisions logged yet" in out
    assert "--interactive" in out


def test_summarize_splits_by_source_and_category():
    records = [
        _record("rule", "error-handling", "accepted"),
        _record("rule", "hygiene", "rejected"),
        _record("model", "security", "rejected"),
        _record("model", "security", "rejected"),
        _record("rule", "error-handling", "accepted"),
    ]
    summary = summarize(records)
    assert summary["total"] == 5
    by_source = {row["key"]: row for row in summary["by_source"]}
    assert by_source["rule"]["accepted"] == 2
    assert by_source["rule"]["rate"] == 2 / 3
    assert by_source["model"]["rate"] == 0.0
    by_cat = {row["key"]: row for row in summary["by_category"]}
    assert by_cat["security"]["decisions"] == 2


def test_top_rejected_lists_worst_messages():
    records = [
        _record("rule", "hygiene", "rejected", message="debug print"),
        _record("rule", "hygiene", "rejected", message="debug print"),
        _record("rule", "hygiene", "rejected", message="debug print"),
        _record("rule", "tests", "accepted"),
    ]
    summary = summarize(records)
    assert summary["top_rejected"][0]["message"] == "debug print"
    assert summary["top_rejected"][0]["count"] == 3


def test_render_stats_shows_tables_and_caveat():
    records = [
        _record("rule", "error-handling", "accepted"),
        _record("model", "security", "rejected"),
    ]
    text = render_stats(records)
    assert "## Decision log" in text
    assert "| rule | 1 | 1 | 100% |" in text
    assert "| model | 1 | 0 | 0% |" in text
    assert "self-reviewed samples" in text


def test_load_decisions_rejects_bad_schema_version(tmp_path):
    log = tmp_path / "decisions.jsonl"
    log.write_text(
        json.dumps(
            {**_record("rule", "x", "accepted"), "schema_version": 99}
        )
        + "\n",
        encoding="utf-8",
    )
    try:
        load_decisions(log)
    except DecisionLogError as exc:
        assert "schema_version" in str(exc)
    else:
        raise AssertionError("expected DecisionLogError")


def test_load_decisions_rejects_malformed_json(tmp_path):
    log = tmp_path / "decisions.jsonl"
    log.write_text("{not json}\n", encoding="utf-8")
    try:
        load_decisions(log)
    except DecisionLogError as exc:
        assert "not valid JSON" in str(exc)
    else:
        raise AssertionError("expected DecisionLogError")


def test_cli_stats_reads_real_log(tmp_path, monkeypatch, capsys):
    from review_agent.cli import main as cli_main

    monkeypatch.chdir(tmp_path)
    log = tmp_path / "decisions.jsonl"
    log.write_text(
        json.dumps(_record("rule", "error-handling", "accepted")) + "\n",
        encoding="utf-8",
    )
    assert cli_main(["--stats"]) == 0
    out = capsys.readouterr().out
    assert "| rule | 1 | 1 | 100% |" in out


def test_cli_stats_with_extra_args_is_input_error(capsys):
    from review_agent.cli import main as cli_main

    assert cli_main(["--stats", "some.diff"]) == 2
    assert "usage:" in capsys.readouterr().err
