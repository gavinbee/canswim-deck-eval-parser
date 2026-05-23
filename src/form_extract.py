"""Deterministic fast path for fillable PDFs (e.g. eval-gen output).

When a PDF has fillable widgets we don't need the vision model at all —
we read values directly from the form fields via the template's
``widget_field_map``.

The intermediate ``PageExtraction`` dict this module produces is the same
shape that ``vision_extract`` emits for scanned pages, so the rest of the
pipeline (merge → output) doesn't care which path produced it.

Confidence is 1.0 for every field, since values are read verbatim from the
file rather than interpreted.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Optional

from . import schema as s
from .templates.base import Template

log = logging.getLogger(__name__)


# When the form-field path can answer with full certainty we use 1.0. The
# downstream low-confidence threshold defaults to 0.75, so these never
# surface for review.
_FORM_FIELD_CONFIDENCE = 1.0


@dataclass
class PageExtraction:
    """One page's worth of extracted data, before merge/reconciliation.

    Both the form-field and vision paths emit instances of this; merge.py
    consumes them and produces the canonical ``ParseResult``.

    The shape intentionally mirrors the canonical schema groupings so a
    later refactor can collapse the two paths into one if we want to.
    """

    page_number: int
    # Meet-level. Stable across pages of one PDF (page 1 is authoritative).
    meet: dict[str, s.FieldValue] = field(default_factory=dict)
    # Session-level. May vary per page within a single PDF.
    session: dict[str, s.FieldValue] = field(default_factory=dict)
    # One dict per official row. Empty trailing rows are stripped.
    rows: list[dict[str, s.FieldValue]] = field(default_factory=list)


def extract_page(
    widgets: dict[str, str],
    template: Template,
    page_number: int,
) -> PageExtraction:
    """Pull a ``PageExtraction`` out of one page's raw widget map.

    Args:
        widgets:
            ``{widget_name: value}`` for the page, typically from
            ``pdf_io.read_widgets``.
        template:
            The provincial template that tells us which widget belongs to
            which canonical field, including the ``{i}`` placeholder for
            per-row widgets.
        page_number:
            1-based page number for the output dict.

    Returns:
        A ``PageExtraction``. Fully-empty trailing rows are dropped —
        we keep a row only if at least one of its fields has a non-blank
        value, so a half-full page doesn't look like nine officials with
        blank names.
    """
    meet_keys = set(s.MEET_FIELDS)
    session_keys = set(s.SESSION_FIELDS)
    row_keys = set(s.ROW_FIELDS)

    result = PageExtraction(page_number=page_number)

    # Single-widget (non-row) fields.
    for widget_name, canonical in template.widget_field_map.items():
        if "{i}" in widget_name:
            continue  # handled below
        raw = widgets.get(widget_name)
        if raw is None:
            continue  # widget not present on this page
        # The form-field path emits even blank values: downstream merge
        # will use blank meet-level values to decide whether to "carry"
        # from page 1 vs "confirm".
        fv = s.FieldValue(value=raw, confidence=_FORM_FIELD_CONFIDENCE)
        if canonical in meet_keys:
            result.meet[canonical] = fv
        elif canonical in session_keys:
            result.session[canonical] = fv
        else:
            # The template's widget map should only point single-widget
            # entries at meet- or session-level keys. A row-level key with
            # no ``{i}`` is a template authoring bug; warn and drop it
            # rather than silently put it somewhere weird.
            log.warning(
                "Template %r maps non-row widget %r to row-level field %r; "
                "ignoring on page %d.",
                template.id, widget_name, canonical, page_number,
            )

    # Per-row fields.
    for row_index in range(1, template.rows_per_page + 1):
        row: dict[str, s.FieldValue] = {}
        for widget_template, canonical in template.widget_field_map.items():
            if "{i}" not in widget_template:
                continue
            if canonical not in row_keys:
                # Template authoring bug — a per-row widget pointing at a
                # non-row field. Warn once and move on.
                log.warning(
                    "Template %r maps per-row widget %r to non-row field "
                    "%r; ignoring.",
                    template.id, widget_template, canonical,
                )
                continue
            widget_name = widget_template.replace("{i}", str(row_index))
            raw = widgets.get(widget_name)
            if raw is None:
                continue
            row[canonical] = s.FieldValue(
                value=raw,
                confidence=_FORM_FIELD_CONFIDENCE,
            )
        if _row_has_content(row):
            result.rows.append(row)

    return result


def extract_pdf(
    pdf_path: str,
    template: Template,
) -> list[PageExtraction]:
    """Run the form-field fast path over every page of a fillable PDF.

    Convenience wrapper around ``pdf_io.read_widgets`` + ``extract_page``
    so callers (CLI, tests) don't have to thread page indices.

    The page count is taken from ``pdf_io.page_count`` to avoid holding
    two ``fitz.Document`` handles open at once.
    """
    from . import pdf_io  # local import keeps the module's surface clear

    pages: list[PageExtraction] = []
    n = pdf_io.page_count(pdf_path)
    for i in range(n):
        widgets = pdf_io.read_widgets(pdf_path, i)
        pages.append(extract_page(widgets, template, page_number=i + 1))
    return pages


def _row_has_content(row: dict[str, s.FieldValue]) -> bool:
    """True if any of the row's fields carries a non-blank value.

    Blank-trailing-rows is the common case on a partial page (e.g. 7
    officials on a 9-row template). We drop them so they don't show up
    downstream as ghost officials with empty names.
    """
    for fv in row.values():
        v = fv.value
        if isinstance(v, str):
            if v.strip():
                return True
        elif v is not None and v is not False:
            return True
    return False


def _is_blank_field(fv: Optional[s.FieldValue]) -> bool:
    """Helper used by callers that want to know whether a single field's
    value is effectively empty. Kept private to this module for now."""
    if fv is None:
        return True
    v = fv.value
    if isinstance(v, str):
        return not v.strip()
    return v is None
