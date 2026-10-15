#!/usr/bin/env bash
# Every mode on all of seeded-v3 (dev and the held-out test split), approved by the owner.
# Each test-split invocation is appended to evals/results/test_access.log by the harness.
# Configuration is the one chosen for v1 (Haiku 5.5, default config), unchanged for v3: nothing
# here was tuned on v3. Needs docker access (run under `sg docker -c`).
set -euo pipefail
cd "$(dirname "$0")/.."
H=anthropic/claude-haiku-5.5
run() { uv run python -m evals.harness run --dataset seeded-v3 "$@"; }

run --split dev --mode deterministic --jobs 5
run --split test --final --mode deterministic --jobs 5
for mode in baseline agent_no_l1 agent; do
  run --split dev --mode "$mode" --model "$H" --jobs 6
  run --split test --final --mode "$mode" --model "$H" --jobs 6
done
uv run python -m evals.harness report
echo "V3 RUN DONE"
