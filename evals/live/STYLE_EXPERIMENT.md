# Experiment 3: does Google-style supervision change what a worker writes?

Written and committed **before the style mode exists and before any run**. Nothing below is
changed after seeing results.

## Question

Experiment 2 asked whether live maintainability feedback (function size, complexity, docstrings,
copy-paste) changes the code. This asks the user's actual goal: does a Watchman configured with
`style = "google"` (the opt-in mode that checks the code against the
[Google Python Style Guide](https://google.github.io/styleguide/pyguide.html), citing a section
for each finding) leave behind code that conforms better to that guide, on open-ended work where
the worker makes every design choice?

## Setup

- Worker: `openrouter/anthropic/claude-haiku-5.5` in OpenCode. Arms: `alone`, and `watchman`
  (advisory only, the experiment-2 install, with `[review] style = "google"`).
- Tasks: the three vague tasks of experiment 2 (`vague_inventory`, `vague_notes`,
  `vague_gradebook`; one sentence each, plus "include tests"). The prompts say nothing about
  style, quality or the guide. 3 trials per task per arm, fresh runs for both arms (experiment 2's
  records are not reused). Spend cap: **$3**; a 1-task smoke run first.

## Grading (independent of the check where possible)

Computed on the code the worker added, production files only (tests excluded), after the run:

1. **Primary: pylint messages per run**, using Google's own published
   [`pylintrc`](https://google.github.io/styleguide/pylintrc) (vendored unchanged in
   `evals/live/google_pylintrc`, Apache-2.0), counted on added lines only. This is a different tool
   from the ruff and AST rules the check uses, written by the guide's authors. It measures naming,
   line length (80), unused code, quote consistency and similar. Lower is better.
2. Docstring coverage of the public API (§3.8.3): share of public functions and classes with a
   docstring.
3. Type-annotation coverage of public functions (§2.21): share of public functions with every
   parameter and the return annotated.
4. Functions over 40 lines (§3.18's advisory figure) and over 10 complexity, 5 parameters, 4 nesting.
5. Correct (the worker's own tests exist and pass), tests added, cost, seconds.

Analysis: paired by (task, trial); count pairs where supervised is lower / higher; exact sign
test on discordant pairs; arm means. Every completed run is reported; errored runs are counted and
excluded. 9 pairs can only show a large effect, so read the counts.

## Hypotheses

- H1: supervised runs have fewer pylint messages (Google rc) than unsupervised runs.
- H2: supervised runs have higher docstring and annotation coverage and fewer functions over 40
  lines, with no loss of correctness.
- If unsupervised runs already conform (near-zero messages), the result is "ceiling" and is
  reported as such.

## Known limits, stated up front

- **Partial circularity.** Items 2 to 4 measure rules the check enforces, so an improvement there
  shows that the worker acts on Watchman's feedback. Only the pylint count is measured by an
  independent tool, and even it overlaps with the check on naming. Neither measures elegance or
  whether the design is good; only a human reading the code does.
- Passing the guide's lints is not the same as readable, well-structured code. The guide itself says
  to optimize for readability and consistency; this experiment cannot measure that.
- One worker model, one reviewer model, three tasks, three trials. Small n: counts, not significance.
- The style mode and its dev-split false-positive measurement are developed before this run, on the
  dev split of the diff-review datasets only; the vague tasks are not used for tuning.

## Amendment before the full run (after a one-task smoke run, results not used)

A smoke run (`vague_notes`, one trial per arm; its records are replaced by the full run's) exposed
two things in the style mode, both fixed before the full run:

- A false positive: `__main__.py` was flagged as a badly named module. Fixed.
- pylint's most frequent message on worker code was `line-too-long` (Google's 80 columns, §3.2), which the
  mode did not check. A line-length rule (80 columns, not for imports, URLs or one-token lines; one aggregated finding per file) was added.
  This makes the check overlap with part of the primary pylint metric, so the independent-tool argument
  is weaker for that one message. The report lists pylint messages by symbol so this is visible.

Hypotheses, tasks, trial counts and the analysis plan are unchanged.
