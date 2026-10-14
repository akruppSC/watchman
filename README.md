# Watchman

Watchman is an AI reviewer for code written by AI coding agents. It checks that a change is
high quality, avoids the pitfalls typical of AI-written code, follows the repo's own
conventions, and is ready for a senior developer's PR review.

It is **tool-using and verifying**. A cheap deterministic layer runs first (lint, types,
tests, secret scan, dependency-existence check). After that, an LLM reviewer agent
*verifies* its suspicions with tools before reporting them. Every finding must cite
evidence, and findings that can't be grounded are dropped.

> **Status:** milestones 1 (foundation), 2 (the tool-using agent) and 3 (evaluation) are
> complete. Next: an overseer stage (a larger model that double-checks risky reviews). See
> [Roadmap](#roadmap).

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

## How it works

```
git diff (merge-base..head) ──► snapshot worktree of head
                                  │
          Layer 1: deterministic checks (cheap, high precision)
          secrets · imports · hygiene · scope · ruff · mypy · pytest(sandbox)
                                  │  results become evidence for ↓
          Layer 2: tool-using agent: plan → verify with tools → submit_findings
                                  │
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
evals/ablation.sh            # the full dev-split ablation used for the results below
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
4. Integration: GitHub Action and PR comment, pre-push hook, writeup, demo
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
- Reasoning-heavy models are slow: GPT-5 agent reviews took 2 to 6 minutes. Haiku 5.5 takes about 1.5 minutes, mostly sandboxed test runs.
- The agent tends to restate one defect under two categories. This isn't merged
  automatically yet, because cross-category merging could hide a genuinely different
  issue on the same line.
- Model behavior varies run to run, even at temperature 0 with a fixed seed. Use the
  record/replay cache (`--llm-cache read_write`) when you need a review you can reproduce.

## License

CC0 1.0. See `LICENSE`.
