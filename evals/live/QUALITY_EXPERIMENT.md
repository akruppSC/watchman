# Experiment 2: does live supervision improve the maintainability of what a worker writes?

Written and committed **before any of these runs**. Nothing below is changed after seeing results.

## Question

Experiment 1 (defects) hit a ceiling: Haiku made no mistakes, so Watchman had nothing to fix.
This one asks a different question. When the work leaves room for structural choices, does a
Watchman that gives live **maintainability** feedback (the Layer-1 `quality` check, delivered at
test runs, commits and "done") change the code the worker leaves behind?

## Setup

- Worker: `openrouter/anthropic/claude-haiku-5.5` in OpenCode. Arms: `alone`, and `watchman`
  (advisory only, the same install as experiment 1, default config; the `quality` check is on).
- 3 trials per task per arm, same random run order scheme as experiment 1. Spend cap: **$3**.
  A smoke run (1 task, both arms, 1 trial) comes first to check the harness; its records are
  replaced by the full run's.

**2A, specified tasks** (`suite = "quality"`): `shipping_cost`, `command_interpreter`,
`invoice_rules`. Each has a detailed rule list and a hidden spec for correctness. The natural
first implementation is one long function with many branches; a reference solution that splits
it into small functions and stays inside every limit exists and is tested (it proves the limits
are reachable). The prompts say nothing about structure or quality.

**2B, vague tasks** (`suite = "vague"`): `vague_inventory`, `vague_notes`, `vague_gradebook`.
One sentence each ("Build a small inventory tracker for a shop..."), plus "include tests", run
the tests, commit. No hidden spec and no reference: the worker chooses the design. The prompts
say nothing about quality or standards (a test enforces this). "Correct" only means the worker's
own tests exist and pass.

## Measurements (all objective, model-free; `evals/quality.py`)

On the functions and classes the worker added or edited:

- **`breaches` (primary)**: functions over any limit: cyclomatic complexity > 10, > 60 lines,
  > 5 parameters, nesting > 4.
- Secondary: `public_undocumented / public_defs` (docstring coverage of the public API), tests
  added, production lines, maintainability lint findings (a ruff rule set separate from the
  reviewer's), copy-pasted 5-line windows, mean complexity, longest function.
- 2A only: hidden-spec correctness. Both suites: cost and seconds per run.

Analysis: paired by (task, trial); count pairs where supervised is lower / higher; exact sign
test on discordant pairs; means per arm. Every completed run is reported; errored runs are
counted and excluded. With 9 pairs per suite, only a large effect can reach significance, so
read the counts.

## Hypotheses

- H1: in 2A, supervised runs have fewer `breaches` than unsupervised runs, with no loss of
  hidden-spec correctness.
- H2: in 2B, supervised runs have fewer `breaches` and a smaller undocumented share of the
  public API.
- If unsupervised runs produce zero breaches (and documented APIs) already, the result is
  "ceiling again" and is reported as such.

## Known limits, stated up front

- **Circularity.** The reviewer's `quality` check uses the same four limits as the primary
  metric. A drop in `breaches` shows that the worker acts on Watchman's maintainability
  feedback. It is not independent proof that the code is better. The secondary metrics
  (docstring coverage, lint set, duplication, tests added) overlap with the check only partly.
- No claim about compliance with any IEEE or ISO standard is made or testable here. The
  measures are proxies for maintainability sub-characteristics (ISO/IEC 25010).
- One worker model, one reviewer model, 3 trials, 6 tasks. The tasks and the metrics were
  written by the same people who wrote the check.
- The pitfall suite results from experiment 1 are not re-opened.
