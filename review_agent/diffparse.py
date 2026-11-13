"""Parse a unified diff into structured hunks.

This is the deterministic foundation of the pipeline: every later stage
(severity classification, line mapping, gating) reasons over this structure
instead of re-reading raw text, so the same diff always produces the same
parse.

Skipped input (binary files, rename/copy-only entries) is recorded rather
than silently dropped, so a review can still cite what it did not look at.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field


@dataclass(frozen=True)
class LineChange:
    """One line inside a hunk, classified as add/remove/context."""

    file: str
    old_lineno: int | None  # None for added lines
    new_lineno: int | None  # None for removed lines
    kind: str  # "add" | "remove" | "context"
    content: str


@dataclass
class Hunk:
    """One @@ hunk from one file's diff."""

    file: str
    old_start: int
    new_start: int
    changes: list[LineChange] = field(default_factory=list)


@dataclass
class FileDiff:
    """All hunks for one file, plus what the parser admittedly skipped."""

    path: str
    is_binary: bool = False
    is_rename_only: bool = False
    hunks: list[Hunk] = field(default_factory=list)
    skipped_reason: str | None = None  # set for files we cannot review

    @property
    def added_lines(self) -> list[LineChange]:
        return [c for h in self.hunks for c in h.changes if c.kind == "add"]

    @property
    def removed_lines(self) -> list[LineChange]:
        return [c for h in self.hunks for c in h.changes if c.kind == "remove"]


class DiffParseError(Exception):
    """The input is not a unified diff we can reason about."""


