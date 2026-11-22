"""Rule-engine tests: each rule catches its pattern, ignores its non-pattern."""

from __future__ import annotations

from review_agent.diffparse import DiffParser
from review_agent.rules import (
    BareExcept,
    DebugLeftBehind,
    NoTestsForLogic,
    SecretInDiff,
)


def _one_file_diff(body_lines: list[str], path: str = "app.py") -> str:
    """Wrap already-prefixed diff body lines (+/ /-) in a valid diff."""
    return (
        f"diff --git a/{path} b/{path}\n"
        f"--- a/{path}\n"
        f"+++ b/{path}\n"
        "@@ -1,3 +1,6 @@\n"
        + "\n".join(body_lines)
    )


def test_bare_except_fires_on_sample_pattern():
    diff = _one_file_diff(
        [
            " try:",
            "     do_thing()",
            "+except Exception:",
            "+    pass",
        ]
    )
    findings = BareExcept().check(DiffParser().parse(diff)[0])
    assert len(findings) == 1
    assert findings[0].severity == "major"
    assert findings[0].line is not None


def test_bare_except_ignores_real_handler():
    diff = _one_file_diff(
        [
            " try:",
            "     do_thing()",
            "+except ValueError:",
            "+    log.warning('bad value')",
        ]
    )
    assert BareExcept().check(DiffParser().parse(diff)[0]) == []


def test_bare_except_ignores_non_python():
    diff = _one_file_diff(
        [
            " try:",
            "   x()",
            "+except:",
            "+  pass",
        ],
        path="app.js",
    )
    assert BareExcept().check(DiffParser().parse(diff)[0]) == []


def test_bare_except_ignores_blank_line_then_real_handler():
    # The parser retains the blank added line, but it does not count as a
    # silent body; the next added statement propagates the error.
    diff = _one_file_diff(
        [
            " try:",
            "     do_thing()",
            "+    except Exception:",
            "+",
            "+        raise ValueError('real handling')",
        ]
    )
    assert BareExcept().check(DiffParser().parse(diff)[0]) == []


def test_bare_except_lookahead_does_not_cross_hunks():
    # "except Exception:" at the end of one hunk and "pass" at the start of
    # the next belong to different parts of the file.
    diff = (
        "diff --git a/app.py b/app.py\n"
        "--- a/app.py\n+++ b/app.py\n"
        "@@ -1,2 +1,2 @@\n"
        " ctx\n"
        "+    except Exception:\n"
        "@@ -50,2 +50,3 @@\n"
        " other\n"
        "+    pass\n"
        " tail\n"
    )
    assert BareExcept().check(DiffParser().parse(diff)[0]) == []


def test_bare_except_ignores_pr_that_fixes_the_pattern():
    # The PR *removes* except+pass and adds real handling: the old removed
    # "pass" must not satisfy the lookahead and flag the fix.
    diff = (
        "diff --git a/app.py b/app.py\n"
        "--- a/app.py\n+++ b/app.py\n"
        "@@ -1,5 +1,5 @@\n"
        " ctx\n"
        "+    except Exception:\n"
        "-        pass\n"
        " ctx2\n"
        "+        logger.exception('x')\n"
    )
    assert BareExcept().check(DiffParser().parse(diff)[0]) == []


def test_bare_except_catches_as_binding_and_trailing_comment():
    diff = _one_file_diff(
        [
            "+    try:",
            "+        do_thing()",
            "+    except Exception as e:  # broad on purpose",
            "+        pass",
        ]
    )
    findings = BareExcept().check(DiffParser().parse(diff)[0])
    assert len(findings) == 1


def test_secret_rule_fires_on_openai_style_key():
    diff = _one_file_diff(['+client = OpenAI(api_key="sk-abc123def456ghi789jkl")'])
    f = SecretInDiff().check(DiffParser().parse(diff)[0])
    assert len(f) == 1 and f[0].severity == "blocker"


def test_secret_rule_ignores_env_lookup():
    diff = _one_file_diff(['+client = OpenAI(api_key=os.environ["OPENAI_API_KEY"])'])
    assert SecretInDiff().check(DiffParser().parse(diff)[0]) == []


def test_debug_print_rule():
    diff = _one_file_diff(["+print(response)"])
    f = DebugLeftBehind().check(DiffParser().parse(diff)[0])
    assert len(f) == 1 and f[0].severity == "nit"


def test_debug_print_ignores_logger_calls():
    diff = _one_file_diff(["+logger.info('done')"])
    assert DebugLeftBehind().check(DiffParser().parse(diff)[0]) == []


def test_no_tests_for_logic_checks_the_whole_pr():
    parser = DiffParser()
    app_files = parser.parse(_one_file_diff(["+value = 1"], "src/app.py"))

    finding = NoTestsForLogic().check_pr(app_files)
    assert finding is not None
    assert finding.category == "tests"
    assert finding.severity == "comment"

    test_files = parser.parse(
        _one_file_diff(["+def test_value():", "+    assert True"], "tests/test_app.py")
    )
    assert NoTestsForLogic().check_pr(app_files + test_files) is None
