# Models

The parser uses two local models via Ollama:

| Role | Default tag | Used when |
|---|---|---|
| **Vision extraction** | `qwen2.5vl:7b` | scanned (or otherwise non-fillable) PDFs go through the vision model — one inference per page |
| **Text edit interpretation** | `qwen2.5:7b` | only when `--interactive` is passed; rewrites the canonical schema from natural-language user instructions |

Both are Apache 2.0 licensed. Both are pulled automatically on first run (see [installation.md](installation.md)).

## GPU-tier auto-pick

When `--vision-model` isn't set, the parser detects available VRAM and picks the highest tier that fits. The probe order is:

1. **NVIDIA** via `nvidia-smi --query-gpu=name,memory.free,memory.total`
2. **Apple Silicon** via `sysctl hw.memsize` (70% of unified RAM, conservative)
3. **Intel Mac** via `system_profiler SPDisplaysDataType` (best-effort)
4. Nothing detected → falls back to the CPU tier (Ollama runs on CPU; slow but functional)

Tier boundaries are on **free VRAM**, not total — if you have a 24 GB card but Photoshop is using 18 GB, you'll get the 7B tier:

| Tier | Free VRAM | Vision model | Edit model | Notes |
|---|---|---|---|---|
| `3b` | <6 GB or no GPU | `qwen2.5vl:3b` | `qwen2.5:3b` | Also the CPU fallback. ~30-60 s/page on CPU. |
| `7b` _(default)_ | 6–20 GB | `qwen2.5vl:7b` | `qwen2.5:7b` | The safe default. ~5-7 GB VRAM, ~5-10 s/page on a typical 8-12 GB GPU. |
| `32b` | 20–48 GB | `qwen2.5vl:32b` | `qwen2.5:7b` | Meaningful accuracy uplift on hard handwriting per OCRBench v2; ~3-5× slower per page. Edit model stays at 7B — no useful gain at 32B for this task. |
| `72b` | ≥48 GB | `qwen2.5vl:72b` | `qwen2.5:7b` | Diminishing returns for this task. Not recommended for v1 outside power-user setups. |

The CLI logs which tier it picked at startup, e.g. `Detected NVIDIA GeForce RTX 3070 with 6505 MB free (8192 MB total). Using qwen2.5vl:7b (7b tier).`

## Overriding the auto-pick

Pass `--vision-model <tag>` to force a specific model. Any tag pulled to Ollama works; the parser will still ensure-pull it if missing:

```
python main.py scan.pdf --vision-model qwen2.5vl:3b   # force the small tier
python main.py scan.pdf --vision-model qwen2.5vl:32b  # force the large tier
```

`--edit-model` similarly overrides the text model.

## Per-field confidence and the model floor

Every extracted value carries a model-reported `confidence` in `[0, 1]` (see [output-schema.md](output-schema.md)). To make the model emit those reliably, the vision call constrains decoding with a **JSON Schema** (passed to Ollama's `format=`), built from the canonical field list in `src/schema.py`. Without that constraint, smaller models flatten each field to a bare scalar and the confidence is lost — the parser then falls back to a neutral `0.5` and logs a warning (background: gh #45).

Confidence quality is **model-dependent**, and the schema can't fix that — it only guarantees the *shape*, not honest *numbers*:

| Model | Confidence behavior |
|---|---|
| `qwen2.5vl:3b` | Unreliable. Tends to stamp a single constant value on every field — no usable signal. Treat 3B output as "values only," not confidence-scored. |
| `qwen2.5vl:7b` _(default)_ | Coarse but useful. In practice a two-level signal — high (~0.9) on what it read confidently, lower (~0.6) on genuinely ambiguous cells (e.g. a crossed-out `successful`). Low values land on the rows worth a human's review. |

**Practical implication:** the confidence-driven features (low-confidence surfacing in `--interactive`, the `row_confidence` composite) are meaningful from the **7B tier up**. On the 3B tier, prefer `--review-all` over relying on confidence thresholds.

## Why this family, not something else

Reasoning is in [`design/0001-initial-design.md`](design/0001-initial-design.md#models--concrete-pinning). Short version: Qwen2.5-VL is currently the strongest open vision-language family on OCRBench v2 / DocVQA under 15 B params, has good handwriting performance, and the same model family covers both the vision and text-edit roles so users only deal with one ecosystem.

## Roadmap: Qwen3-VL

Qwen3-VL is the next generation of this family and is starting to land on Ollama. We pin to Qwen2.5-VL for v1 because it's mature today. The swap is tracked under the "Qwen3-VL swap" issue — it should be a one-line model-tag change in `src/gpu_detect.py` plus a regression run against the golden fixtures (when those land in #16).

## Implementation pointer

`src/gpu_detect.py` owns VRAM probing and tier selection. The `Tier` enum, `TIER_MODELS` map, and `_TIER_THRESHOLDS_MB` table are the single source of truth — change them in one place and everything downstream (CLI logging, model-pull list, fallback behavior) picks it up.
