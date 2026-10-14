#!/usr/bin/env bash
# The single held-out run for the overseer design, on the frozen v2 datasets (every item is
# test split; nothing in v2 was used for tuning).
#
#   deterministic: Layer 1 only
#   agent + overseer: Gemini Flash primary, Opus overseer on the risk trigger as it will ship.
#   The result also stores the primary-only findings of the same reviews, so the
#   "without the overseer" arm is scored from the same primary run (paired, no extra cost).
#
# Every invocation is logged in evals/results/test_access.log. Do not tune after this.
set -euo pipefail
cd "$(dirname "$0")/.."
RPM="${RPM:-18}"
M=google/gemini-3.7-flash
run() { uv run python -m evals.harness run --split test --final --rpm "$RPM" "$@"; }

for ds in seeded-v2 reverted-v2 clean-v2; do
  run --dataset "$ds" --mode deterministic --jobs 5
done
for ds in seeded-v2 reverted-v2 clean-v2; do
  run --dataset "$ds" --mode agent --model "$M" --overseer risky --jobs 6
done
uv run python -m evals.harness report
echo "FINAL V2 DONE"
