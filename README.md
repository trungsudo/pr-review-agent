# PR Review Agent

## Problem and design

The inherited `review_bot.py` sent a pull request diff to an LLM and printed
whatever came back. I kept the PR-review problem, but replaced the one-shot
call with a bounded pipeline whose findings can be checked and reviewed.

I treated the script as a stopgap that may already have unattended callers:
the default stays non-interactive so it will not unexpectedly wait for input
or change an existing workflow. Human review is available explicitly with
`--interactive`. A non-interactive report does not mean an engineer accepted
its findings.

The agent produces a review report; it does not edit code or merge a PR.

> **AI-assisted development:** I used an AI coding agent to develop and revise
> this solution under my direction. I guided the design, reviewed generated
> changes, and requested iterations. Comments and documentation added or
> revised with the agent were drafted from my instructions and reviewed by me.
> I am responsible for the final submission and its trade-offs.

```text
unified diff
    |
parse files and line numbers
    |
deterministic rules
    |
AI candidates (receive the diff and rule findings as context)
    |
trust gate (validate, ground, deduplicate, limit)
    |
optional human review (if enabled) --> decisions.jsonl
    |
final report + verdict
```

The stages run sequentially because the AI candidate stage receives rule
findings as context. The current stub ignores that context and replays its
fixed catalogue. The trust gate filters AI candidates; rule findings remain
separately identifiable in the final report.

| Stage | What it does | Why |
|---|---|---|
| Diff parser (deterministic) | Maps files, hunks, and new-side line numbers; discloses unsupported or metadata-only changes. | Findings need checkable locations, and skipped input must not look reviewed. |
| Rules (deterministic) | Checks for silently swallowed exceptions, recognizable hard-coded secrets, debug prints, and Python logic changes without test-file changes. | Keep automatic blocking narrow and reproducible. |
| AI candidate stage (currently a stub) | Replays a fixed catalogue through the `LLMBackend` interface; it does not infer from the supplied diff. | Exercise the trust boundary without credentials, network calls, or nondeterministic output. |
| Trust gate (deterministic) | Rejects malformed candidates and unknown files, removes unverifiable line anchors, deduplicates near rule findings, caps AI severity at `major`, and limits its count. | Treat model output as untrusted suggestions, not merge authority. |
| Human review (optional) | Lets an engineer accept, reject, or edit each finding. | The engineer directs the outcome; uncertain suggestions do not block by themselves. |
| Verdict (deterministic policy) | Only a deterministic rule blocker can request changes. | A model suggestion cannot independently block a PR. |

`StubLLMBackend` deliberately includes malformed, ungrounded, and duplicate
candidates so the trust gate can be exercised. The deterministic rules do
inspect the diff; the stub does not perform real model inference.

## Run

From the repository root:

```console
# Run a non-interactive review of the sample diff
python -m review_agent examples/sample_diff.txt

# Review findings interactively and save your decisions
python -m review_agent --interactive examples/sample_diff.txt

# Summarize decisions saved in the current directory
python -m review_agent --stats
```

`ALL FINDINGS REJECTED` is interactive-only: reject every finding in
`--interactive` mode. It does not require a separate diff example.

The default command prints Markdown without prompting. Its exit code is a
simple CI gate, not a claim that a human reviewed the findings:

- `0` — no deterministic blocker requests changes. Advisory findings and
  `INCOMPLETE REVIEW` may still need attention.
- `1` — a deterministic rule blocker requests changes.
- `2` — invalid input, unreadable diff, or an incomplete interactive review.

### Verdicts

- `REQUEST CHANGES` — a deterministic blocker was found; exit code `1`.
- `INCOMPLETE REVIEW` — one or more files were skipped; check the report's
  `Not reviewed` disclosure. This takes precedence over advisory findings,
  but not over a deterministic blocker.
- `REVIEW RECOMMENDED` — an AI-stage major finding needs human verification.
- `NON-BLOCKING FINDINGS` — findings exist, but none is a blocker or an
  AI-stage major.
- `NO ISSUES FLAGGED` — no findings were produced; this is not proof of
  correctness.
- `ALL FINDINGS REJECTED` — the interactive reviewer rejected every finding.

Only `REQUEST CHANGES` returns exit code `1`; the other verdicts return `0`.
Input and interactive-review errors return `2`.

## Human review and feedback

Use `--interactive` in a terminal to see a few lines of diff context and
accept, reject, or edit each finding. Blockers default to accept so Enter
cannot silently drop one; other findings default to reject. Only accepted
findings appear in the final report. Interactive review requires a terminal
and reports an error if input ends before all findings are decided.

Each decision is appended to `decisions.jsonl` in the current directory. The
record includes the original and final finding and a UTC timestamp; code
snippets are omitted. The file is ignored by Git.

After an interactive review, `--stats` reads that decision log; it does not
run a new review. It reports acceptance rates by source (`rule` or `model`)
and category, plus the most rejected messages. For example, hypothetical
results might look like this:

**Acceptance by source**

| Key | Decisions | Accepted | Rate |
|---|---:|---:|---:|
| rule | 20 | 16 | 80% |
| model | 15 | 6 | 40% |

**Acceptance by category**

| Key | Decisions | Accepted | Rate |
|---|---:|---:|---:|
| tests | 10 | 3 | 30% |

That could prompt the team to revisit a low-value rule or investigate model
suggestions that reviewers often reject. These are human-feedback signals,
not objective accuracy measurements. Small or self-reviewed samples say
little, and the fixed stub cannot measure real model quality.

## Limitations and next steps

- The rules are small and Python-focused.
- The pipeline sees the diff, not the full repository or issue context. It
  does not run tests or post findings to GitHub.
- Grounding verifies file and line placement; it does not prove that a
  finding's reasoning is correct.

With more time, I would:

1. Evaluate findings against reviewed PRs and handle decision-log privacy and
   retention before using the data for tuning.
2. Add read-only repository context while preserving location grounding.
3. Connect a real model backend with structured output, timeouts, and explicit
   failure reporting; keep its findings advisory until evaluation supports
   more authority.
4. Run relevant checks and integrate the report with a GitHub check or review.
