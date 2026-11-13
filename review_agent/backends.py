"""The AI-driven candidate-generation seam used by the review pipeline.

The active CLI uses ``StubLLMBackend`` instead of performing inference. Its
fixed output keeps the AI boundary testable without an API key or network access.
"""

from __future__ import annotations

from abc import ABC, abstractmethod

from review_agent.findings import Finding


class LLMBackendError(Exception):
    """The AI-driven candidate-generation stage failed."""


class LLMBackend(ABC):
    """Represent the AI-driven step that proposes candidate findings.

    The contract is shared by the local stub and any future live LLM adapter:

    - ``generate`` returns raw candidates or raises ``LLMBackendError``;
    - candidates may be malformed (unknown severity word, out-of-range
      line, hallucinated file). Sanitizing them is the pipeline's job,
      not this interface's, because the pipeline owns the parsed diff and
      is the only stage that can verify file and line anchors.
    """

    @abstractmethod
    def generate(self, diff_text: str, context: dict) -> list[Finding]:
        raise NotImplementedError

    @property
    def name(self) -> str:
        return type(self).__name__


class StubLLMBackend(LLMBackend):
    """Deterministic simulation of AI-generated findings for demos and tests.

    Replays a fixed catalogue of plausible candidates — including
    deliberately flawed ones (an unknown severity word, an out-of-range
    line, a hallucinated file, a duplicate of what the rule engine finds)
    so the sanitizer, dedup and cap stages downstream are exercised on
    every run, not just in dedicated tests. This makes the AI boundary's
    trust checks reviewable without an API key, network dependency, or
    changing model responses. The catalog is a test fixture, not an actual
    analysis of the supplied diff.
    """

    CATALOG: list[dict] = [
        {
            "file": "billing/invoice.py",
            "line": 14,
            "severity": "critical",  # not a real severity: must coerce
            "category": "billing",
            "message": "Tax applied multiplicatively; confirm the rounding "
            "policy for totals with finance.",
            "confidence": 0.4,
        },
        {
            # Same defect the bare-except rule reports, a few lines off:
            # exercises rule/model dedup.
            "file": "billing/invoice.py",
            "line": 33,
            "severity": "major",
            "category": "error-handling",
            "message": "A failing discount now silently vanishes inside the "
            "swallowed exception.",
            "confidence": 0.7,
        },
        {
            "file": "Backend.py",  # hallucinated path: must be dropped
            "line": 5,
            "severity": "major",
            "category": "error-handling",
            "message": "File that is not part of this diff at all.",
            "confidence": 0.9,
        },
        {
            "file": "billing/invoice.py",
            "line": 999,  # out of range: must end up unanchored + capped
            "severity": "major",
            "category": "security",
            "message": "Overall file could use a look at input validation "
            "of constructor arguments.",
            "confidence": 0.5,
        },
        {
            "file": "billing/invoice.py",
            "line": 40,
            "severity": "blocker",  # model claim: must cap to major
            "category": "security",
            "message": "CSV export interpolates values into the file "
            "without escaping; a crafted invoice id could inject rows.",
            "suggestion": "Use the csv module instead of f-string assembly.",
            "confidence": 0.6,
        },
    ]

    def __init__(self, catalog: list[dict] | None = None):
        self.catalog = catalog if catalog is not None else self.CATALOG

    def generate(self, diff_text: str, context: dict) -> list[Finding]:
        # Pure function of the catalogue: same input, same output, always.
        return [Finding(**dict(item)) for item in self.catalog]
