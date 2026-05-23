# PDF parsing notes

How the parser actually reads PDFs, plus PyMuPDF gotchas worth remembering. This is implementation reference — for the **why**, see the design docs.

## Two paths, one shape

The parser handles two flavours of input PDF:

1. **Fillable PDFs** with form widgets (e.g. `eval-gen` output, online-form exports). Read deterministically via `src.pdf_io.read_widgets` + `src.form_extract.extract_page`.
2. **Scanned/flat PDFs** with no widgets. Each page is rasterized to PNG bytes (`src.pdf_io.rasterize_page`) and handed to the vision model.

Both paths emit the same `src.form_extract.PageExtraction` shape (`meet` / `session` / `rows` dicts of `FieldValue`), so the merge → output pipeline doesn't care which path produced a page.

Form-field path is preferred whenever `src.pdf_io.has_form_fields(path)` returns `True`. The two paths are not mixed within a single PDF in v1 — if any page has widgets, every page goes through the form-field path.

## PyMuPDF (`fitz`) gotchas

### Widget names get a ` [NNN]` suffix on later pages

When the same widget name appears on more than one page of a PDF (which is exactly what `eval-gen` does — every page of the Swim Ontario form has a `Name of OfficialRow1`), PyMuPDF appends a space and the widget's object id in square brackets to disambiguate:

| Page | What PyMuPDF returns |
|---|---|
| 1 | `Name of OfficialRow1` |
| 2 | `Name of OfficialRow1 [226]` |
| 3 | `Name of OfficialRow1 [371]` (id varies) |

If we naively looked these up in a template's `widget_field_map` keyed by the bare name, **only page 1 would match**, and pages 2+ would come back empty (which is the bug this whole section exists to call out — we hit it in #2).

**`src.pdf_io._canonicalize_widget_name`** strips a trailing space + `[digits]` from every widget name in `read_widgets` so the rest of the pipeline only ever sees the bare form. The brackets-in-the-middle case (`"Some [weird] name"`) is preserved.

### File handles leak on Windows if you don't close

PyMuPDF doesn't enforce closing a `Document`. On Linux/macOS this is mostly fine because the GC eventually frees it. On Windows the file stays locked, which trips other tests trying to read or delete the same path.

**`src.pdf_io.open_pdf` is a context manager** — every read in the codebase goes through it. Don't bypass.

### `page.widgets()` can return `None`

For some malformed PDFs (and any non-form PDF), `page.widgets()` returns `None` instead of an empty iterator. `src.pdf_io.has_form_fields` and `read_widgets` defend against this with `(page.widgets() or ())`.

### Widget values for unfilled fields are `""`, not `None`

Text widgets that the user never filled in come back as empty strings. Check-boxes and radio buttons can come back as `bool` or `None`. `read_widgets` normalizes everything to `str`.

## Rasterization

`src.pdf_io.rasterize_page` returns PNG bytes (not a Pillow `Image`) so callers can hand them straight to Ollama's HTTP API without re-encoding.

- Default DPI: **200**. On a US Letter landscape page this gives ~1700×2200 pixels — large enough for the vision model to read handwriting clearly without hammering the model's context window.
- We render via `page.get_pixmap(matrix=fitz.Matrix(zoom, zoom), alpha=False)` then save through Pillow as PNG with `optimize=True`. PyMuPDF's own `pixmap.tobytes("png")` works but Pillow produces consistently smaller output.

## Page coordinates

PDF coordinate space is in **points** (1/72 of an inch). The Swim Ontario form is `(792, 612)` — landscape US Letter. `src.pdf_io.page_dimensions` returns `(width, height)` floats, which will eventually be used to compute crop boxes for per-row snippets in interactive mode.

## Templates and widget mapping

A `Template` (see `src/templates/base.py`) carries a `widget_field_map: dict[str, str]` that maps a widget name *as it appears on the form* to a canonical schema key. Per-row widgets use a `{i}` placeholder for the row index — `src.form_extract.extract_page` substitutes 1..`template.rows_per_page` at lookup time.

Authoring rule: **per-row widgets must include `{i}`, meet- and session-level widgets must not.** Violations are logged as warnings and ignored (see the `TestTemplateAuthoringWarnings` cases in `tests/test_form_extract.py`).
