# Usage

> **Status:** both extraction paths work. Interactive review / edit loop (`--interactive`, `--review-all`) is the next milestone.

## Quick start

```
python main.py path/to/eval.pdf
```

- **Fillable PDFs** (eval-gen output, online-form exports) → fast deterministic form-field path. No model needed.
- **Scanned / flat PDFs** → local vision model (Qwen2.5-VL via Ollama). The daemon auto-starts and the model auto-pulls on first run.

Outputs land in `./output/` as `<pdf-stem>.json` (canonical), `<pdf-stem>.csv`, and `<pdf-stem>.xlsx`. See [output-schema.md](output-schema.md). On the vision path a `<pdf-stem>.raw.json` cache sidecar is also written.

## Flags

| Flag | Default | Notes |
|---|---|---|
| *(positional)* `pdf` | required | the input PDF |
| `--output-dir <DIR>` | `output` | output directory (created if missing) |
| `--template <ID>` | auto | force a provincial template. Default: auto-detect from page 1 on the vision path; `swim_ontario_v1` on the form-field path. Choices are implemented templates only. |
| `--vision-model <TAG>` | auto | Ollama vision model tag. Default: auto-picked from detected GPU VRAM (see [models.md](models.md)). Vision path only. |
| `--no-cache` | off | re-invoke the vision model even if a `<stem>.raw.json` cache exists. Vision path only. |
| `--no-auto-pull` | off | don't auto-pull missing Ollama models; error with the manual `ollama pull` command instead. Vision path only. |
| `-v` / `-vv` | `WARNING` | verbosity. `-v` = `INFO` (shows the picked model/tier, template, daemon lifecycle), `-vv` = `DEBUG`. |

Reserved for the next milestone (interactive review): `--interactive`, `--review-all`, `--edit-model`, `--low-confidence-threshold`.

## Exit codes

| Code | Meaning |
|---|---|
| `0` | success |
| `1` | extraction failure — Ollama not installed / unreachable, model missing under `--no-auto-pull`, or the vision model returned unparseable output |
| `2` | validation failure — PDF missing, template can't be identified (or is a not-yet-supported province), or no recognisable evaluation rows |
| `3` | reserved for `--interactive` user abort |
| `4` | `MultiMeetError` — pages reference more than one meet (see [architecture.md](architecture.md#multi-page-reconciliation)) |

## Examples

```
# Fillable PDF — fast path, no model
python main.py session_3_evals.pdf

# Scanned PDF — auto-detect template, auto-pick model for your GPU
python main.py data/scan.pdf -v

# Force a specific model and template, skip the cache
python main.py data/scan.pdf --vision-model qwen2.5vl:32b \
    --template swim_ontario_v1 --no-cache

# Air-gapped / pre-pulled: fail loudly instead of pulling
python main.py data/scan.pdf --no-auto-pull
```
