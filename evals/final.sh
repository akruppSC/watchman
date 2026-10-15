#!/usr/bin/env bash
# The single held-out test-split run for milestone 3. Every invocation is appended to
# evals/results/test_access.log by the harness. Do not tune anything after running this.
#
# Configuration chosen on the dev split: agent mode with Claude Haiku 5.5 (best precision
# on clean diffs at ~1/50 the cost of GPT-5/Opus), with the deterministic and baseline
# arms for the ablation, and Opus 4.6 agent as the stronger-model reference.
set -euo pipefail
cd "$(dirname "$0")/.."

H=anthropic/claude-haiku-5.5
O=anthropic/claude-opus-4.6
run() { uv run python -m evals.harness run --split test --final "$@"; }

for ds in seeded-v1 reverted-v1 clean-v1; do
  run --dataset "$ds" --mode deterministic --jobs 5
  for mode in baseline agent agent_no_l1; do
    run --dataset "$ds" --mode "$mode" --model "$H" --jobs 6
  done
  run --dataset "$ds" --mode agent --model "$O" --jobs 6
done
uv run python -m evals.harness report
echo "FINAL TEST RUN DONE"
