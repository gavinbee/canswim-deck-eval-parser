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
| Vision extraction, template detection, Ollama lifecycle, merge, output, CLI, interactive review | — | pending — see [open issues](https://github.com/gavinbee/canswim-deck-eval-parser/issues) |

## How a parse runs (form-field path)

Today only the form-field path is wired together:

1. **Open** the PDF via `pdf_io.open_pdf` (context-managed `fitz.Document`).
2. **Detect** whether it has fillable widgets via `pdf_io.has_form_fields`.
3. For each page, **read widgets** via `pdf_io.read_widgets`, which returns a `{widget_name: value}` dict with PyMuPDF's `[NNN]` disambiguator suffix stripped. See [`pdf-parsing.md`](pdf-parsing.md) for that and other gotchas.
4. Pass the widget dict plus the appropriate `Template` to `form_extract.extract_page`. It walks the template's `widget_field_map`, expanding `{i}` placeholders for per-row entries, and emits a `PageExtraction` (one `meet` dict, one `session` dict, and a list of `rows`, all of `FieldValue` with confidence 1.0). Trailing blank rows are dropped.
5. The result is a `list[PageExtraction]`.

The vision path (which will share the same `PageExtraction` output shape) is not yet wired in; see issue #8 onwards.

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
