"""Parser tests: structure, line numbers and skip-disclosure."""

from __future__ import annotations

import pytest

from review_agent.diffparse import DiffParseError, DiffParser


def test_parses_sample_diff(sample_diff):
    files = DiffParser().parse(sample_diff)
    assert len(files) == 1
    assert files[0].path == "billing/invoice.py"
    assert len(files[0].hunks) == 2


def test_line_numbers_are_real_positions(sample_diff):
    files = DiffParser().parse(sample_diff)
    added = files[0].added_lines
    # Hunk 1 @@ -12,7 +12,7 @@: two context lines (12, 13), then the
    # "+" line lands on new line 14 per git's numbering-from-header.
    assert [c.new_lineno for c in added if c.new_lineno and c.new_lineno < 30] == [14]
    # content is the line without the leading "+", indentation preserved
    tax_line = added[0]
    assert tax_line.content.strip().startswith(
        "return subtotal * (1 + self.tax_rate)"
    )


def test_removed_lines_carry_old_positions(sample_diff):
    files = DiffParser().parse(sample_diff)
    removed = files[0].removed_lines
    assert all(c.old_lineno is not None for c in removed)
    assert all(c.new_lineno is None for c in removed)


def test_multi_hunk_line_numbers(sample_diff):
    files = DiffParser().parse(sample_diff)
    second = files[0].hunks[1]
    assert second.new_start == 30  # per the @@ -30,6 +30,15 @@ header
    assert second.changes[0].kind == "context"
    # Added lines in hunk 2: the try-block (32-36), then export_csv (38-41)
    # after the blank context line 37.
    added2 = [c.new_lineno for c in second.changes if c.kind == "add"]
    assert added2 == [32, 33, 34, 35, 36, 38, 39, 40, 41]
    # except/pass pair lands on 35/36 — where the bare-except rule anchors.
    contents = {c.new_lineno: c.content.strip() for c in second.changes}
    assert contents[35] == "except Exception:"
    assert contents[36] == "pass"


def test_rejects_non_diff_input():
    with pytest.raises(DiffParseError):
        DiffParser().parse("hello world\nthis is not a diff\n")


def test_rejects_empty_input():
    with pytest.raises(DiffParseError):
        DiffParser().parse("")


def test_binary_file_is_disclosed_not_reviewed():
    diff = (
        "diff --git a/logo.png b/logo.png\n"
        "index 123..456 100644\n"
        "Binary files a/logo.png and b/logo.png differ\n"
    )
    files = DiffParser().parse(diff)
    assert files[0].is_binary is True
    assert files[0].skipped_reason == "binary file"


def test_rename_only_is_disclosed():
    diff = (
        "diff --git a/old.py b/new.py\n"
        "rename from old.py\n"
        "rename to new.py\n"
    )
    files = DiffParser().parse(diff)
    assert files[0].skipped_reason is not None
    assert "old.py" in files[0].path


def test_file_deletion_uses_a_side_path():
    diff = (
        "diff --git a/gone.py b/gone.py\n"
        "deleted file mode 100644\n"
        "index 123..000\n"
        "--- a/gone.py\n"
        "+++ /dev/null\n"
        "@@ -1,3 +0,0 @@\n"
        "-x = 1\n"
        "-y = 2\n"
        "-z = 3\n"
    )
    files = DiffParser().parse(diff)
    assert files[0].path == "gone.py"
    assert len(files[0].removed_lines) == 3
    assert not files[0].added_lines


def test_a_side_and_b_side_paths_both_work():
    diff = (
        "diff --git a/pkg/mod.py b/pkg/mod.py\n"
        "--- a/pkg/mod.py\n"
        "+++ b/pkg/mod.py\n"
        "@@ -1,3 +1,4 @@\n"
        " ctx\n"
        "+new\n"
    )
    files = DiffParser().parse(diff)
    # Both a/... and b/... paths resolve to the post-change path.
    assert files[0].path == "pkg/mod.py"


def test_no_newline_marker_is_not_a_line():
    # git emits "\ No newline at end of file" after the last line of a side.
    # It must not become a phantom context line and inflate line numbers.
    diff = (
        "diff --git a/app.py b/app.py\n"
        "--- a/app.py\n"
        "+++ b/app.py\n"
        "@@ -1,2 +1,3 @@\n"
        " ctx1\n"
        "+added_last_no_newline\n"
        "\\ No newline at end of file\n"
    )
    files = DiffParser().parse(diff)
    changes = files[0].hunks[0].changes
    assert [c.new_lineno for c in changes] == [1, 2]
    assert changes[-1].content == "added_last_no_newline"


def test_path_containing_space_is_attributable():
    # "diff --git a/my file.py b/my file.py" defeats the greedy split; the
    # file must stay attributable to one sane path, not a half-name hybrid.
    diff = (
        "diff --git a/my file.py b/my file.py\n"
        "--- a/my file.py\n"
        "+++ b/my file.py\n"
        "@@ -1,2 +1,3 @@\n"
        " ctx\n"
        "+new = 1\n"
    )
    files = DiffParser().parse(diff)
    assert files[0].path == "my file.py"
    assert files[0].added_lines[0].new_lineno == 2


def test_rename_with_content_hunks_uses_destination_path():
    diff = (
        "diff --git a/old.py b/new.py\n"
        "rename from old.py\n"
        "rename to new.py\n"
        "--- a/old.py\n"
        "+++ b/new.py\n"
        "@@ -1,2 +1,3 @@\n"
        " ctx\n"
        "+new = 1\n"
    )
    files = DiffParser().parse(diff)
    assert files[0].path == "new.py"
    assert files[0].hunks, "rename with content must stay reviewable"


def test_combined_merge_hunk_is_disclosed_not_guessed():
    diff = (
        "diff --git a/app.py b/app.py\n"
        "@@@ -5,3 -5,3 +5,4 @@@\n"
        " ctx\n"
        "+merged = 1\n"
    )
    files = DiffParser().parse(diff)
    assert files[0].skipped_reason is not None
    assert not files[0].hunks


def test_formfeed_inside_line_does_not_split_it():
    # str.splitlines() would split here; only \n may terminate a diff line.
    diff = (
        "diff --git a/app.py b/app.py\n"
        "--- a/app.py\n"
        "+++ b/app.py\n"
        "@@ -1,2 +1,3 @@\n"
        " ctx\n"
        '+weird = "a\fb"\n'
    )
    files = DiffParser().parse(diff)
    added = files[0].added_lines
    assert len(added) == 1
    assert added[0].new_lineno == 2
