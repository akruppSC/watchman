#!/usr/bin/env bash
# Dev-split evaluation of the primary Flash model choice and of the overseer.
#
#   flash:    Gemini Flash 3.7 vs 3.8 as the primary reviewer, overseer off, on the same
#             40-item stratified sample used for the earlier model-tier comparison.
#   overseer: one run per dataset with the overseer ALWAYS on. Each result stores the
#             primary-only findings next to the overseer's, so `evals.tune overseer`
#             scores primary-only / overseer-when-risky / overseer-always from the same
#             primary review (paired, and no extra primary cost).
#
# Usage: evals/overseer_eval.sh <flash|overseer> [primary-model]
set -euo pipefail
cd "$(dirname "$0")/.."
STAGE="${1:?stage: flash | overseer}"
DATASETS=(seeded-v1 reverted-v1 clean-v1)
declare -A SAMPLE=([seeded-v1]=16 [reverted-v1]=10 [clean-v1]=14)
run() { uv run python -m evals.harness run "$@"; }

if [[ "$STAGE" == flash ]]; then
  for model in google/gemini-3.7-flash google/gemini-3.8-flash; do
    for ds in "${DATASETS[@]}"; do
      run --dataset "$ds" --mode agent --model "$model" --sample "${SAMPLE[$ds]}" \
          --overseer never --jobs 6
    done
  done
else
  model="${2:?primary model}"
  for ds in "${DATASETS[@]}"; do
    run --dataset "$ds" --mode agent --model "$model" --overseer always --jobs 6
  done
fi
echo "STAGE $STAGE DONE"
