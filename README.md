# canswim-deck-eval-parser

Parse Canadian swimming **On-Deck Evaluation** PDFs into a structured spreadsheet, using a local vision LLM. Hand-filled scanned forms or natively-generated PDFs, both supported.

> **Status:** under construction. **Both paths now run end-to-end:** fillable PDFs (e.g. `eval-gen` output, online-form exports) via a fast deterministic path, and scanned PDFs via a local vision model (Qwen2.5-VL through Ollama). Interactive review / correction is next. See [docs/design/0001-initial-design.md](docs/design/0001-initial-design.md) and [the open issues](https://github.com/swimblocks/deck-eval-parser/issues).

## Quick start

```bash
# 1. Install Ollama (one-time) — see docs/installation.md for full details
winget install Ollama.Ollama         # Windows
brew install ollama                  # macOS
curl -fsSL https://ollama.com/install.sh | sh   # Linux

# 2. Set up the parser
git clone https://github.com/swimblocks/deck-eval-parser.git
cd canswim-deck-eval-parser
python -m venv .venv && source .venv/bin/activate  # or .venv\Scripts\activate on Windows
pip install -r requirements.txt

# 3. Parse a PDF — models auto-pull on first run, daemon auto-starts/stops
python main.py path/to/eval.pdf
```

Output lands in `output/` as `<pdf-stem>.json` (canonical), plus a derived `.csv` and `.xlsx`.

One-command install scripts (`scripts/install.ps1` / `install.sh`) wrap all of step 1+2 and are tracked under [issue #11](https://github.com/swimblocks/deck-eval-parser/issues/11).

## Supported templates

| Province | Template ID | Status |
|---|---|---|
| Ontario | `swim_ontario_v1` | implemented (v1) |
| Quebec | `swim_quebec_v1` | stub — [follow-up issue](https://github.com/swimblocks/deck-eval-parser/issues) |
| Alberta | `swim_alberta_v1` | stub |
| British Columbia | `swim_bc_v1` | stub |

Other provinces: please [file an issue](https://github.com/swimblocks/deck-eval-parser/issues/new) with a sample PDF.

## Documentation

- [Installation](docs/installation.md) — install scripts, Ollama prerequisites, GPU / VRAM notes
- [Usage](docs/usage.md) — every CLI flag with examples, including `--interactive` and `--review-all`
- [Output schema](docs/output-schema.md) — the canonical JSON shape and how CSV/XLSX are derived from it
- [Models](docs/models.md) — vision and text model choices, GPU-tier auto-pick
- [Architecture](docs/architecture.md) — how the pipeline works end-to-end (CURRENT state)
- [PDF parsing notes](docs/pdf-parsing.md) — PyMuPDF gotchas, widget naming, rasterization choices
- [Templates](docs/templates/README.md) — how to add a provincial template
  - [Swim Ontario reference](docs/templates/swim_ontario.md)
- [Troubleshooting](docs/troubleshooting.md)
- [Design docs](docs/design/) — the *why*, ordered by date

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md). One issue → one branch (`{issue-number}-{slug}`) → one PR.

## License

[MIT](LICENSE) © 2026 Gavin Bee
