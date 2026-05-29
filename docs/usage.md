# Usage

> **Status:** v1, form-field path only. Vision extraction, interactive review, and the agentic edit loop land in later issues. This page is updated as flags become functional.

## Quick start

```
python main.py path/to/eval.pdf
```

Outputs land in `./output/` as `<pdf-stem>.json`, `<pdf-stem>.csv`, and `<pdf-stem>.xlsx`. See [output-schema.md](output-schema.md) for the column contract.

## Flags

| Flag | Default | Status |
|---|---|---|
| *(positional)* `pdf` | required | the input PDF |
| `--output-dir <DIR>` | `output` | output directory (created if missing) |
| `--template <ID>` | `swim_ontario_v1` | provincial template to parse against. Becomes "auto-detect" once template detection lands ([#9](https://github.com/swimblocks/deck-eval-parser/issues/9)) |
| `-v` / `-vv` | `WARNING` | verbosity. `-v` = `INFO`, `-vv` = `DEBUG` |
| `--vision-model <TAG>` | — | reserved for the vision path ([#8](https://github.com/swimblocks/deck-eval-parser/issues/8)) |
| `--edit-model <TAG>` | — | reserved for interactive edits ([#13](https://github.com/swimblocks/deck-eval-parser/issues/13)) |
| `--no-cache` | — | reserved for vision-path caching ([#8](https://github.com/swimblocks/deck-eval-parser/issues/8)) |
| `--no-auto-pull` | — | reserved for Ollama runtime ([#6](https://github.com/swimblocks/deck-eval-parser/issues/6)) |
| `--interactive` | — | reserved for interactive review ([#12](https://github.com/swimblocks/deck-eval-parser/issues/12)) |
| `--review-all` | — | reserved for walk-every-eval mode ([#14](https://github.com/swimblocks/deck-eval-parser/issues/14)) |
| `--low-confidence-threshold <FLOAT>` | — | reserved for interactive review ([#12](https://github.com/swimblocks/deck-eval-parser/issues/12)) |

## Exit codes

| Code | Meaning |
|---|---|
| `0` | success |
| `1` | extraction failure — PDF has no fillable widgets (scanned), and the vision path isn't yet implemented |
| `2` | validation failure — PDF doesn't match the chosen template, or no recognisable evaluation rows |
| `3` | reserved for `--interactive` user abort |
| `4` | `MultiMeetError` — pages in the same PDF reference more than one meet (see [architecture.md](architecture.md#multi-page-reconciliation)) |

## Examples

```
# Default — produce JSON + CSV + XLSX in ./output/
python main.py session_3_evals.pdf

# Custom output directory
python main.py session_3_evals.pdf --output-dir ~/Desktop/evals

# Verbose logging for debugging
python main.py session_3_evals.pdf -vv
```

## What's not in v1

Scanned PDFs route through a local vision model (Qwen2.5-VL via Ollama). That path is the next several issues. Until then, scanned inputs exit with code 1 and a clear message.
