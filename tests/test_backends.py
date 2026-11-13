"""Stub backend and CLI behavior tests."""

from __future__ import annotations

import io
import json

from review_agent.backends import StubLLMBackend
from review_agent.cli import main as cli_main


def test_stub_llm_is_deterministic():
    ctx = {"changed_files": [], "rule_findings": []}
    a = StubLLMBackend().generate("x", ctx)
    b = StubLLMBackend().generate("x", ctx)
    assert [f.to_dict() for f in a] == [f.to_dict() for f in b]


def test_cli_runs_one_diff_file(tmp_path, sample_diff, capsys):
    diff = tmp_path / "d.txt"
    diff.write_text(sample_diff)
    code = cli_main([str(diff)])
    assert code == 0  # no rule blocker: review is advisory, not request-changes
    assert "REVIEW RECOMMENDED" in capsys.readouterr().out


def test_cli_requires_exactly_one_diff_file(capsys):
    assert cli_main([]) == 2
    assert "usage:" in capsys.readouterr().err
    assert cli_main(["first.diff", "second.diff"]) == 2


def test_cli_missing_file_is_input_error(capsys):
    code = cli_main(["/nonexistent/diff.txt"])
    assert code == 2
    assert "cannot read" in capsys.readouterr().err


def test_cli_secret_diff_exits_request_changes(tmp_path, sample_diff):
    diff = tmp_path / "d.txt"
    diff.write_text(
        sample_diff
        + 'diff --git a/cfg.py b/cfg.py\n--- a/cfg.py\n+++ b/cfg.py\n'
        '@@ -1,2 +1,3 @@\n+API_KEY = "sk-0123456789abcdefghij"\n'
    )
    assert cli_main([str(diff)]) == 1


def test_cli_interactive_reports_only_accepted_findings(
    tmp_path, sample_diff, monkeypatch, capsys
):
    import builtins
    import sys

    diff = tmp_path / "d.txt"
    diff.write_text(sample_diff)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(sys, "stdin", io.StringIO())
    monkeypatch.setattr(sys.stdin, "isatty", lambda: True)
    choices = iter(
        ["e", "minor", "Edited exception finding", "a", "r", "r", "r", "r"]
    )
    monkeypatch.setattr(builtins, "input", lambda _prompt: next(choices))

    code = cli_main(["--interactive", str(diff)])

    output = capsys.readouterr().out
    assert code == 0
    assert "**Human review:** accepted 1, rejected 4." in output
    assert "Exception handler swallows the error silently" in output
    assert ">   35 +        except Exception:" in output
    assert "    36 +            pass" in output
    assert "Diff context: (no changed-line anchor)" in output
    final_report = output.split("**Human review:**", 1)[1]
    assert (
        "**[minor]** `billing/invoice.py:35` - Edited exception finding"
        in final_report
    )
    assert "Tax applied multiplicatively" not in final_report
    records = [
        json.loads(line)
        for line in (tmp_path / "decisions.jsonl").read_text(
            encoding="utf-8"
        ).splitlines()
    ]
    assert len(records) == 5
    assert records[0]["decision"] == "accepted"
    assert records[0]["original_finding"]["severity"] == "major"
    assert records[0]["final_finding"]["severity"] == "minor"
    assert records[0]["final_finding"]["message"] == "Edited exception finding"
    assert "snippet" not in records[0]["original_finding"]
    assert records[1]["decision"] == "rejected"


def test_cli_interactive_requires_terminal(tmp_path, sample_diff, capsys):
    diff = tmp_path / "d.txt"
    diff.write_text(sample_diff)

    assert cli_main(["--interactive", str(diff)]) == 2
    assert "--interactive requires a terminal" in capsys.readouterr().err


def test_cli_interactive_accepting_blocker_returns_request_changes(
    tmp_path, sample_diff, monkeypatch, capsys
):
    import builtins
    import sys

    diff = tmp_path / "d.txt"
    diff.write_text(
        sample_diff
        + 'diff --git a/cfg.py b/cfg.py\n--- a/cfg.py\n+++ b/cfg.py\n'
        '@@ -1,2 +1,3 @@\n+API_KEY = "sk-0123456789abcdefghij"\n'
    )
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(sys, "stdin", io.StringIO())
    monkeypatch.setattr(sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr(builtins, "input", lambda _prompt: "a")

    assert cli_main(["--interactive", str(diff)]) == 1
    assert "REQUEST CHANGES" in capsys.readouterr().out


def test_cli_interactive_eof_is_reported_as_input_error(
    tmp_path, sample_diff, monkeypatch, capsys
):
    import builtins
    import sys

    diff = tmp_path / "d.txt"
    diff.write_text(sample_diff)
    monkeypatch.setattr(sys, "stdin", io.StringIO())
    monkeypatch.setattr(sys.stdin, "isatty", lambda: True)

    def eof(_prompt):
        raise EOFError

    monkeypatch.setattr(builtins, "input", eof)
    assert cli_main(["--interactive", str(diff)]) == 2
    assert "ended before all findings were decided" in capsys.readouterr().err


def test_cli_interactive_blocker_defaults_to_accept(
    tmp_path, sample_diff, monkeypatch, capsys
):
    import builtins
    import sys

    # Secret blocker + an all-Enter session: the blocker must survive the
    # default action, not be silently dropped.
    diff = tmp_path / "d.txt"
    diff.write_text(
        sample_diff
        + 'diff --git a/cfg.py b/cfg.py\n--- a/cfg.py\n+++ b/cfg.py\n'
        '@@ -1,2 +1,3 @@\n+API_KEY = "sk-0123456789abcdefghij"\n'
    )
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(sys, "stdin", io.StringIO())
    monkeypatch.setattr(sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr(builtins, "input", lambda _prompt: "")

    assert cli_main(["--interactive", str(diff)]) == 1
    assert "REQUEST CHANGES" in capsys.readouterr().out
