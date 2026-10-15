# Experiment 3: Google-style supervision

## Worker `openrouter/anthropic/claude-haiku-5.5`: alone vs. Watchman with `style = google`

18 completed runs (9 paired); spend: worker $0.19, reviewer $0.49. Cells are means per run; the last column counts pairs where supervised was lower / higher (exact sign test on discordant pairs).

| metric | alone | supervised | lower / higher |
|---|---|---|---|
| pylint messages, Google rc (primary) | 3.33 | 1.89 | 5 / 1 (p = 0.22) |
| pylint messages per 100 added lines | 2.02 | 0.76 | 8 / 1 (p = 0.04) |
| public API with a docstring (share) | 0.42 | 0.73 | 1 / 7 (p = 0.07) |
| public functions fully annotated (share) | 1.00 | 1.00 | 0 / 0 (p = 1.00) |
| functions over 40 lines | 0.22 | 0.11 | 1 / 0 (p = 1.00) |
| functions over a quality limit (40/60-line, 10 complexity, ...) | 0.33 | 0.11 | 2 / 0 (p = 0.50) |
| production lines added | 167.56 | 199.44 | 2 / 7 (p = 0.18) |
| tests added | 19.78 | 23.44 | 2 / 7 (p = 0.18) |

- alone: 9/9 runs with passing own tests.

- watchman: 9/9 runs with passing own tests.

Most common pylint messages (total over runs):

| message | alone | supervised |
|---|---|---|
| line-too-long | 19 | 10 |
| missing-module-docstring | 3 | 3 |
| invalid-name | 3 | 3 |
| missing-class-docstring | 5 | 0 |
| unused-argument | 0 | 1 |
