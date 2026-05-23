# canswim-deck-eval-parser

Parse Canadian swimming **On-Deck Evaluation** PDFs into a structured spreadsheet, using a local vision LLM. Hand-filled scanned forms or natively-generated PDFs, both supported.

> **Status:** under construction. **Fillable PDFs (e.g. `eval-gen` output, online-form exports) parse end-to-end today.** Scanned PDFs go through a local vision model; that path is being built. See [docs/design/0001-initial-design.md](docs/design/0001-initial-design.md) and [the open issues](https://github.com/gavinbee/canswim-deck-eval-parser/issues).

## Quick start

```
# One-time setup (installs Ollama, pulls the vision and edit models, sets up .venv)
scripts/install.ps1     # Windows
scripts/install.sh      # macOS / Linux

# Parse a single PDF
python main.py path/to/eval.pdf
```

Output lands in `output/` as `<pdf-stem>.json` (canonical), plus a derived `.csv` and `.xlsx`.

## Supported templates

| Province | Template ID | Status |
|---|---|---|
| Ontario | `swim_ontario_v1` | implemented (v1) |
| Quebec | `swim_quebec_v1` | stub — [follow-up issue](https://github.com/gavinbee/canswim-deck-eval-parser/issues) |
| Alberta | `swim_alberta_v1` | stub |
| British Columbia | `swim_bc_v1` | stub |

Other provinces: please [file an issue](https://github.com/gavinbee/canswim-deck-eval-parser/issues/new) with a sample PDF.

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
