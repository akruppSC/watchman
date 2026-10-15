#!/usr/bin/env bash
# Dev-split ablation used for milestone 3. Re-runnable; LLM responses are cached under
# evals/.cache/llm, so a rerun of unchanged requests costs nothing.
#
#   Ablation 1: baseline vs deterministic vs agent
#   Ablation 2: agent with vs without Layer 1 evidence (agent vs agent_no_l1)
#   Model tiers: Haiku 5.5 on the full dev split; GPT-5 on a 40-item stratified sample;
#                Opus 4.6 on a 20-item sample (a subset of the 40).
#
# Usage: evals/ablation.sh [haiku|tiers|all]   (default: all)
# Needs docker access (run under `sg docker -c` if your login predates the docker group).
set -euo pipefail
cd "$(dirname "$0")/.."
STAGE="${1:-all}"

H=anthropic/claude-haiku-5.5
G=openai/gpt-5
O=anthropic/claude-opus-4.6
DATASETS=(seeded-v1 reverted-v1 clean-v1)
run() { uv run python -m evals.harness run "$@"; }

declare -A G_SAMPLE=([seeded-v1]=16 [reverted-v1]=10 [clean-v1]=14)
declare -A O_SAMPLE=([seeded-v1]=8 [reverted-v1]=5 [clean-v1]=7)

if [[ "$STAGE" == haiku || "$STAGE" == all ]]; then
  for ds in "${DATASETS[@]}"; do
    run --dataset "$ds" --mode deterministic --jobs 5
  done
  for mode in baseline agent agent_no_l1; do
    for ds in "${DATASETS[@]}"; do
      run --dataset "$ds" --mode "$mode" --model "$H" --jobs 6
    done
  done
fi
if [[ "$STAGE" == tiers || "$STAGE" == all ]]; then
  for mode in baseline agent; do
    for ds in "${DATASETS[@]}"; do
      run --dataset "$ds" --mode "$mode" --model "$G" --sample "${G_SAMPLE[$ds]}" --jobs 6
    done
  done
  for ds in "${DATASETS[@]}"; do
    run --dataset "$ds" --mode agent --model "$O" --sample "${O_SAMPLE[$ds]}" --jobs 6
  done
fi
uv run python -m evals.harness report
echo "ABLATION STAGE $STAGE DONE"
