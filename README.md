# Watchman

Watchman is an AI reviewer for code written by AI coding agents. It checks that a change is
high quality, avoids the pitfalls typical of AI-written code, follows the repo's own
conventions, and is ready for a senior developer's PR review.

It is **tool-using and verifying**. A cheap deterministic layer runs first (lint, types,
tests, secret scan, dependency-existence check). After that, an LLM reviewer agent
*verifies* its suspicions with tools before reporting them. Every finding must cite
evidence, and findings that can't be grounded are dropped.

> **Status:** milestone 1 (foundation) is complete: the deterministic layer, the
> single-prompt baseline, the CLI, and the eval harness skeleton. The tool-using agent is
> milestone 2. See [Roadmap](#roadmap).

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

# Single-prompt LLM baseline (the ablation control arm):
uv run watchman review --base main --mode baseline --task "Add retry to the HTTP client"
```

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
          Layer 2: LLM reviewer (M1: single-prompt baseline; M2: tool-using agent)
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

## Evaluation

```bash
uv run python -m evals.harness list --dataset toy
uv run python -m evals.harness run --dataset toy --mode deterministic
make results    # regenerates evals/results/RESULTS.md from saved runs
```

Datasets live in `evals/datasets/<name>/`. Each item has a `base/` tree, a `head/` tree,
and ground-truth defects (file, line range, category). Freezing a dataset records a
SHA-256 of its content, and the harness refuses to run if a frozen dataset has changed.
Items are split dev/test by a seeded hash. The held-out **test split** only runs with
`--final`, and every such run is appended to `evals/results/test_access.log`. Milestone 3
builds the real datasets: seeded defects, reverted fixes, and clean diffs.

## Development

```bash
make check                       # ruff, ruff format --check, mypy --strict, pytest
uv run pytest -m network         # tests that hit PyPI
uv run pytest -m docker          # tests that need a docker daemon
```

## Roadmap

1. ✅ Foundation: CLI, schema, deterministic checks, single-prompt baseline, eval harness skeleton
2. Agent: tool-using loop with budgets and pitfall rules
3. Evals: frozen datasets A/B/C, ablations, precision tuning
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
- **Grounding is post-processing, not a prompt instruction.** Model findings that cite
  files outside the repo or diff, or lines past the end of a file, are dropped and counted
  in `dropped_findings`. Milestone 2 adds tool-call-ID citations.
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
- **Baseline fairness.** The single-prompt baseline gets the same category vocabulary,
  severity guide, and output schema as the agent will, so the ablation measures tools and
  verification rather than prompt wording.

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

## License

CC0 1.0. See `LICENSE`.
