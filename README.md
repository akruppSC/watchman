# Watchman

Watchman is an AI reviewer for code written by AI coding agents. It checks that a change is
high quality, avoids the pitfalls typical of AI-written code, follows the repo's own
conventions, and is ready for a senior developer's PR review.

It is **tool-using and verifying**. A cheap deterministic layer runs first (lint, types,
tests, secret scan, dependency-existence check). After that, an LLM reviewer agent
*verifies* its suspicions with tools before reporting them. Every finding must cite
evidence, and findings that can't be grounded are dropped.

> **Status:** the CLI, the tool-using agent, the evaluation, live mode, a maintainability check and an
> opt-in Google-style mode, and the integrations (a GitHub
> Action that comments on pull requests, and a git pre-push hook) are complete. The overseer
> (a larger model for a deeper second look) is an opt-in `--deep` tool; automatic use didn't
> pay for itself in evaluation. See [Roadmap](#roadmap) and the
> [design writeup](docs/WRITEUP.md).

**See it in 60 seconds:** `asciinema play demo/watchman.cast` (a 50 second recording), or run
`bash demo/demo.sh` yourself. It builds a throwaway project, has an "AI agent" add a hallucinated
import, a leaked token, a bare `except`, and a skipped test, and shows Watchman catching them
and a pre-push hook rejecting the push. The deterministic part needs no API key.

## Goals

Watchman has three main goals:

1. **Catch what AI-written code gets wrong**: hallucinated dependencies, swallowed errors, weakened
   tests, copied helpers, scope creep, invented APIs.
2. **Be verifiable**: every finding cites the tool calls behind it, and the whole thing is measured on
   a frozen evaluation, not on impressions.
3. **Keep AI-written code readable and maintainable.** As more code is written by AI agents, it
   should still read like code a senior developer would accept. That has two parts. The first is
   *quantified*: small, focused functions with **low cyclomatic complexity** (10 or less), shallow
   nesting, few parameters, documented public interfaces, and no copy-paste. The second is a style
   guide, the [Google Python Style Guide](https://google.github.io/styleguide/pyguide.html), plus the
   repo's own conventions (naming, error handling, structure). Watchman works on both live, with the
   worker, as it writes: see [Code quality and style](#code-quality-and-style).

Goal 3 is the hardest to measure, and the sections below say exactly what is and isn't shown.

## Install

Requires Python 3.12+, git, and [uv](https://docs.astral.sh/uv/).

```bash
git clone https://github.com/akruppSC/watchman && cd watchman
uv sync --all-extras
uv run watchman --help
```

Optional but recommended: Docker, used as the sandbox for running the reviewed repo's
tests. Without Docker, Watchman falls back to a local subprocess and cuts off its network
access with `unshare` where the kernel allows it.

Check the install by reviewing any branch of any git repository. The deterministic layer is
free and needs no API key:

```bash
cd /path/to/some/repo      # a repository with a branch to review
uv run --project /path/to/watchman watchman review --base main --head HEAD
```

## Quick start

```bash
# One-time: choose provider, model, key source, and budgets (only needed for LLM modes).
uv run watchman setup

# Review the current branch against main (deterministic layer only, no LLM, free):
uv run watchman review --base main --head HEAD

# Machine-readable output, and a CI gate that fails on blockers:
uv run watchman review --base main --head HEAD --json -o review.json --fail-on block

# Uncommitted work in progress:
uv run watchman review --base main --worktree

# Full review: deterministic layer + tool-using agent (uses your configured model):
uv run watchman review --base main --mode agent --task "Add retry to the HTTP client"

# Try a different model for one run:
uv run watchman review --base main --mode agent --model anthropic/claude-haiku-5.5

# A deeper second look from the larger overseer model, for a change that deserves it:
uv run watchman review --base main --mode agent --deep

# Single-prompt LLM baseline (the ablation control arm):
uv run watchman review --base main --mode baseline --task "Add retry to the HTTP client"
```

| Mode | What runs | Cost |
|---|---|---|
| `deterministic` (default) | Layer 1 checks only | free |
| `agent` | Layer 1, then the agent, which sees Layer 1 results as evidence | LLM |
| `agent_no_l1` | The agent alone (ablation arm) | LLM |
| `baseline` | One prompt over the diff: no tools, no Layer 1 (ablation arm) | LLM |

To make `agent` your default, set `default_mode = "agent"` under `[review]` in
`~/.config/watchman/config.toml`. A repo's `.watchman.toml` can't change this.

Exit codes: `0` = verdict below the `--fail-on` threshold, `1` = gate tripped, `2` = error.

Other commands: `watchman schema` (JSON Schema of the output), `watchman budget` (spend this
month against the cap), `watchman config show`.

### Configuration

`watchman setup` writes `~/.config/watchman/config.toml` with mode `600`. It records the
provider (OpenRouter, Anthropic, OpenAI, or Ollama), the model, the *name* of the env var
holding the API key (or a note that the key is in the OS keyring), pricing, and budgets.
**API key values are never written to any file.**

The keyring option is only offered when a real OS keyring backend exists (Secret Service /
GNOME Keyring, macOS Keychain, Windows Credential Locker). On headless or minimal Linux
installs there usually isn't one. In that case setup tells you to
`export OPENROUTER_API_KEY=...` (or the env var for your provider) instead. Watchman
deliberately does not fall back to `keyrings.alt`, which stores keys in plaintext.

A repo can add a `.watchman.toml` to tune review options:

```toml
[review]
min_confidence = 0.6
test_command = ["{python}", "-m", "pytest", "-q", "-m", "not network"]
[budget]
max_tool_calls = 15        # repo config may only *lower* budgets
```

## Use it in CI (GitHub Action)

Watchman reviews every pull request and posts **one comment that it updates in place** on each
push. Add `.github/workflows/watchman.yml` (also in [`examples/workflows`](examples/workflows)):

```yaml
name: Watchman
on: pull_request            # not pull_request_target (see below)
permissions:
  contents: read
  pull-requests: write
jobs:
  review:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v7
        with:
          fetch-depth: 0    # Watchman needs history to find the merge base
      - uses: akruppSC/watchman@main   # pin a tag or commit
        with:
          mode: agent
          model: google/gemini-3.7-flash
          api-key: ${{ secrets.OPENROUTER_API_KEY }}
```

With no `api-key` and the default `mode: deterministic`, it runs the free Layer 1 checks only.

| Input | Default | |
|---|---|---|
| `mode` | `deterministic` | `deterministic`, `agent`, `baseline`, `agent_no_l1` |
| `model`, `provider` | config / `openrouter` | LLM modes only |
| `api-key` | | From a secret. Not needed for `deterministic` |
| `checks` | all | Comma-separated Layer 1 checks, e.g. skip `pytest` if CI already runs it |
| `deep` | `false` | Also run the overseer (a larger model). Costs more |
| `fail-on` | `block` | Fail the job at `block`, `warn`, or `never` |
| `strict` | `false` | Also fail the job if the review is incomplete (a model call failed) |
| `comment` | `true` | Post or update the PR comment |

Outputs: `verdict`, `findings`, `incomplete`, `cost-usd`, `json-path`, `markdown-path`,
`comment-url`. The result is also written to the job summary.

How it handles the awkward cases:

- **Fork PRs** have no secrets and a read-only token. Watchman falls back to the free Layer 1
  checks, notes why in the comment, and if it can't comment it writes the job summary instead of
  failing the run.
- **The PR is untrusted input.** Use the `pull_request` event only. `pull_request_target` would
  hand a secret-bearing token to code from the PR. The PR's title and body become the review's
  task text and are wrapped as untrusted data. Everything a model wrote is sanitized before it
  goes into a public comment: HTML is escaped, `@mentions` and `#123` references can't ping or
  link, markdown links and bare URLs are made inert, and the comment marker can't be forged.
- **The comment is found by marker and by bot authorship**, so a person who pastes the marker
  into their own comment can't get Watchman to edit it.
- **Action inputs reach the shell only as environment variables**, never interpolated into
  script text.
- **A model failure doesn't look like a pass.** An incomplete review says so in the comment,
  and fails the job only with `strict: true`.

The repository's own [`watchman.yml`](.github/workflows/watchman.yml) is the action reviewing
Watchman's pull requests. The workflow files are linted with
[actionlint](https://github.com/rhysd/actionlint), and `tests/test_ci.py` exercises the comment
logic against a mocked GitHub API. The action itself has not yet run on GitHub: check the first
run's logs before relying on it.

## Use it as a git pre-push hook

```bash
uv run watchman hook install                 # fast checks, no LLM, no API key
uv run watchman hook install --mode agent    # also run the LLM reviewer (costs a little)
uv run watchman hook install --with-tests    # also run the project's tests (slow)
uv run watchman hook status
uv run watchman hook uninstall
```

On `git push`, the hook reviews the commits being pushed (against the remote branch if it
exists, otherwise against `origin/main`) and **blocks the push only when the verdict is BLOCK**.
Tool errors, an incomplete review, tags, and branch deletions never block. Skip it once with
`git push --no-verify` or `WATCHMAN_SKIP=1 git push`. It respects `core.hooksPath` and
won't replace someone else's pre-push hook without `--force` (the old one is kept and restored on
uninstall).

## Live mode: supervise a coding agent as it works

Instead of reviewing a finished diff, Watchman can watch a coding agent in the same repository
and feed terse findings back at **checkpoints**: after the worker runs its tests, when it makes a
commit, and when it says it is done. It never reviews half-written files, never edits your source,
and by default only advises.

```bash
uv run watchman live install-opencode --repo /path/to/project   # OpenCode plugin
uv run watchman live install-opencode --policy blocking --mcp   # + blocking gate, + MCP tool
uv run watchman live status --repo /path/to/project             # rounds and open findings
uv run watchman live reset --repo /path/to/project              # start the loop over
uv run watchman live uninstall-opencode
```

What the worker sees is a few lines, not a report:

```
WATCHMAN commit round 2/3 verdict=WARN gate=pass new=1 open=1 fixed=1
+ minor scope_creep demo/__pycache__/config.pyc Compiled bytecode committed | fix: remove it and ignore *.pyc
= major swallowed_error demo/config.py:8 (open since r1) E722: Do not use bare `except` [check:ruff]
```

**How it works**

- **Snapshots, not live files.** Each review runs on a commit object built with git plumbing and a
  throwaway index (`live/snapshot.py`). The worker's index, refs, branch, and working tree are never
  touched, and the same tree always yields the same snapshot, so "nothing changed since the last
  review" costs nothing. Untracked files and deletions are included; `.gitignore` is honored.
- **One review/fix episode per checkpoint kind.** `test_run` runs only the cheap deterministic checks
  (the worker just ran the tests), `commit` and `done` run the full agent. Rounds count only while
  there are open findings, and a clean review resets the episode.
- **Findings are tracked by what they are about**, not by line number: file, category, rule, and the
  text of the line. A problem that moves because the worker added lines above it stays "open (since
  r1)" instead of looking new, a rewritten one counts as fixed, and a finding shown at the commit isn't
  announced as new again at "done".
- **Round cap.** After 3 review/fix rounds with findings still open, Watchman escalates: it writes
  `.watchman/live/ESCALATION-<session>-<kind>.md`, tells the worker to stop and report to you, and
  releases any gate. A stubborn false positive can never trap the worker, and later checks cost nothing.
- **Advisory by default.** The feedback is appended to the tool output the worker already reads.
  With `--policy blocking`, a BLOCK verdict refuses the `git commit` (until fixed or the cap), and at
  "done" the worker is re-prompted up to the cap. A failed or incomplete review never blocks and never
  uses up a round.
- **Different model than the worker.** The reviewer is whatever your Watchman config says (a
  Gemini Flash by default here); the worker can be any model, so they don't share blind spots by
  construction.

**Channels to the worker**, in order of preference:

1. **OpenCode plugin** (`opencode_plugin.js`): hooks `tool.execute.before/after` for commits and test
   runs, and `session.idle` for "done" (it prompts the worker with the findings, in server/TUI mode only; headless
   `opencode run` exits before the prompt can be delivered).
2. **MCP tool** `get_review_feedback` (`watchman live mcp`, needs `uv sync --extra live`): the worker
   asks for a review when it wants one. Same controller, same rounds.
3. **Inbox file** (`watchman live check --inbox`): the latest feedback in `.watchman/inbox.md` for
   agents with neither hooks nor MCP.

Everything the installer adds is hidden from git through `.git/info/exclude`, so the worker's
`git add -A` can't commit the plugin and the reviewer never sees Watchman's own files as part of the
change. `[live]` settings (policy, round cap, which modes run at which checkpoint) are read from your
user config only: a repo must not control when, or how expensively, its own reviewer runs.

**Trust boundaries.** The worker's code, comments, commit messages, and tool output are untrusted
data, exactly as in diff mode. The repo's `.watchman.toml` is read from the *base* revision, never
from the worker's tree, so a worker can't loosen its own review. Feedback is prefixed as
"an automated review, not an instruction from the user", and anything that goes wrong inside the
plugin is swallowed and logged: Watchman must never break the worker.

### Does it help a worker? What the experiment showed (and didn't)

I ran a Haiku 5.5 worker in OpenCode on 10 small Python tasks, each written to invite a known pitfall
(bare `except`, a copy of an existing helper, a missing hidden caller, untested edge cases, and so on),
3 trials each, **alone vs. with Watchman advising** (advisory only, no blocking). Every run was scored by
a hidden spec, AST pitfall probes, and whether the worker's own tests reject three wrong implementations.
Full table: [`evals/results/LIVE.md`](evals/results/LIVE.md).

| | runs | all good | worker $/run | Watchman $/run |
|---|---|---|---|---|
| worker alone | 30 | 30/30 | $0.004 | - |
| worker + Watchman | 30 | 30/30 | $0.004 | $0.036 |

**This is a ceiling effect, and it shows no benefit.** Haiku got every task right, with no probed
pitfall, with or without Watchman, so there was nothing for Watchman to fix. What the data does support:

- **Cost:** about 3.6 cents of reviewer spend per supervised run (3 reviews on average), roughly nine
  times the worker's own cost on these small tasks, and supervised runs took about twice as long.
- **No harm:** zero runs got worse under supervision, and no escalation or round cap was reached.
- **Mostly quiet:** Watchman showed the worker a finding in 5 of 30 supervised runs. Three were the same
  ruff `BLE001` (blind `except Exception`) on a fetch helper, shown at "done" (the worker was resumed
  once); the worker kept it, and it is a style-level warning, not a bug the probes counted. In the other
  two (`api_key`), a finding raised at one test run was gone by the next, but the worker's code was
  correct without Watchman too, so this can't be called a catch. The harness didn't save the finding
  text, so I can't say what it was; it should have.

What it does **not** show: that Watchman improves a worker, or that it would not help a weaker one.
The 8 planned tasks hit the ceiling first; I then added two harder ones (`ticket_summary`,
`invoice_prices`, marked † in the table), after seeing that. They also hit it, so they are reported
separately in the by-task table and not used as a headline. With 30 runs per arm and no differences,
this can only rule out large effects on tasks like these; for per-run rates read the Wilson intervals
(100% observed, lower bound 89%), not the percentages. The reference solutions and mutants were written by
the same author as the probes, so the scoring is not independent either.

The earlier, stronger evidence is the diff-review evaluation below (seeded defects, not a live worker).
The supervision loop itself was verified end to end against real OpenCode sessions: a worker received
test-run and commit feedback, fixed a committed `__pycache__`, and (in server mode) answered the
done-prompt.

**Live-mode limitations**

- **The "done" prompt only works in server/TUI mode.** In headless `opencode run`, the process exits at
  `session.idle`, so a plugin's done-prompt is lost. The experiment harness plays that step itself (it runs
  `watchman live check --kind done` and resumes the session once). Test-run and commit feedback work in
  both modes.
- **OpenCode only.** The plugin hooks are OpenCode's. Other agents can use the MCP tool or the inbox file,
  which are pull-based: the worker has to ask.
- **Advisory runs were exercised far more than blocking.** The blocking gate is covered by unit and plugin
  tests and a few manual sessions, not by the experiment.
- **Worker cost isn't in Watchman's ledger.** Reviewer spend is; the supervised worker's own spend is
  billed to whatever runs it.
- **The overseer was not shown to help** (see [The overseer](#the-overseer)). Live mode does not use it;
  `--deep` stays an opt-in tool.

## Code quality and style

Watchman measures code quality in two ways. The first is a set of **quantified limits that don't depend on
any style guide**, with complexity first among them: they were the original, measurable definition of
"good code" for this project. The second is the **Google Python Style Guide**, an opt-in mode that checks the
rules a program can decide. Both run in live mode, so the worker hears about a problem while it is cheap to
fix.

### Quantified maintainability: complexity, size, nesting, parameters, duplication

The `quality` check (`checks/quality.py`, on by default) measures the code a change touches against fixed
limits. A finding is raised only when *this change* creates the problem (a new function over a limit, or an
existing one the change pushes across it), never for debt that was already there: adding one branch to a
function that was already too complex does not make it too complex. The limits are fixed in code, so a
reviewed repo can't loosen its own review.

| measure | how it is computed | limit | why |
|---|---|---|---|
| **Cyclomatic complexity** | McCabe: 1 + decisions (`if`/`elif`, loops, `except`, ternaries, `and`/`or`, comprehension filters, `match` cases) per function | **10** | McCabe's proposed limit (discussed in NIST SP 500-235): beyond it a function has too many paths to test or hold in your head |
| Function length | lines from `def` to the last line | 60 (**40** in Google mode) | Google's guide §3.18 hints at "about 40 lines" |
| Parameters | positional and keyword parameters, excluding `self`/`cls` | 5 | ruff's default (`PLR0913`) |
| Nesting | deepest block depth in a function (`elif` is not deeper than its `if`) | 4 | common practice; deep nesting hides the main path |
| Documentation | public functions and classes added without a docstring, where the module documents its API; new modules whose public API is mostly undocumented | n/a | readers need the contract |
| Copy-paste | six or more added lines that already exist elsewhere in production code | 6 lines | reuse over duplication |

In live mode the worker sees, for example,

```
+ minor maintainability app/cli.py:81 `_dispatch` has cyclomatic complexity 11 (limit 10) | fix: split it into smaller functions (one per branch of the logic), or replace the if/elif chain with a lookup table
```

On the dev split of the evaluation (real commits from attrs, click, httpx and more-itertools) the check fires
about 0.07 times per clean commit and caught 1 of the 41 seeded defects (an injected copied helper): it is a
maintainability guard, not a defect finder. That was measured on dev only, at $0.

**Did supervision lower complexity?** Complexity is the cleanest of these measures, and the most consistent
effect I found. Maximum cyclomatic complexity in the code a Haiku 5.5 worker wrote, and functions over the
limit of 10, alone vs. supervised (the experiments are described below):

| experiment | max complexity per run | functions over 10 per run | pairs where supervised was lower / higher (functions over 10) |
|---|---|---|---|
| 1: defect tasks (54 runs; Haiku's code was already simple) | 3.2 → 3.5 | 0.00 → 0.00 | 0 / 0 (a ceiling: nothing to fix) |
| **2A: specified tasks that invite long functions** | **17.7 → 11.2** | **0.67 → 0.22** | 4 / 0 (p = 0.12) |
| **2B: vague one-line tasks** | **8.6 → 5.3** | **0.33 → 0.00** | 3 / 0 (p = 0.25) |
| 3: same vague tasks, Google mode | 7.8 → 7.2 | 0.33 → 0.11 | 2 / 0 (p = 0.50) |

Case by case, the effect is large where the first draft was bad. On `vague_notes` (experiment 2B) all three
unsupervised runs ended with a function of complexity 15 to 18, and all three supervised runs with 5 to 6
after Watchman flagged it. On `invoice_rules` the unsupervised runs ended at 32 to 33 and two of three
supervised runs at 9; on `shipping_cost` unsupervised 14 to 16 and supervised 7, 8 and 16. **It did not
replicate that strongly:** on the same `vague_notes` task in experiment 3 the supervised runs ended at 10, 15
and 7 against 14 to 15 unsupervised, and the mean maximum barely moved (7.8 → 7.2, with 4 pairs higher and 2
lower). In experiment 2A, all three supervised runs that still ended over a limit had been shown a finding
for it and did not fix it.

What this does and doesn't show. Complexity went down in the direction the goal wants in every experiment
where there was something to reduce, and the clearest cases are large. No comparison is statistically
significant at the registered per-suite level (nine pairs each), the mean maximum is not monotone across
experiments (2B's pairs were mixed, 3 lower and 4 higher, because already-simple runs wobbled by a point or
two), and the metric uses the same limit as the check, so it shows that **the worker acts on the complexity
feedback**, not that complexity independently proves better code. Splitting a function also costs something:
supervised runs wrote about 10% more production code on 2A, and the most parameters in a function rose from
2.6 to 3.2 as logic moved into helpers. Full tables are in
[`evals/results/QUALITY.md`](evals/results/QUALITY.md).

### The Google Python Style Guide

**The reference is a style guide, not a standard.** A style guide is something code can be checked
against, which is why Watchman uses the
[Google Python Style Guide](https://google.github.io/styleguide/pyguide.html) as its yardstick
for "readable by a senior developer". Watchman does not certify compliance with it, or with any
standard: it flags concrete departures it can detect reliably, tells the worker while they are cheap
to fix, and leaves the rest (formatting, taste, whether the design is elegant) to a formatter and to
human review. What it covers today, by section of the guide:

| Google Python Style Guide | What Watchman does today | Where |
|---|---|---|
| §3.18 Function length: "prefer small and focused functions" (about 40 lines) | Flags a function that *this change* pushes past 60 lines (**40 in Google mode**), 10 cyclomatic complexity, 5 parameters, or 4 levels of nesting, and suggests how to split it. The guide gives only the 40-line hint; the other limits come from general practice (McCabe's 10, ruff's defaults) | `quality` check |
| §3.8 Comments and docstrings | Flags a new public function/class without a docstring where the module documents its API, and a new module whose public API is mostly undocumented. **Google mode** adds: a module docstring on new modules, a one-line summary ending in punctuation, and `Args:`/`Returns:`/`Yields:` sections on multi-line docstrings of new public functions | `quality`, `google_style` |
| §3.16 Naming | **Google mode:** CapWords classes; lower_with_under functions, arguments, variables and module files (camelCase and PascalCase functions are flagged; PascalCase variables are not, since they are usually class aliases) | `google_style` |
| §2.21 Type annotations | **Google mode:** new public functions without full annotations (not if a `.pyi` stub sits next to the module) | `google_style` |
| §3.2 Line length | **Google mode:** one finding per file listing lines over 80 columns (not for imports, URLs or one-token lines). Other formatting (indentation, whitespace, blank lines) is left to a formatter (Black, Pyink or `ruff format`) | `google_style` |
| §2.2 Imports, §2.7 comprehensions, §2.17 decorators, §2.5 globals, §2.14 truthiness, §3.10.1 logging, §3.12 TODOs, §3.17 main | **Google mode:** relative imports; comprehensions with several `for` clauses or filters; `@staticmethod`; `global`; `len(x) == 0`, `== None`, `== True`; f-strings in logging calls; TODOs without a context link; an unguarded `main()` call | `google_style` |
| §2.4 Exceptions | Bare and blind `except`, `except: pass`, `raise` without `from` (ruff and the agent); **Google mode** adds `try` bodies over 30 lines, and the agent is told to judge `assert` for validation | `ruff`, `google_style`, agent |
| §2.12 Default argument values | Mutable and call-expression defaults | `ruff` |
| §2.1 Lint | Runs ruff and mypy on the changed lines (not pylint) | `ruff`, `mypy` |
| Reuse over copy-paste, project fit, readability | Flags added blocks that already exist elsewhere; **Google mode** hands the agent a style summary (`prompts/style_google_python.md`) so it judges the rest (naming quality, power features, small `try` bodies, whether the change fits the existing structure). The repo's own `CONVENTIONS.md` still wins | `quality`, agent |
| §2.2 "import modules, not names" | Not enforced: it contradicts how most Python projects are written, so it is left to the repo's conventions | n/a |

**Google mode is opt-in.** It is off by default, because other projects follow other styles. Turn it on in
your user config (a reviewed repository cannot turn it on or off for itself):

```toml
# ~/.config/watchman/config.toml
[review]
style = "google"
```

Every finding it raises cites its guide section, for example
`Argument 'camelCase' is not lower_with_under (Google Python Style Guide §3.16)`. In live mode the
worker sees these at test runs, commits and "done". It is a best-effort detector of departures from the
guide, not a certification of compliance.

### Does it improve the code?

Three pre-registered experiments, each stated with its limits (complexity results are broken out above):

- *Experiment 1 (above), re-measured on code quality:* no difference. Haiku's code was already small
  and simple, so there was nothing to improve (`evals/results/QUALITY.md`).
- *Experiment 2* (pre-registered in [`evals/live/QUALITY_EXPERIMENT.md`](evals/live/QUALITY_EXPERIMENT.md)
  before any run; full tables in [`evals/results/QUALITY.md`](evals/results/QUALITY.md)): 36 runs, 9 per
  arm and suite. **2A** has three specified tasks that invite one long, branchy function; **2B** has three
  deliberately vague one-line tasks ("Build a small inventory tracker for a shop"). Haiku 5.5 worker, alone
  vs. supervised (advisory), cost about $1.26 in total.

  | functions over a limit (per run) | alone | supervised | pairs where supervised was lower / higher |
  |---|---|---|---|
  | 2A, specified tasks | 0.67 | 0.33 | 3 / 0 (sign test p = 0.25) |
  | 2B, vague tasks | 0.33 | 0.00 | 3 / 0 (p = 0.25) |
  | 2B, public API without a docstring | 7.9 | 5.3 | 6 / 1 (p = 0.12) |
  | 2A, longest function (lines) | 46.7 | 30.2 | 7 / 1 (p = 0.07) |

  **Every comparison points the same way, and none is significant** at the registered per-suite level
  (9 pairs each; the pre-registered plan did not pool suites). Pooling the primary metric across both
  suites after the fact gives 6 pairs lower and 0 higher (p = 0.03), which I report as exploratory only.
  The clearest case is `vague_notes`: all three unsupervised runs ended with a `main` of complexity 15 to
  18, all three supervised runs with complexity 5 to 6, after Watchman flagged it. On `shipping_cost`
  Watchman flagged the oversized function in all three runs and the worker fixed it in two. It costs
  something: supervised runs wrote about 10% more production code on 2A (8 pairs longer, 1 shorter,
  p = 0.04), because splitting a function adds signatures and docstrings; with 14 metrics compared per
  suite, one p below 0.05 is expected by chance, so treat that as suggestive.

  Cost: about $0.05 of reviewer spend per supervised run (4 reviews on average), against about $0.009 of
  worker spend.

  **What this does not show.**
  - *Not independent proof of better code.* The metric uses the same limits as the check (stated in the
    pre-registration), so it shows that the worker acts on the feedback.
  - *Correctness is not reliably measured on two of the three specified tasks.* On `shipping_cost` all six
    runs failed the same hidden assertion, which tests behavior for an unknown `tier` that the prompt
    never specifies (the worker raised `ValueError`); without that one line all six runs pass. The
    `invoice_rules` hidden spec likewise asserts a few cases the prompt leaves open (for example a boolean
    `tax_rate`), and its saved diffs were truncated, so I couldn't re-check which failed. I left the
    pre-registered tasks unchanged and don't claim anything about correctness there. On
    `command_interpreter` and the vague tasks the results are clean: 3/3 and 9/9, equal in both arms.
  - *Small n:* nine pairs per suite, one worker model, one reviewer model, three tasks per suite.

- *Experiment 3* (Google mode, pre-registered in
  [`evals/live/STYLE_EXPERIMENT.md`](evals/live/STYLE_EXPERIMENT.md); tables in
  [`evals/results/STYLE.md`](evals/results/STYLE.md)): the three vague tasks again, 18 runs, alone vs.
  supervised with `style = "google"`, graded with Google's own published `pylintrc` run through pylint, a
  tool independent of Watchman's check. About $0.68 in total.

  | per run | alone | supervised | pairs, supervised lower / higher |
  |---|---|---|---|
  | pylint messages (**primary**) | 3.33 | 1.89 | 5 / 1 (sign test p = 0.22) |
  | pylint messages per 100 added lines (secondary) | 2.02 | 0.76 | 8 / 1 (p = 0.04) |
  | public API with a docstring | 42% | 73% | 1 / 7 (p = 0.07) |
  | public functions fully annotated | 100% | 100% | 0 / 0 |

  **The pre-registered primary metric did not reach significance** (nine pairs). The improvement came
  from two message types Watchman explicitly checks: `line-too-long` fell from 19 to 10 and
  `missing-class-docstring` from 5 to 0, while `invalid-name` (3 vs 3) and `missing-module-docstring`
  (3 vs 3) did not move. So the worker acts on the style feedback it is given, which is partly circular
  (and was made more so by adding the line-length rule after a smoke run; the pre-registration records
  that). Annotations were already complete without help (a ceiling). Own tests passed in all 18 runs, and
  supervised runs wrote about 19% more code (199 vs 168 lines) and 18% more tests. Cost: about $0.054 of
  reviewer spend per supervised run.

**Across experiments 2 and 3, nearly every metric moved in the supervised direction and none was significant
on its registered primary comparison.** Nothing here measures elegance or design quality; only a human
reading the code can judge those.

## How it works

```
git diff (merge-base..head) ──► snapshot worktree of head
                                  │
          Layer 1: deterministic checks (cheap, high precision)
          secrets · imports · hygiene · scope · ruff · mypy · pytest(sandbox)
                                  │  results become evidence for ↓
          Layer 2: tool-using agent: plan → verify with tools → submit_findings
                                  │  only with --deep (or `auto = true` in config)
          Layer 3: overseer, a larger model: spot-check → dig deep → brief the agent
                                  │  if it rejected/added many findings: one more agent pass
          post-process: grounding → confidence threshold → merge/dedupe → verdict
                                  │
                    terminal report / JSON (ReviewResult schema)
```

| Check | What it catches | Notes |
|---|---|---|
| `secrets` | Provider keys, private keys, URL credentials, high-entropy assignments | Values are redacted in output |
| `imports` | Hallucinated packages, undeclared deps, likely slopsquatting | Checks stdlib, repo modules, manifests and lockfiles, the repo venv, then PyPI |
| `hygiene` | Skipped or deleted tests, removed assertions, unjustified `noqa`/`type: ignore` | Uses the tokenizer, so strings never count as comments |
| `scope` | Oversized diffs, CI/build config changes | |
| `ruff` | Swallowed errors, mutable defaults, `shell=True`, `eval`, SQL building, and more | Fixed pitfall rule set plus the repo's own config |
| `mypy` | Hallucinated APIs (`json.parse_file`), wrong call signatures | Small allowlist of error codes |
| `pytest` | Tests broken by the change | Failures are re-run at the merge-base and not blamed if they already failed there |

All deterministic findings are limited to lines **added by the diff**, so pre-existing debt
never shows up as noise.

### The agent

The agent receives the task, the diff, and the Layer 1 results, and works through a
**plan → verify → submit** loop with these tools:

| Tool | Notes |
|---|---|
| `get_diff(path?)` | The change, whole or per file |
| `read_file(path, start, end)` | Head snapshot only, line-numbered, ≤ 400 lines per call |
| `search_repo(pattern, regex?, path_glob?)` | Find callers, existing helpers, duplicates |
| `git_log(path?)`, `git_blame(path, start, end)` | History and intent |
| `run_tests(selectors)` | Sandboxed, no network; flags are rejected (no option injection) |
| `run_linter(paths)` | ruff, all lines |
| `check_package_exists(name)` | PyPI; the only network access a tool has |
| `read_conventions()` | `CONVENTIONS.md` from the **base** revision |

Every tool result is tagged `[ref:tN]`, and each finding must list the refs that support
it. Grounding grades each finding:

- **Verified:** it cites a successful tool call or Layer 1 check that concerns its file.
  Confidence is kept.
- **Diff-only:** it cites only the diff. Confidence is capped at 0.6, so it can't block a
  merge.
- **Unverified:** it cites no valid refs (refs that never ran, or failed calls). It is
  dropped.

Dropped findings appear in the JSON output under `dropped`, each with its reason. The
loop enforces the tool-call budget (default 25) and token and cost limits. Near a limit,
it refuses further tools and asks for submission, so a review always ends with a
structured result. The full tool trace is included in the output.

**First measurements** come from the toy smoke set, with GPT-5 as reviewer. These five
hand-written items sanity-check the pipeline and aren't a benchmark:

| mode | recall | precision | findings per clean diff | cost/review | latency |
|---|---|---|---|---|---|
| deterministic | 50% | 80% | 0.00 | $0 | 1s |
| agent (GPT-5) | 100% | 57% | 0.00 | $0.058 | 146s |

- The agent found every defect the deterministic layer can't see: the duplicated helper,
  the test that can't fail, and the silent behavior change.
- It reported the prompt-injection comment instead of obeying it.
- Most of its "false positives" restate a real defect under a second category on the
  same lines. Milestone 3 will measure and tune this on the dev split of real datasets.

### The overseer

A larger, more expensive model (default `openai/gpt-6-astra`) that double-checks the
primary reviewer, "the watcher of the watchman". **It is an opt-in tool, not part of every
review**: run `watchman review --mode agent --deep` on a change that deserves a rare,
expensive second opinion. Evaluation (below) found that running it automatically doesn't
pay for itself.

**What it does**, in one tool-using stage with its own caps (default $0.50 and 15 tool
calls per review, on top of the primary's budget):

1. **Spot-check.** It receives compact evidence packets for the primary's findings: the
   claim, a ±12 line code excerpt, and the tool output the primary cited. For each of up to
   12 findings it answers `confirm`, `reject`, or `downgrade`, with a reason. It can use the
   same read-only tools to check.
2. **Dig deep.** It investigates anything the primary escalated and anything risky the
   primary missed. New findings carry `source = overseer`, are grounded exactly like the
   primary's (they must cite tool calls that ran, with refs `o1`, `o2`, ...), and are
   dropped if the evidence isn't real.
3. **Brief.** It writes notes for the primary reviewer.

If it rejected or added **3 or more** findings, or rejected at least half of those it
checked, the notes go back to the primary model for **one** more pass with a fresh tool
budget (refs `r1`, `r2`, ...). The larger model directs the review; the cheap model does the
reading and running. Findings the overseer rejected stay rejected: a second-pass finding
that matches one is discarded.

**Automatic mode** (`auto = true`, off by default) also runs it without `--deep`, following
`trigger`: `risky` fires on a major or blocker primary finding, an escalated question, a
Layer 1 check that found problems, a large diff (over 300 lines or 8 files), security-
sensitive code (subprocess, eval, pickle, SQL execution, disabled TLS verification, crypto,
auth and session paths), or an unfinished primary review. `always` runs it every time.

**Trust and failure handling:**

- Everything the overseer reads is wrapped as untrusted data, including the primary
  reviewer's own text (it was written after reading untrusted code).
- It can never touch deterministic findings. Those aren't packaged for review.
- `[overseer]` is read from user config only. A repo's `.watchman.toml` can't turn it on,
  retarget it, or raise its budget.
- If the overseer fails (timeout, no credit, budget), the review keeps the primary's
  findings, says so in a warning, and records the error on the stage. It does not mark the
  review incomplete, since the primary review stands on its own.

Every result records per-stage usage (`stages` in the JSON) and what the overseer
confirmed, rejected, downgraded, and added. When the overseer is active, the primary
stage also keeps the findings the review would have reported without it, so primary-only
and overseer arms can be compared from a single run.

```toml
# ~/.config/watchman/config.toml (only values that differ from the defaults are saved)
[overseer]
# model = "openai/gpt-6-astra"   # default
# max_cost_usd = 0.50
# max_tool_calls = 15
# auto = false                   # true: also run automatically (see `trigger`)
# trigger = "risky"              # risky | always
```

## Evaluation

### Datasets

Frozen datasets are built from four permissively licensed, well-tested projects:
more-itertools (MIT), attrs (MIT), click (BSD-3), and httpx (BSD-3). Each item pins an
upstream commit SHA plus patches.

| dataset | items | dev / test | what it is |
|---|---|---|---|
| `seeded-v1` (A) | 60 | 41 / 19 | Real commits with **one injected AI-style defect**, from 13 hand-written templates |
| `reverted-v1` (B) | 30 | 22 / 8 | Real **bug fixes with the source change undone**; ground truth is the fix |
| `clean-v1` (C) | 60 | 44 / 16 | Unmodified real commits; measures false positives |
| `seeded-v3` | 30 | 18 / 12 | Real commits with one injected **readability or design defect**, from 6 templates for `over_abstraction` and `convention_violation` (see below) |
| `seeded-v2`, `reverted-v2`, `clean-v2` | 30, 16, 32 | all test | Held-out sets built from upstream commits v1 never used. **Never run.** Kept in reserve |

How the datasets are built:

- **Templates edit real code.** Templates (`evals/build/mutators.py`) are AST-guided
  edits to lines the real commit added. They cover:
  - swallowed or broadened exceptions
  - `raise` → `return None`
  - a changed default on an existing public function
  - off-by-one and inverted conditions
  - hallucinated packages and stdlib attributes
  - weakened tests
  - restating comments
  - copied helpers
  - drive-by renames
  - prompt-injection comments

  Each records exact ground-truth lines, plus acceptable alternates such as the import
  *and* the decorator. The build checks these against the generated diff.
- **No model writes the defects** a model is later graded on. The build is deterministic:
  `python -m evals.build.make_datasets --seed 20261014` reproduces it byte for byte.
- **The future is hidden.** Materialized repos contain no commits after the item, and
  reverted-fix items hide the fix's message, so a reviewer can't find the answer in
  `git log`. The fix's regression test is not included.
- **Splits are stratified** by template (A) or repo (B, C). Datasets were frozen after
  owner review of `REVIEW.md`. The test split was run once, then touched only by a logged
  infrastructure retry and a pre-registered re-score (`evals/results/test_access.log`).

Known biases:

- Clean items are real commits that may contain real issues, so measured precision is
  pessimistic.
- Every B item is labeled `correctness`, and B diffs are small, so lenient recall on B is
  inflated. Use strict recall there.
- v1 has no `over_abstraction` or `convention_violation` items; `seeded-v3` adds them, with its own
  limits (below).

### Running

```bash
uv run python -m evals.harness run --dataset seeded-v1 --mode agent --model anthropic/claude-haiku-5.5 --jobs 6
# every mode on one dataset (the results below used Haiku 5.5, default config; add --split test --final for held-out)
uv run python -m evals.harness run --dataset seeded-v1 --mode deterministic
uv run python -m evals.harness run --dataset seeded-v1 --mode baseline --model anthropic/claude-haiku-5.5
uv run python -m evals.harness run --dataset seeded-v1 --mode agent_no_l1 --model anthropic/claude-haiku-5.5
uv run python -m evals.tune policies   # replay post-processing policies on dev runs (free)
make results                 # regenerates evals/results/RESULTS.md
```

### Results

All numbers below come from `evals/results/RESULTS.md` (regenerate with `make results`; per-run
JSON in `evals/results/runs/`). Rates are *found/total* with a 95% Wilson interval. With 8 to 19
test items per dataset the intervals are wide, so read differences of less than about 20
points as "same direction, not proven".

**Held-out test split, Haiku 5.5 as the reviewer** (`min_confidence` 0.6):

| arm | seeded recall | seeded strict recall | seeded precision | findings per clean diff | cost per review |
|---|---|---|---|---|---|
| deterministic (Layer 1 only) | 21% (4/19) [9-43] | 21% | 15% (4/26) | 0.06 | $0 |
| baseline (one prompt) | 74% (14/19) [51-88] | 58% | 50% (14/28) | 0.38 | $0.002 |
| agent without Layer 1 | 68% (13/19) [46-85] | 47% | 72% (13/18) | 0.50 | $0.004 |
| **agent (full Watchman)** | **84% (16/19) [62-94]** | **79%** | 32% (16/50) | 0.50 | $0.005 |

The same arms on the 41 dev seeded items: 27%, 59%, 56%, and 78% recall (11, 24, 23 and 32 found), at $0 to $0.004 per review.

What the ablations show:

1. **Layer 1 evidence helps the agent, on both splits.** The agent that sees Layer 1 results
   finds more than the same agent without them: 78% vs 56% on dev (32/41 vs 23/41) and 84% vs
   68% on test (16/19 vs 13/19). Strict recall differs more: 79% vs 47% on test. The biggest
   category gaps on dev are scope creep (3/3 vs 0/3) and swallowed errors (6/7 vs 3/7).
2. **The agent beats the single-prompt baseline in direction, not provably on test.** Dev: 78%
   vs 59%. Test: 84% vs 74%, which is 16/19 vs 14/19, well inside noise.
3. **Layer 1 alone is quiet but misses most defects.** About 0.06 findings per clean diff, and
   21-27% recall. It's best at hallucinated dependencies and swallowed errors.
4. **Precision on test is much worse for the agent (32%) than on dev (56%).** One test item
   explains most of it. A seeded off-by-one in click broke the project's own test suite, and
   Layer 1 correctly reported 21 failing tests. Those findings have no line number, so
   line-overlap scoring counts every one as a false positive. Excluding that item the agent's
   precision is 16/29 = 55%. That number is a post-hoc observation and is not used for any
   claim. The underlying product problem, 21 near-identical findings for one cause, is
   fixed after the test run (failures are grouped per test file).
5. **Reverted-fix items (B) can't tell the arms apart.** Lenient recall is 89-91% for every LLM
   arm, but every B item is labeled `correctness` and models usually pick a different category,
   so strict recall is 11-27%. These diffs are small, so lenient recall is inflated. Treat B as
   a smoke test.

**Readability and design defects: `seeded-v3`** (30 items, Haiku 5.5, default config, nothing tuned on it;
the owner approved freezing it and running every mode on both splits). Its six templates are mechanical, like
v1's: a pass-through wrapper that adds nothing, an unused keyword parameter, an unused "compatibility"
alias, a snake_case local renamed to camelCase, `from x import *`, and a debug `print` left in library code.
Upstream commits are disjoint from v1 and v2.

| arm | held-out test (12) | dev (18) | all 30 | cost per review |
|---|---|---|---|---|
| deterministic (Layer 1 only) | 0% (0/12) | 6% (1/18) | 3% (1/30) | $0 |
| baseline (one prompt) | 50% (6/12) | 67% (12/18) | 60% (18/30) | $0.001 |
| agent without Layer 1 | 50% (6/12) | 67% (12/18) | 60% (18/30) | $0.004 |
| **agent (full Watchman)** | **75% (9/12) [47-91]** | **83% (15/18) [61-94]** | **80% (24/30) [63-90]** | $0.004 |

Layer 1 evidence again helps the agent: on the 30 items the full agent found 6 that the same agent without
Layer 1 missed and lost none (sign test p = 0.03). Against the single-prompt baseline it found 8 that the
baseline missed and lost 2 (p = 0.11, same direction, not proven). By category the agent found 12 of 15
`over_abstraction` and 12 of 15 `convention_violation` items; it found every dead-code, debug-print and
wildcard-import item and was weakest on unused parameters (3/5) and camelCase renames (2/5). The checks alone
find almost none of these (1/30): ruff and the quality check were not built for them, which is the point.

**Exploratory: the same agent with Google mode on** (`style = "google"` in user config, same 30 items and command as above;
run files in `evals/results/v3_google/`, kept out of `runs/` so they don't replace the primary results): 87% (26/30), 83% (10/12) held out, against 80% (24/30) with the
default config. Paired, it found 6 items the default run missed (three camelCase renames, two unused
parameters, a wrapper) and missed 4 it had found (three dead-code items, a wrapper): p = 0.75, which is
noise. What changes reliably is volume: 76 findings against 46, so lower pessimistic precision (34% vs 52%),
because the style rules add real but unlabeled findings. I treat this as "Google mode doesn't hurt recall and
adds style feedback", not as an improvement.

Limits: one injected defect per item, plainer than real AI code (the same caveat as v1); 12 held-out items
make wide intervals; the dataset has no clean items, so false positives come from `clean-v1` (the "findings
per clean diff" column above); and I designed the templates knowing which Layer-1 rules existed, though
none of them was tuned on v3.

**Model tiers, dev split, identical items** (agent mode; 20 items shared by all three, 40 for GPT-5):

| reviewer | seeded recall | findings per clean diff | major+ findings per clean diff | cost per review |
|---|---|---|---|---|
| Haiku 5.5 | 88% | 0.43 | 0.00 | $0.004 |
| Opus 4.6 | 88% | 1.43 | 0.43 | $0.20 |
| GPT-5 | 88% | 1.71 | 0.57 | $0.22 |

The larger models found the same defects but produced three to four times as many findings on
known-good commits, at about 50 times the cost. Some of those extra findings look like real
upstream issues (Opus and GPT-5 independently flagged the same wrong type hint in attrs), and many are
nits or restatements, so this measures noise, not necessarily wrongness. Opus did better on
reintroduced bugs (60% vs 40% strict, on only 5 items).

**Gemini Flash 3.7 vs 3.8 as the primary reviewer** (dev, same 40-item stratified sample, agent mode, overseer off):

| model | seeded recall (strict) | reverted recall | findings per clean diff | cost per review | latency |
|---|---|---|---|---|---|
| Gemini 3.7 Flash | 69% (11/16), strict 62% | 80% (8/10) | 0.21 | $0.05 | about 4 min |
| Gemini 3.8 Flash | 75% (12/16), strict 75% | 90% (9/10) | 0.36 | $0.07 | about 6 min |

The differences are one or two items, well inside noise, so the cheaper, faster, quieter 3.7 is
the default. (Both ran under OpenRouter's new-account limit of 20 requests per minute, enforced by
`--rpm 18`.)

**The overseer, dev split, all 107 items, paired** (Gemini 3.7 Flash primary, Claude Opus 5.5
as the overseer, run once with the overseer always on; each result stores the primary-only
findings, so all three arms score the same primary reviews):

| arm | defects found (63) | strict | precision | findings per clean diff | cost per review | overseer ran on |
|---|---|---|---|---|---|---|
| primary only | 52 (83%) [72-90] | 40 | 52/77 (68%) | 0.27 | $0.046 | 0 |
| overseer on risk triggers | 52 (83%) | 40 | 52/81 (64%) | 0.27 | $0.082 | 57 of 107 |
| overseer always | 57 (90%) [80-95] | 42 | 57/90 (63%) | 0.41 | $0.109 | 107 of 107 |

- **Risk-triggered gained nothing.** Same defects found, lower precision, 80% more cost.
- **Always-on found 5 more defects** (+7 points, intervals overlap) for 2.4x the cost, and flagged
  more on clean diffs (0.41 vs 0.27).
- **The overseer rarely filters.** Of 72 findings it spot-checked it rejected 2, confirmed 59, and
  downgraded 11, and it never rejected a true positive. It mostly adds findings (31 added).
- **Its gains are in reviews where the primary found nothing.** Ten of the 11 missed defects were
  in reviews with no primary findings, and the overseer caught 5 of them. "The primary reported
  nothing" is also true of 84% of clean diffs, so no cheap trigger isolates those cases.
- **Escalations went unused.** Flash raised 0 escalated questions in 107 reviews.

An earlier version of the risk trigger counted a Layer 1 check that *couldn't run* (the target
repo's tests failing to install in the sandbox, 53% of these reviews) as a reason to call the
overseer. That fired on 71% of reviews and made the risk arm look 3 points better. It only
looked better because the overseer ran on more items; fixed to count only checks that found
problems, the gain disappears. Both numbers are in `evals.tune overseer-triggers`.

**Decision.** The overseer isn't worth running on every review, so it is an opt-in `--deep` tool,
which also makes a more expensive model affordable (the default is GPT-6 Astra at $10/$50 per
million tokens, from a different model family than both the Gemini reviewer and a Claude coding
worker). I ran the cheaper Opus 5.5 for the evaluation above, so Astra's own numbers are not
measured here. A held-out run of this design on fresh `*-v2` datasets (30 seeded, 16 reverted, 32
clean, frozen, never run) was cancelled once the dev result was clearly negative. Those sets
stay in the repo as unused held-out data for the next design change.

**Settings tuned on the dev split only** (`python -m evals.tune`): `min_confidence` 0.6, and
dropping a model finding that overlaps a stronger finding on the same lines. With the Haiku
agent on dev this raised precision from 56% to 68% for a 4-point recall cost. The Layer 1
precision fixes (coverage pragmas, test fixtures, mypy on tests, rules from a newer ruff, and
so on) took findings per clean diff from 1.18 to 0.07.

**Problems found in the evaluation itself** (kept here because they change how far to trust
the numbers):

- **Provider errors were counted as empty reviews.** OpenRouter can answer HTTP 200 with an
  error body ("A Timeout Occurred") and no choices. That was neither retried nor treated as a
  failure, so some reviews silently returned zero findings. Fixed: it is retried, a review that
  can't finish is marked `incomplete` (never PASS, CLI exit 2), and the harness excludes
  incomplete items from rates and reports them under `errors`. Affected runs were audited and
  the failed items re-run: GPT-5 dev (6 items), Haiku test seeded agent (5 items). Three GPT-5
  reverted-set items are still errors and are excluded.
- **The tuned threshold was not applied to most runs.** Saving the user config pinned the old
  default `min_confidence = 0.5`. Fixed (`save_user_config` writes only non-default values), and
  every run was re-scored at the committed 0.6 from its saved model output, with no new model
  calls. That re-score of the test split was logged as pre-registered: the policy was committed
  (`a0524a6`) before the test run started.
- **Opus 4.6 test results were discarded.** About half its test items had hit the provider-timeout
  bug, and the model has since been superseded. Its dev results stay, because they ran
  before any failures in that stage.
- **The test split was accessed more than once.** Every access is in
  `evals/results/test_access.log`: the single full run, one infrastructure retry of 5 failed
  items, and the pre-registered re-score. Nothing was tuned on test results.

## Development

```bash
make check                       # ruff, ruff format --check, mypy --strict, pytest
uv run pytest -m network         # tests that hit PyPI
uv run pytest -m docker          # tests that need a docker daemon
```

## Roadmap

1. ✅ Foundation: CLI, schema, deterministic checks, single-prompt baseline, eval harness skeleton
2. ✅ Agent: tool-using loop with budgets, pitfall rules, and evidence-ref grounding
3. ✅ Evals: frozen datasets A/B/C, ablations, precision tuning
3b. ✅ Overseer: a larger model for a deeper second look, opt-in via `--deep` (automatic use evaluated and rejected)
4. ✅ Integration: GitHub Action with an updatable PR comment, pre-push hook, demo recording, [writeup](docs/WRITEUP.md)
5. ✅ Live mode: supervising an OpenCode worker at checkpoints (snapshot reviews, round cap, advisory/blocking, plugin + MCP)
6. ✅ Code quality and style: maintainability check, opt-in Google Python Style Guide mode, two pre-registered worker experiments, and `seeded-v3` (over-abstraction and convention defects)

## Design decisions

- **Package name `watchman_review`, CLI `watchman` (alias `wm`).** This avoids a PyPI and
  import clash with Meta's `watchman` / `pywatchman`.
- **Thin in-house LLM adapter instead of LiteLLM or vendor SDKs.** There are two wire
  formats (OpenAI-compatible, which covers OpenAI, OpenRouter, and Ollama, plus native
  Anthropic), and both are small. Owning them keeps tool-call normalization, cost capture,
  and record/replay under test, and keeps dependencies light.
- **Record/replay LLM cache.** Responses are keyed by a hash of (model, request). Eval
  reruns are reproducible and free, and tests never call a real model.
- **Budgets are enforced in code.** Per-review token, cost, and tool-call caps, plus an
  append-only monthly ledger (`~/.local/share/watchman/ledger.jsonl`) that refuses calls
  once the cap is reached. Cached responses count toward the per-review budget (so a
  replayed eval behaves identically) but not toward real spend.
- **Grounding is post-processing, not a prompt instruction.**
  - Model findings that cite files outside the repo or diff, or lines past the end of a
    file, are dropped.
  - Agent findings must also cite refs of tool calls that actually ran and succeeded.
    Confidence is capped by evidence grade (see [The agent](#the-agent)).
  - Every drop is recorded with its reason.
  - The model can't set a finding's `source`, `rule`, or `id`.
- **Submission via a `submit_findings` tool, not free text.** Tool-call arguments are
  more reliable structured output across providers. A JSON-in-text answer is still
  accepted as a fallback.
- **All tools are offered on every turn, even in forced-submit mode.** Some providers
  reject histories that reference tools missing from the request, so the loop refuses
  non-submit calls itself.
- **Tool output is untrusted too.** File contents, test output, and commit messages are
  wrapped in `<untrusted_tool_output>`. The toy set includes a prompt-injection canary
  (`seeded-003`).
- **Reasoning models need headroom.** GPT-5 spends hidden reasoning tokens out of
  `max_tokens`, so the defaults are 16k output tokens and a 300 s timeout.
- **`default_mode` lives in user config only**, so a reviewed repo can't opt you into
  paid LLM modes.
- **Eval runs count toward the monthly cap.** The harness records real spend in the
  same ledger as reviews.
- **Reviewed content is untrusted.**
  - The diff, task, and conventions are wrapped in `<untrusted_*>` delimiters, and
    attempts to close a tag early are neutralized.
  - A repo's `.watchman.toml` can't change the provider, endpoint, model, or key source,
    because a malicious repo could otherwise redirect the API key.
  - `.watchman.toml` and `CONVENTIONS.md` are read from the **base** revision, so a change
    can't relax its own review.
  - The sandbox environment is scrubbed, so reviewed code never sees API keys.
  - mypy runs with an empty config, because the repo's mypy plugins would execute repo
    code outside the sandbox.
- **Review a snapshot, not the live tree.** Checks run in a detached `git worktree` of
  `head`. `--worktree` is the explicit opt-in for uncommitted changes.
- **Precision over recall.**
  - Deterministic findings are limited to changed lines.
  - PyPI network failures map to `UNKNOWN` and never produce a finding.
  - Test failures are verified against the merge-base.
  - Co-located findings with the same category are merged (for example, ruff E722 and
    S110 on one bare `except: pass`).
- **Precision changes from dogfooding** (running Watchman on this repo):
  - Dropped ruff rules that produced false positives:
    - ARG001 fires on required callback signatures.
    - S105/S106 fire on enum members like `PASS = "pass"`.
    - B011/PT015 fire on `assert False`, which tests use intentionally.
  - Only unconditional skip decorators count as test tampering.
  - Suppressions that carry a justification are accepted.
  - A private-key match requires key material, not just the header.
  - Pattern-only secret matches in tests are downgraded.
  - Undeclared imports in test files (pytest and similar) are not reported.
- **Baseline fairness.** The baseline and the agent render their pitfall catalog from one
  source (`prompts/pitfalls.py`) and share the severity guide. The agent's version adds a
  "verify with" column, so the ablation measures tools and verification rather than
  prompt wording.
- **A larger model checks the cheap one, on request.** On the v1 dev split the cheap reviewer
  found as many defects as the 50x costlier ones and was far quieter on clean diffs, so the
  expensive model supervises instead of replacing it. Measured, though, the supervision didn't
  pay for itself automatically (risk-triggered: no gain for +80% cost; always-on: +7 points of
  recall for 2.4x cost and more noise), so it ships as an opt-in `--deep` tool. Rare use is
  also what makes a premium model reasonable.
- **Notes flow back to the cheap model, not to the user.** The larger model directs and the
  cheap model does the reading and test-running, which keeps the overseer's cost small
  (compact packets, 8 tool calls, a hard dollar cap). The cost is that overseer notes are
  model-written text passed to another model, so they are wrapped as untrusted too.
- **One stage engine, three callers.** The primary reviewer, the overseer, and the second
  pass run on the same tool loop (`agent/engine.py`), differing only in their submit tool and
  tool-ref prefix (`t`, `o`, `r`), so budgets, grounding, and prompt-injection handling are
  identical across stages.
- **The overseer cannot silently approve itself.** It never sees deterministic findings as
  things to judge, its own findings must cite tool calls that ran, and unverifiable ones
  are dropped and not counted toward the feedback threshold.
- **Provider errors are failures.** A model stage that errors or never submits marks the
  review `incomplete` (never PASS, exit code 2), instead of looking like a clean review.
- **Restatements need real overlap across categories.** A real run showed that dropping
  any finding within two lines of a stronger one lost a SQL injection sitting under a
  prompt-injection comment. Different categories must now overlap on the same lines;
  the same category may still be two lines apart.
- **Config files never pin old defaults.** `save_user_config` writes only values that
  differ from the defaults, so later improvements to a default reach existing users.

## Known limitations

- Python-only target repos for now.
- **Code quality is measured only by proxies.** The maintainability and Google-mode checks detect specific,
  mechanical departures (function size, complexity, docstrings, naming, 80 columns, ...). They do not judge
  design, elegance or whether a change fits the project, except through the LLM agent, which is not
  separately evaluated on those qualities. The two quality experiments (36 and 18 runs) moved every metric in
  the intended direction, none significantly on its registered primary comparison, and their metrics
  overlap the checks (see [Code quality and style](#code-quality-and-style)).
- **Google mode is noisy on repos that follow another style.** On the evaluation's real commits it fires about
  1.4 times per clean commit (mostly 80-column lines, relative imports and non-Google docstring sections),
  so it is opt-in and user-level only. Formatting other than line length is left to a formatter.
- Without a repo virtualenv (`.venv/`), mypy can't resolve third-party types, so
  hallucinated *third-party* APIs are left to the agent. Hallucinated stdlib APIs are
  still caught.
- In Docker, dependencies are installed into each review's temporary snapshot. That is
  slow for large repos, and a cached per-lockfile image is planned.
- In subprocess-sandbox mode, the target repo's tests run with your user's filesystem
  permissions. Network is isolated only where unprivileged user namespaces are available.
- Coverage diff isn't implemented yet.
- The slopsquatting heuristic (package < 90 days old or ≤ 1 release) is coarse.
- On defect items, eval precision is pessimistic: real but unlabeled issues count as
  false positives. The clean-diff set is the uncontaminated false-positive measure.
- Reasoning-heavy models are slow: GPT-5 agent reviews took 2 to 6 minutes, Gemini 3.7 Flash
  about 4, and Haiku 5.5 about 1.5, mostly sandboxed test runs.
- The agent sometimes restates one defect under two categories. Findings that overlap on the
  same lines are collapsed, but a restatement a few lines away under a different category is
  kept, because collapsing those lost a real SQL injection once.
- The `pytest` check's dependency install is best-effort. Before milestone 4 it failed on about
  half of the real reviews in the evaluation, for three separate reasons: `pip install -e
  '.[test]'` exits 0 when a project has no `test` extra (so the `tests` extra was never tried),
  setuptools-scm and hatch-vcs projects can't derive a version inside the container, and PEP 735
  dependency groups were never read. All three are fixed (Docker sandbox), and a flood of
  failures is now compared against the whole base suite instead of being blamed on the change.
  **The v1 evaluation numbers above predate those fixes**, so they understate Layer 1 on real
  repositories; they were not re-run. (`seeded-v3` was run afterwards, with the fixes and Docker.) The subprocess fallback sandbox still installs nothing, and
  a project whose tests need system libraries or a service will still report `error`.
- The agent's `escalations` channel (hand a question to the overseer) went unused by Gemini
  Flash in evaluation, so in practice the overseer works from the primary's findings and the diff.
- Model behavior varies run to run, even at temperature 0 with a fixed seed. Use the
  record/replay cache (`--llm-cache read_write`) when you need a review you can reproduce.
- The default overseer (GPT-6 Astra) is a different model family from the primary reviewer
  (Gemini Flash) and from a Claude coding worker, to avoid shared blind spots. That benefit is
  untested: the overseer evaluation used Claude Opus 5.5, and the live-mode experiment hit a ceiling
  (the worker made no errors), so it could not measure blind-spot overlap either.
- Overseer notes are model-written. A prompt-injection in the reviewed code could try to
  steer them; they are wrapped as untrusted, and the second pass must re-verify claims with
  its own tools, but this isn't a hard guarantee.

## License

CC0 1.0. See `LICENSE`.
