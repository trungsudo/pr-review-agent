"""Finding model and severity policy — shared vocabulary for every stage."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field

# Ordered lowest -> highest so comparisons and sorting are meaningful.
SEVERITIES = ("comment", "nit", "minor", "major", "blocker")


@dataclass
class Finding:
    """One review comment, anchored (when possible) to a changed line.

    ``source`` records whether it came from a deterministic rule or the
    AI-candidate stage. The CLI currently mocks that stage, so ``model``
    identifies the role of the finding, not proof that an LLM ran.
    """

    file: str
    line: int | None  # new-side line number, None if unanchored
    severity: str  # one of SEVERITIES
    category: str  # e.g. "error-handling", "security", "tests"
    message: str
    suggestion: str | None = None
    confidence: float = 1.0  # rules are 1.0; a live LLM would self-report
    source: str = "model"
    snippet: str | None = None

    def to_dict(self) -> dict:
        return asdict(self)

    def validate(self) -> "Finding":
        """Validate and normalize a candidate from the AI-driven stage."""
        # A bad line type or non-positive line must not reach hunk comparisons
        # or become a plausible-looking inline comment.
        if self.line is not None and (
            not isinstance(self.line, int)
            or isinstance(self.line, bool)
            or self.line < 1
        ):
            raise ValueError("line must be a positive integer or None")
        # A producer may return a non-string or unsupported label such as
        # "critical". A wrong *word* is still a usable observation that is
        # kept as a minor advisory; a non-string is malformed data and is
        # rejected outright.
        if not isinstance(self.severity, str):
            raise ValueError("severity must be a string")
        if self.severity not in SEVERITIES:
            self.severity = "minor"
        # Malformed scalars (message=None, confidence="high") must degrade to
        # a counted invalid candidate, never crash the review mid-gate.
        if not isinstance(self.message, str):
            raise ValueError("message must be a string")
        try:
            self.confidence = min(
                max(float(self.confidence or 0.0), 0.0), 1.0
            )
        except (TypeError, ValueError) as exc:
            raise ValueError("confidence must be numeric") from exc
        self.message = " ".join(self.message.split())
        if not self.message:
            raise ValueError("empty message")
        return self


@dataclass
class ReviewResult:
    """Everything the pipeline knows about one review run."""

    findings: list[Finding] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)  # disclosures, not errors
    stats: dict = field(default_factory=dict)
    unanchored: list[Finding] = field(default_factory=list)
