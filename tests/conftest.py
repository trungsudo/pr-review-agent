"""Shared fixtures: the exercise's own sample diff, loaded from disk.

Puts the repo root on ``sys.path`` so both ``python -m pytest`` and a bare
``pytest`` run import ``review_agent`` the same way.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

SOLUTION_ROOT = Path(__file__).resolve().parent.parent
if str(SOLUTION_ROOT) not in sys.path:
    sys.path.insert(0, str(SOLUTION_ROOT))


@pytest.fixture
def sample_diff() -> str:
    return (SOLUTION_ROOT / "examples" / "sample_diff.txt").read_text(
        encoding="utf-8"
    )
