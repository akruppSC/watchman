# Experiment 3: Google-style supervision

## Worker `openrouter/anthropic/claude-haiku-5.5`: alone vs. Watchman with `style = google`

2 completed runs (1 paired); spend: worker $0.02, reviewer $0.02. Cells are means per run; the last column counts pairs where supervised was lower / higher (exact sign test on discordant pairs).

| metric | alone | supervised | lower / higher |
|---|---|---|---|
| pylint messages, Google rc (primary) | 2.00 | 7.00 | 0 / 1 (p = 1.00) |
| pylint messages per 100 added lines | 1.06 | 2.88 | 0 / 1 (p = 1.00) |
| public API with a docstring (share) | 0.18 | 0.77 | 0 / 1 (p = 1.00) |
| public functions fully annotated (share) | 1.00 | 1.00 | 0 / 0 (p = 1.00) |
| functions over 40 lines | 0.00 | 1.00 | 0 / 1 (p = 1.00) |
| functions over a quality limit (40/60-line, 10 complexity, ...) | 0.00 | 1.00 | 0 / 1 (p = 1.00) |
| production lines added | 188.00 | 243.00 | 0 / 1 (p = 1.00) |
| tests added | 21.00 | 24.00 | 0 / 1 (p = 1.00) |

- alone: 1/1 runs with passing own tests.

- watchman: 1/1 runs with passing own tests.

Most common pylint messages (total over runs):

| message | alone | supervised |
|---|---|---|
| line-too-long | 0 | 5 |
| missing-module-docstring | 1 | 1 |
| invalid-name | 1 | 1 |
