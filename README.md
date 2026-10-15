# Watchman

Watchman is an AI reviewer for code written by AI coding agents. It checks that a change is
high quality, avoids the pitfalls typical of AI-written code, follows the repo's own
conventions, and is ready for a senior developer's PR review.

It is **tool-using and verifying**. A cheap deterministic layer runs first (lint, types,
tests, secret scan, dependency-existence check). After that, an LLM reviewer agent
*verifies* its suspicions with tools before reporting them. Every finding must cite
evidence, and findings that can't be grounded are dropped.

> **Status:** the CLI, the tool-using agent, the evaluation, and the integrations (a GitHub
> Action that comments on pull requests, and a git pre-push hook) are complete. The overseer
> (a larger model for a deeper second look) is an opt-in `--deep` tool; automatic use didn't
> pay for itself in evaluation. See [Roadmap](#roadmap) and the
> [design writeup](docs/WRITEUP.md).

**See it in 60 seconds:** `asciinema play demo/watchman.cast` (a 50 second recording), or run
`bash demo/demo.sh` yourself. It builds a throwaway project, has an "AI agent" add a hallucinated
import, a leaked token, a bare `except`, and a skipped test, and shows Watchman catching them
and a pre-push hook rejecting the push. The deterministic part needs no API key.

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

Three frozen datasets are built from four permissively licensed, well-tested projects:
more-itertools (MIT), attrs (MIT), click (BSD-3), and httpx (BSD-3). Each item pins an
upstream commit SHA plus patches.

| dataset | items | dev / test | what it is |
|---|---|---|---|
| `seeded-v1` (A) | 60 | 41 / 19 | Real commits with **one injected AI-style defect**, from 13 hand-written templates |
| `reverted-v1` (B) | 30 | 22 / 8 | Real **bug fixes with the source change undone**; ground truth is the fix |
| `clean-v1` (C) | 60 | 44 / 16 | Unmodified real commits; measures false positives |

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
- There are no `over_abstraction` or `convention_violation` items.

### Running

```bash
uv run python -m evals.harness run --dataset seeded-v1 --mode agent --model anthropic/claude-haiku-5.5 --jobs 6
bash evals/ablation.sh       # the full dev-split ablation used for the results below
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
5. Live mode: supervising an OpenCode worker at checkpoints

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
  **The evaluation numbers above predate those fixes**, so they understate Layer 1 on real
  repositories; they were not re-run. The subprocess fallback sandbox still installs nothing, and
  a project whose tests need system libraries or a service will still report `error`.
- The agent's `escalations` channel (hand a question to the overseer) went unused by Gemini
  Flash in evaluation, so in practice the overseer works from the primary's findings and the diff.
- Model behavior varies run to run, even at temperature 0 with a fixed seed. Use the
  record/replay cache (`--llm-cache read_write`) when you need a review you can reproduce.
- The default overseer (GPT-6 Astra) is a different model family from the primary reviewer
  (Gemini Flash) and from a Claude coding worker, to avoid shared blind spots. That benefit is
  untested: the overseer evaluation used Claude Opus 5.5, and measuring blind-spot overlap needs
  live mode (milestone 5).
- Overseer notes are model-written. A prompt-injection in the reviewed code could try to
  steer them; they are wrapped as untrusted, and the second pass must re-verify claims with
  its own tools, but this isn't a hard guarantee.

## License

CC0 1.0. See `LICENSE`.
