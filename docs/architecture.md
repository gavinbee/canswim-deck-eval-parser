# Architecture

> Source of truth for the **current** architecture. Updated whenever a non-trivial change lands. For the **why** behind a decision, see the relevant doc in [`docs/design/`](design/).

## What works today

| Component | Module | Status |
|---|---|---|
| Canonical schema | [`src/schema.py`](../src/schema.py) | implemented |
| Template registry | [`src/templates/__init__.py`](../src/templates/__init__.py) | implemented |
| Swim Ontario template | [`src/templates/swim_ontario_v1.py`](../src/templates/swim_ontario_v1.py) | implemented |
| Quebec / Alberta / BC templates | stubs in `src/templates/` | reserved (raise `NotImplementedError`) |
| PDF I/O (PyMuPDF wrapper) | [`src/pdf_io.py`](../src/pdf_io.py) | implemented |
| Form-field fast path | [`src/form_extract.py`](../src/form_extract.py) | implemented |
| Merge + multi-meet detection | [`src/merge.py`](../src/merge.py) | implemented |
| Output (JSON + CSV + XLSX) | [`src/output.py`](../src/output.py) | implemented |
| End-to-end CLI (both paths) | [`main.py`](../main.py) | implemented |
| Ollama runtime + lifecycle | [`src/ollama_runtime.py`](../src/ollama_runtime.py) | implemented |
| GPU detection + tier picker | [`src/gpu_detect.py`](../src/gpu_detect.py) | implemented |
| Vision extraction | [`src/vision_extract.py`](../src/vision_extract.py) | implemented |
| Template detection | [`src/template_detect.py`](../src/template_detect.py) | implemented |
| Interactive review / edit loop | — | pending — see [open issues](https://github.com/swimblocks/deck-eval-parser/issues) |

## How a parse runs

`main.py` picks one of two paths based on whether the PDF has fillable widgets (`pdf_io.has_form_fields`).

### Form-field path (fillable PDFs — fast, no model)

1. **Open** the PDF via `pdf_io.open_pdf` (context-managed `fitz.Document`).
2. For each page, **read widgets** via `pdf_io.read_widgets`, which returns a `{widget_name: value}` dict with PyMuPDF's `[NNN]` disambiguator suffix stripped. See [`pdf-parsing.md`](pdf-parsing.md).
3. Pass the widget dict plus the `Template` to `form_extract.extract_page` → a `PageExtraction` per page (confidence 1.0; trailing blank rows dropped).
4. Template defaults to `swim_ontario_v1` (or `--template`); no detection — we don't spin up a model just to classify a fillable form.
5. **Merge** then **write** (shared tail, below).

### Vision path (scanned / flat PDFs)

1. **Resolve the model**: `--vision-model`, else `gpu_detect` picks a tier from free VRAM (see [`models.md`](models.md)).
2. **Start Ollama** via the `OllamaDaemon` context manager — auto-starts the daemon if needed, ensures the model is pulled, stops the daemon on exit if we started it.
3. **Detect the template** from page 1 via `template_detect.detect_template` (unless `--template`). A recognized-but-stubbed province (e.g. Quebec) raises a helpful `NotImplementedError` (exit 2); a low-confidence / unknown result raises `TemplateDetectionError` (exit 2).
4. **Extract** each page with `vision_extract.extract_pdf` → `PageExtraction` list, cached to `<stem>.raw.json` (skip with `--no-cache`).
5. **Merge** with a `same_meet_checker` backed by the loaded vision model, so multi-page scans whose headers differ only by OCR noise get a real "same meet?" judgement instead of a spurious `MultiMeetError`.
6. **Write**.

### Shared tail (both paths)

- **Merge** via `src.merge.merge(pages, ...)` assembles the canonical `ParseResult`. Page 1's meet header is authoritative; later pages get `meet_match` = `confirmed` / `carried` / (model-judged) `confirmed`/`unknown`, or raise `MultiMeetError` (exit 4) on a `different` verdict. See [Multi-page reconciliation](#multi-page-reconciliation).
- **Write** via `src.output.write_all(result, output_dir)` — JSON canonical, plus derived CSV and XLSX (one `evaluations` sheet). See [`output-schema.md`](output-schema.md).

## Multi-page reconciliation

`src.merge` decides whether each page of a multi-page PDF agrees with page 1 about the meet identity. Three deterministic outcomes plus an LLM-mediated path for the hard cases:

| Page N>1 condition | `meet_match.value` | Confidence | LLM call? |
|---|---|---|---|
| All meet fields blank / absent | `carried` | 1.0 | no |
| Every non-blank value matches page 1 (case- and whitespace-insensitive) | `confirmed` | 1.0 | no |
| Values differ in a non-trivial way | depends on the `same_meet_checker` verdict | model's confidence | yes |
| `same_meet_checker` returned `different` | — | — | raise `MultiMeetError`, exit 4 |
| `same_meet_checker` returned `unknown` | `unknown` | model's confidence | yes — surfaces in interactive review |

`meet_match.confidence` is folded into `row_confidence` so a shaky page-N reconciliation drags every row of that page into the low-confidence review.

On the vision path the `same_meet_checker` is backed by the loaded vision model (`vision_extract.make_same_meet_checker`); on the form-field path no checker is passed, so the deterministic fast paths handle everything `eval-gen` produces and any genuine disagreement raises `MultiMeetError`.

## Key shapes

- **Canonical field constants** (`src.schema`): `MEET_FIELDS`, `SESSION_FIELDS`, `ROW_FIELDS`, `ALL_FIELDS` — the single source of truth for field names. Every module imports from here rather than using string literals.
- **`Template`** (`src.templates.base`): a frozen dataclass each province fills in (id, display_name, field_names, language, date_formats, vision_prompt_addendum, widget_field_map, rows_per_page).
- **`PageExtraction`** (`src.form_extract`): the converged shape both paths emit per page. Carries `page_number`, `meet`, `session`, `rows`.
- **`ParseResult`** (`src.schema`): the top-level canonical output (template_id, template_confidence, extraction_method, vision_model, edit_model, meet, evaluations). Serialises straight to canonical JSON via `dataclasses.asdict`. Not yet populated by anything; will be assembled by the merge module (issue #4).

## Pointers

- PyMuPDF / PDF specifics → [`pdf-parsing.md`](pdf-parsing.md)
- Swim Ontario template field reference → [`templates/swim_ontario.md`](templates/swim_ontario.md)
- Adding a new provincial template → [`templates/README.md`](templates/README.md) *(stub — will be filled in #15)*
- Original design rationale → [`design/0001-initial-design.md`](design/0001-initial-design.md)
