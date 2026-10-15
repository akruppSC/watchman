#!/usr/bin/env bash
# Exploratory: the same agent run on seeded-v3 with `style = "google"` in user config.
# Run files are moved to evals/results/v3_google/ so they never replace the primary results.
# Needs docker access (`sg docker -c`). Not tuned on; reported as exploratory.
set -euo pipefail
cd "$(dirname "$0")/.."
H=anthropic/claude-haiku-5.5
TMP=$(mktemp -d)
uv run python - "$TMP/watchman.toml" <<'PY'
import sys
from pathlib import Path
from watchman_review.config import StyleGuide, load_config, save_user_config
cfg, _ = load_config(None)
cfg.review.style = StyleGuide.GOOGLE
save_user_config(cfg, Path(sys.argv[1]))
PY
export WATCHMAN_CONFIG="$TMP/watchman.toml"
touch "$TMP/marker"
for split in dev "test --final"; do
  uv run python -m evals.harness run --dataset seeded-v3 --split $split --mode agent --model "$H" --jobs 6
done
find evals/results/runs -newer "$TMP/marker" -name '*.json' -exec mv {} evals/results/v3_google/ \;
echo "V3 GOOGLE DONE"