class DiffParser:
    """Stateful parser for unified-diff text.

    Follows the shape git produces: ``diff --git a/x b/x`` headers,
    ``@@ -a,b +c,d @@`` ranges, then ``+``/``-``/``' '`` bodies. Tolerates
    the usual noise (index lines, mode lines, ``---``/``+++`` file headers)
    without interpreting it. The parsed new-side line numbers are the
    grounding source for findings; unsupported file sections are marked as
    skipped rather than treated as if they were reviewed.
    """

    # Greedy (.+) (.+) breaks when the path itself contains a space (the
    # b-side above wrongly becomes "file.py"). Prefer fixing the split on
    # the " b/" separator, which git emits for the second path.
    DIFF_HEADER = re.compile(r"^diff --git (?P<a>.+) (?P<b>b/.+)$")
    DIFF_HEADER_FALLBACK = re.compile(r"^diff --git (?P<a>.+) (?P<b>/dev/null)$")
    BINARY_MARKER = re.compile(r"^Binary files .* differ$")
    RENAME_FROM = re.compile(r"^rename from (.+)$")
    RENAME_TO = re.compile(r"^rename to (.+)$")
    HUNK_HEADER = re.compile(
        r"^@@ -(?P<os>\d+)(?:,(?P<oc>\d+))? \+(?P<ns>\d+)(?:,(?P<nc>\d+))? @@"
    )
    # Combined merge hunks (@@@) have no single old/new numbering we can map
    # findings to; the parser detects and discloses them instead of guessing.
    COMBINED_HUNK = re.compile(r"^@@@")

    def parse(self, diff_text: str) -> list[FileDiff]:
        """Parse ``diff_text`` into a list of per-file diffs.

        Raises DiffParseError when the text contains no diff header at all
        (i.e. it is certainly not a diff), but is lenient about per-file
        oddities: those become FileDiff entries with ``skipped_reason`` set
        instead of aborting the whole parse. Only ``\\n`` terminates a line:
        Python's ``splitlines()`` also splits on form feed, vertical tab, NEL
        and line separator, which would fabricate phantom changed lines out of
        a single line that merely contains one of those characters.
        """
        if not diff_text.strip():
            raise DiffParseError("empty diff")

        files: list[FileDiff] = []
        current: FileDiff | None = None
        hunk: Hunk | None = None
        old_lineno = new_lineno = 0

        lines = diff_text.split("\n")
        if lines and lines[-1] == "":
            # The artifact of the trailing newline, not an empty context
            # line; an empty line mid-file is legitimate and stays.
            lines.pop()
        for lineno, raw in enumerate(lines, start=1):
            raw = raw.rstrip("\r")
            header = (
                self.DIFF_HEADER.match(raw)
                or self.DIFF_HEADER_FALLBACK.match(raw)
            )
            if header:
                # Close out the previous file.
                current = FileDiff(path=self._path_from_header(header))
                files.append(current)
                hunk = None
                continue

            if current is None:
                # Noise before the first "diff --git" line: ignore quietly.
                continue

            if self.BINARY_MARKER.match(raw):
                current.is_binary = True
                current.skipped_reason = "binary file"
                hunk = None
                continue

            if self.COMBINED_HUNK.match(raw):
                # A combined (merge) hunk: line numbers are not mappable to
                # one side, so nothing in this section can be grounded.
                current.skipped_reason = (
                    current.skipped_reason or "combination merge diff"
                )
                hunk = None
                continue

            rename_from = self.RENAME_FROM.match(raw)
            if rename_from:
                current.is_rename_only = True
                current.path = rename_from.group(1)
                current.hunks = []
                continue
            rename_to = self.RENAME_TO.match(raw)
            if rename_to:
                current.is_rename_only = True
                current.path = f"{current.path} -> {rename_to.group(1)}"
                continue

            hh = self.HUNK_HEADER.match(raw)
            if hh:
                if current.is_rename_only and not current.hunks:
                    # A rename entry followed by real content hunks: the
                    # reviewable path is the destination, never the display
                    # string "old -> new" a finding would be anchored to.
                    current.path = current.path.split(" -> ")[-1]
                    current.is_rename_only = False
                    current.skipped_reason = None
                old_lineno = int(hh.group("os"))
                new_lineno = int(hh.group("ns"))
                hunk = Hunk(
                    file=current.path,
                    old_start=old_lineno,
                    new_start=new_lineno,
                )
                current.hunks.append(hunk)
                continue

            if hunk is None:
                # Header/noise inside a file section we don't interpret.
                continue

            if raw.startswith("\\"):
                # "\ No newline at end of file" (and friends): a marker about
                # the previous line, not a line itself. Skip so it does not
                # inflate the line counters or grow the hunk's range.
                continue

            if raw.startswith("+"):
                hunk.changes.append(
                    LineChange(
                        file=current.path,
                        old_lineno=None,
                        new_lineno=new_lineno,
                        kind="add",
                        content=raw[1:],
                    )
                )
                new_lineno += 1
            elif raw.startswith("-"):
                hunk.changes.append(
                    LineChange(
                        file=current.path,
                        old_lineno=old_lineno,
                        new_lineno=None,
                        kind="remove",
                        content=raw[1:],
                    )
                )
                old_lineno += 1
            else:
                # Context line (leading space, or an empty line which some
                # tools emit as truly empty rather than " ").
                hunk.changes.append(
                    LineChange(
                        file=current.path,
                        old_lineno=old_lineno,
                        new_lineno=new_lineno,
                        kind="context",
                        content=raw[1:] if raw.startswith(" ") else raw,
                    )
                )
                old_lineno += 1
                new_lineno += 1

        if not files:
            raise DiffParseError("no 'diff --git' header found in input")

        # A file whose section shows only hunks of pure removals with no
        # adds is a deletion; those stay reviewable (removed code can be a
        # finding), so no skipping here.
        for f in files:
            if f.skipped_reason is None and not f.hunks:
                f.skipped_reason = "no hunks (metadata-only change)"
        return files

    @staticmethod
    def _path_from_header(header: re.Match) -> str:
        # Prefer the b/ side: that is the post-change path reviewers comment
        # against. Fall back to the a/ side for deletions. Paths that contain
        # spaces break the a/b split itself; fall back to the a-side, which
        # keeps such a file attributable rather than inventing a wrong path.
        a, b = header.group("a"), header.group("b")
        candidate = b.removeprefix("b/")
        a_side = a.removeprefix("a/")
        if b == "/dev/null":
            return a_side
        if " " in candidate and " " not in a_side:
            # The greedy .+/.+ split put the boundary in the wrong place;
            # the b-side path wrongly swallowed another token.
            return a_side
        return candidate
