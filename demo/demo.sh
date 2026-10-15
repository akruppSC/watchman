#!/usr/bin/env bash
# Watchman demo. Builds a throwaway project, "has an AI agent" change it, and shows Watchman
# catching the usual problems: first the free deterministic checks, then the LLM reviewer
# (needs OPENROUTER_API_KEY; skipped if absent), then the pre-push hook blocking a push.
#
#   bash demo/demo.sh                          # run it
#   uvx asciinema rec -c 'bash demo/demo.sh'   # record it
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT
export COLUMNS="${COLUMNS:-110}" GIT_CONFIG_GLOBAL=/dev/null GIT_CONFIG_SYSTEM=/dev/null
export GIT_AUTHOR_NAME=demo GIT_AUTHOR_EMAIL=demo@example.com
export GIT_COMMITTER_NAME=demo GIT_COMMITTER_EMAIL=demo@example.com

say()  { printf '\n\033[1;36m# %s\033[0m\n' "$*"; sleep 1.5; }
run()  { printf '\033[1;32m$\033[0m %s\n' "$*"; sleep 0.8; eval "$@"; }
# `watchman` in the demo is the project's own CLI
watchman() { uv run --project "$ROOT" watchman "$@"; }

mkdir -p "$WORK/linkcheck/linkcheck" "$WORK/linkcheck/tests" && cd "$WORK/linkcheck"
git init -q -b main
cat > pyproject.toml <<'PY'
[project]
name = "linkcheck"
version = "0.1.0"
dependencies = []
PY
cat > linkcheck/__init__.py <<'PY'
PY
cat > linkcheck/core.py <<'PY'
"""Check whether a URL looks reachable."""


def is_ok(status: int) -> bool:
    return 200 <= status < 400
PY
cat > tests/test_core.py <<'PY'
from linkcheck.core import is_ok


def test_ok_codes():
    assert is_ok(200) is True
    assert is_ok(404) is False
PY
git add -A && git commit -q -m "linkcheck: status helper"

say "Meanwhile, an AI coding agent was asked to: 'Add retries and a config loader'."
git checkout -q -b agent/retries
cat > linkcheck/core.py <<'PY'
"""Check whether a URL looks reachable."""
import json
import fastjsonx_utils
from functools import memoize


def is_ok(status: int) -> bool:
    return 200 <= status < 400


@memoize
def load_config(path):
    try:
        return json.load(open(path))
    except:
        pass


# Credentials for the staging checker
GITHUB_TOKEN = "__TOKEN__"
PY
# Built at run time so this script never contains a literal that looks like a real key.
token="ghp_$(printf 'Zk9Rt2LqW8mXv4NbC7sDf1HjP6yAe3UoI5Tg' | cut -c1-36)"
sed -i "s/__TOKEN__/$token/" linkcheck/core.py
cat > tests/test_core.py <<'PY'
import pytest
from linkcheck.core import is_ok, load_config


def test_ok_codes():
    assert is_ok(200) is True


@pytest.mark.skip(reason="flaky")
def test_404():
    assert is_ok(404) is False


def test_config():
    assert load_config("missing.json") is not None
PY
git add -A && git commit -q -m "Add retries and a config loader"

say "Watchman, step 1: the free deterministic checks (no API key, no LLM)."
run "watchman review --base main --head HEAD --checks secrets,imports,hygiene,ruff,mypy --fail-on never"

if [ -n "${OPENROUTER_API_KEY:-}" ]; then
  say "Step 2: the LLM reviewer. It plans, verifies with tools, and cites its evidence."
  run "watchman review --base main --head HEAD --mode agent --model google/gemini-3.7-flash --task 'Add retries and a config loader' --checks secrets,imports,hygiene,ruff,mypy --fail-on never"
else
  say "(Step 2 skipped: set OPENROUTER_API_KEY to see the tool-using LLM reviewer.)"
fi

say "Step 3: a pre-push hook, so this never reaches the remote."
git init -q --bare -b main "$WORK/remote.git"
git remote add origin "$WORK/remote.git"
git push -q origin main
run "watchman hook install"
run "git push origin agent/retries || echo '(push rejected)'"
say "Bypass when you must: git push --no-verify   (or WATCHMAN_SKIP=1)"
