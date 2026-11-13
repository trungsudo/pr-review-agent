"""A hardened PR-review pipeline built on the inherited review_bot.py.

Parsing, rules, grounding, deduplication, and verdicts are deterministic.
The AI-driven candidate-generation stage is represented by ``LLMBackend``;
the CLI uses ``StubLLMBackend`` so the full flow runs without an API call.
"""

from review_agent.pipeline import ReviewPipeline, ReviewResult

__all__ = ["ReviewPipeline", "ReviewResult"]
