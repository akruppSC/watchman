# Watchman

Watchman is an AI reviewer for code written by AI coding agents. It checks that a change is
high quality, avoids the pitfalls typical of AI-written code, follows the repo's own
conventions, and is ready for a senior developer's PR review.

It is **tool-using and verifying**. A cheap deterministic layer runs first (lint, types,
tests, secret scan, dependency-existence check). After that, an LLM reviewer agent
*verifies* its suspicions with tools before reporting them. Every finding must cite
evidence.

> Status: early development (milestone 1: foundation). See [Roadmap](#roadmap).

## Install

Requires Python 3.12+ and [uv](https://docs.astral.sh/uv/).

```bash
git clone <this repo> && cd watchman
uv sync --all-extras
uv run watchman --help
```

## Quick start

```bash
uv run watchman setup                       # choose provider, models, budget, key source
uv run watchman review --base main --head HEAD
uv run watchman review --base main --head HEAD --json > review.json
```

## Roadmap

1. Foundation: CLI, schema, deterministic checks, single-prompt baseline, eval harness skeleton
2. Agent: tool-using loop with budgets and pitfall rules
3. Evals: frozen datasets A/B/C, ablations, precision tuning
4. Integration: GitHub Action and PR comment, pre-push hook, writeup, demo
5. Live mode: supervising an OpenCode worker at checkpoints

## Design decisions

Recorded as they are made.

- **Package name `watchman_review`, CLI `watchman` (alias `wm`).** This avoids a PyPI and
  import clash with Meta's `watchman` / `pywatchman`.

## Known limitations

- Python-only target repos for now.

## License

CC0 1.0. See `LICENSE`.
