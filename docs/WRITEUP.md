# Watchman: design tradeoffs and what went wrong

Watchman reviews code written by AI coding agents. Numbers below come from
`evals/results/RESULTS.md`; the README has the details.

## The design

A change is reviewed in layers. **Layer 1** is deterministic and cheap: secret scan, dependency
existence check against PyPI, ruff and mypy on the changed lines, test tampering, and the project's
tests in a sandbox. **Layer 2** is an LLM agent that sees Layer 1's results and *verifies
suspicions with tools* before reporting. Each finding must cite the tool calls behind it, and code
enforces that, not the prompt: a finding citing calls that never ran is dropped, and one citing
only the diff can't block a merge. **Layer 3**, an optional larger model (`--deep`), spot-checks
the agent. Everything the reviewer reads (diff, PR text, file contents, tool output, even the
agent's own prose) is untrusted data.

## Tradeoffs

- **Precision over recall.** A noisy reviewer gets ignored. Deterministic findings cover only lines
  the diff added, a network failure never produces a finding, and a failing test is re-run at the
  merge-base and not blamed if it already failed there. The cost is missed defects.
- **A cheap model, many guardrails.** On a shared 20-item sample Claude Haiku 5.5 found as many
  defects as Opus 4.6 and GPT-5 (88% each) at about 1/50 the cost, and flagged about a third as
  many problems on known-good commits. The structure around the model mattered more than its size.
  Some extra flags from the large models look like real upstream issues, so that gap measures noise,
  not necessarily wrongness.
- **Pessimistic scoring.** Findings match ground truth by file and line overlap, and findings on a
  defect item that match nothing count as false positives even when they might be real.
- **Mechanical, synthetic defects.** 13 hand-written, AST-guided templates inject AI-style defects
  into real commits, so no model grades itself on defects a model invented. The price is that they
  are plainer than real ones.

## What the evaluation showed

On the held-out split (19 seeded items, Haiku 5.5), deterministic checks alone found 21% of the
injected defects, one prompt found 74%, the agent without Layer 1 evidence 68%, and the full agent
84% (16 of 19). The dev split ordered them the same way (27%, 59%, 56%, 78%), and Layer 1 evidence
helped the agent on both. The agent beat the single prompt in direction only: on 19 items, 16 vs 14
defects is within noise. A full review costs a few cents.

The overseer did **not** pay for itself. On 107 dev items it rejected 2 of the 72 findings it
checked. Risk-triggered, it found no extra defects for 80% more cost. Always on, it found 5 more
(+7 points) for 2.4x the cost and flagged more on clean diffs. So it ships as an opt-in `--deep`
tool, and a held-out run of that design was cancelled rather than spent on a settled question.

## What went wrong

Most of the lessons are failures, and several were in the evaluation, not the product.

1. **Provider errors counted as clean reviews.** OpenRouter can return HTTP 200 with an error body
   ("A Timeout Occurred") and no choices. That became zero findings, so failed reviews looked like
   passes. It surfaced while investigating an implausibly weak Opus result. Now such responses are
   retried or raised, a review whose model stage fails is `incomplete` (never PASS, exit 2), and
   the harness excludes those items from every rate.
2. **The tuned threshold never ran.** Saving a config wrote every field, pinning the old
   `min_confidence = 0.5` over the tuned 0.6. Most runs, the whole test split included, used 0.5
   until every run was re-scored from its saved model output, and that re-score of the test split
   was logged as pre-registered. Configs now store only non-default values.
3. **The held-out split was touched more than once.** One full run, a retry of 5 items that hit
   provider errors, and a re-score. All are in an access log and nothing was tuned on test, but
   "run once" wasn't literally true.
4. **A filter destroyed real findings, twice.** Dropping a finding within two lines of a stronger
   one removed a SQL injection sitting under a prompt-injection comment, first across categories
   and again within one. It changed no dev metric,
   which is why metrics alone never caught it; a real run did. Findings now count as restatements
   only when their line ranges overlap.
5. **A trigger that measured the sandbox.** "A Layer 1 check failed" fired on 61% of reviews
   because the project's tests couldn't *install* in the sandbox (`pip install '.[test]'` exits 0
   for a missing extra, VCS-derived versions can't be computed in a container, dependency groups
   went unread). That made the risk-triggered overseer look better only because it ran on more
   items. The install is fixed, but the evaluation numbers predate it, so they understate Layer 1
   on real repositories.
6. **Smaller ones.** One injected bug failed 21 tests and produced 21 findings (now grouped per
   file; that fix followed a look at test results, so it is flagged as such). The first real agent
   run dropped every finding over an evidence-reference format mismatch. Models add junk keys to
   structured output that strict validation discarded along with the finding. A new account's 20
   requests per minute failed most of a parallel batch until retries honored the provider's reset
   time.

## Next

Re-run the evaluation now that the sandbox installs real projects' test dependencies; grow the
labeled set beyond injected defects; measure larger models only where the cheap one reports
nothing, the one place the overseer helped; and build the live mode that reviews a coding agent's
work at checkpoints, where the model-family blind-spot question can finally be tested.
