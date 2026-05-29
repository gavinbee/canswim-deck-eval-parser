# Troubleshooting

## Vision path: `GGML_ASSERT(a->ne[2] * 4 == b->ne[0]) failed` (HTTP 500)

**Symptom.** A scanned PDF errors during extraction with an Ollama 500 and a message containing `GGML_ASSERT(a->ne[2] * 4 == b->ne[0]) failed`. Template detection may succeed first, then the per-page extraction crashes.

**Cause.** This is a **known regression in Ollama ≥ 0.13.x** affecting Qwen2.5-VL (and other vision models) on CUDA. It is *not* a problem with your PDF or this tool — the projector matmul assertion fires inside Ollama's own runtime.

- [ollama#13630 — GGML_ASSERT crash with Qwen2.5-VL on CUDA (works on 0.12.x)](https://github.com/ollama/ollama/issues/13630)
- [ollama#14171 — same assert with glm-ocr](https://github.com/ollama/ollama/issues/14171)

**Fix / workaround.**
- **Downgrade Ollama to the 0.12.x line**, where Qwen2.5-VL works on CUDA. This is the most reliable fix today.
- Or track the issues above and move to a newer release once the regression is fixed upstream.

## Vision path: model runs 100% on CPU (very slow) on an 8 GB GPU

**Symptom.** `ollama ps` shows the model on `100% CPU` with a `SIZE` larger than your VRAM (e.g. `qwen2.5vl:3b … 10 GB … 100% CPU` on an 8 GB card). Extraction takes many minutes per page with high CPU and idle GPU.

**Cause.** A compute-graph memory-estimation change in **Ollama ≥ 0.13.4** over-estimates the memory a Qwen2.5-VL model needs, so it no longer fits the estimator's budget on an 8 GB GPU and Ollama falls back entirely to CPU.

- [ollama#13687 — qwen2.5vl:3b no longer runs on 8 GB GPUs since 0.13.4](https://github.com/ollama/ollama/issues/13687)

**Fix / workaround.**
- **Downgrade Ollama to 0.12.x** (same fix as the assert above — both regressions arrived together).
- Reducing the rasterized image size helps the footprint a little (the parser already caps the long edge to 1600 px; `pdf_io.rasterize_page(..., max_edge_px=…)` is tunable) but does not overcome the estimator regression on its own.
- A larger-VRAM GPU sidesteps the estimator fallback.

> **Why the tier picker still suggests 7B for 8 GB cards:** `gpu_detect` maps free VRAM to a model tier assuming a *working* Ollama. On a regressed Ollama even the 3B model won't GPU-offload on 8 GB. Once you're on a good Ollama version (0.12.x), the tier picker's mapping holds.

## Checking your Ollama version

```
ollama --version
```

If it reports 0.13.x or newer and you hit either symptom above on an NVIDIA card, the 0.12.x downgrade is the current remedy.

## Ollama not found / daemon won't start

See [installation.md](installation.md#troubleshooting-install-issues).

## Scanned PDF returns "no evaluation rows"

The vision model didn't find any official-rows it was confident about. Check the scan quality (very faint or skewed scans are hard), confirm the right template was detected (run with `-v`), and consider a higher-resolution scan. If the form is a province we don't support yet, template detection will say so.
