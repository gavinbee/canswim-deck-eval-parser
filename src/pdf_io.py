"""PyMuPDF helpers — the only module that talks to ``fitz`` directly.

Two responsibilities:

1. **Detect form fields.** If the input PDF has fillable widgets we use the
   fast path (``form_extract``). Otherwise we rasterize and route to the
   vision model.
2. **Rasterize pages to PNG bytes.** Used both by the vision extractor and
   the (future) template-detection pass on page 1.

Read-only — we never modify PDFs. Open every document with the path so
PyMuPDF mmaps the file rather than holding it in memory.
"""
from __future__ import annotations

import io
import logging
import re
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

import fitz  # PyMuPDF — module name is "fitz"
from PIL import Image

log = logging.getLogger(__name__)


# Default DPI for rasterization. 200 DPI on a standard letter-size page
# gives a ~1700x2200 PNG; large enough for the vision model to read
# handwriting clearly without blowing up Ollama's context window.
DEFAULT_DPI = 200


# When a multi-page PDF has duplicate widget names across pages (which is
# exactly what eval-gen produces — every page of the Swim Ontario form has
# a "Name of OfficialRow1"), PyMuPDF disambiguates by appending a space
# and a bracketed object id, e.g. ``"Name of OfficialRow1 [226]"``. We
# strip that suffix so widget names match what's in the template's
# widget_field_map.
_DUPLICATE_WIDGET_SUFFIX_RE = re.compile(r"\s\[\d+\]$")


def _canonicalize_widget_name(name: str) -> str:
    """Strip PyMuPDF's `` [NNN]`` duplicate-name suffix."""
    return _DUPLICATE_WIDGET_SUFFIX_RE.sub("", name)


@contextmanager
def open_pdf(path: str | Path) -> Iterator[fitz.Document]:
    """Open a PDF as a context-managed ``fitz.Document``.

    PyMuPDF doesn't enforce closing but it does leak file handles on
    Windows if you don't, which trips other tests that try to delete the
    same file. Always use this in a ``with``.
    """
    path = Path(path)
    if not path.is_file():
        raise FileNotFoundError(f"PDF not found: {path}")
    doc = fitz.open(path)
    try:
        yield doc
    finally:
        doc.close()


def page_count(path: str | Path) -> int:
    """Number of pages in the PDF."""
    with open_pdf(path) as doc:
        return len(doc)


def has_form_fields(path: str | Path) -> bool:
    """True if any page of the PDF has at least one fillable widget.

    Fillable PDFs (e.g. ``eval-gen`` output) can be parsed deterministically
    via the form-field fast path. Scanned PDFs have no widgets and must go
    through vision extraction.
    """
    with open_pdf(path) as doc:
        for page in doc:
            # ``page.widgets()`` returns an iterator (or None for some
            # malformed PDFs). Take the first element if any.
            for _ in (page.widgets() or ()):
                return True
    return False


def read_widgets(path: str | Path, page_index: int) -> dict[str, str]:
    """Extract ``{widget_name: value}`` for one page of a fillable PDF.

    The mapping is what the form-field fast path consumes. Values are
    always strings — PyMuPDF returns the empty string for unfilled fields
    rather than ``None``.

    Args:
        path: Path to the PDF.
        page_index: 0-based page index.
    """
    with open_pdf(path) as doc:
        if not (0 <= page_index < len(doc)):
            raise IndexError(
                f"page_index {page_index} out of range for {path} "
                f"({len(doc)} pages)"
            )
        page = doc[page_index]
        widgets: dict[str, str] = {}
        for w in (page.widgets() or ()):
            name = w.field_name
            value = w.field_value
            if name is None:
                continue
            canonical = _canonicalize_widget_name(name)
            # Coerce to str — PyMuPDF returns str for text widgets, but
            # check-boxes and radio buttons can return bool/None.
            widgets[canonical] = "" if value is None else str(value)
    return widgets


def rasterize_page(
    path: str | Path,
    page_index: int,
    dpi: int = DEFAULT_DPI,
) -> bytes:
    """Render one page to PNG bytes.

    Used by the vision extractor and by template detection. Returns bytes
    rather than a Pillow ``Image`` so callers can pass them straight to
    the Ollama HTTP API without re-encoding.
    """
    with open_pdf(path) as doc:
        if not (0 <= page_index < len(doc)):
            raise IndexError(
                f"page_index {page_index} out of range for {path} "
                f"({len(doc)} pages)"
            )
        page = doc[page_index]
        # PyMuPDF's default is 72 DPI; ``zoom`` scales linearly.
        zoom = dpi / 72.0
        pix = page.get_pixmap(matrix=fitz.Matrix(zoom, zoom), alpha=False)
        # Convert via Pillow so we get a real PNG with sensible
        # compression rather than PyMuPDF's raw output.
        img = Image.frombytes("RGB", (pix.width, pix.height), pix.samples)
        buf = io.BytesIO()
        img.save(buf, format="PNG", optimize=True)
        return buf.getvalue()


def page_dimensions(path: str | Path, page_index: int) -> tuple[float, float]:
    """Return ``(width, height)`` of a page in PDF points (72/inch).

    Useful for sanity checks and for computing crop boxes when we
    eventually want to show row-level snippets in interactive mode.
    """
    with open_pdf(path) as doc:
        if not (0 <= page_index < len(doc)):
            raise IndexError(
                f"page_index {page_index} out of range for {path} "
                f"({len(doc)} pages)"
            )
        rect = doc[page_index].rect
        return float(rect.width), float(rect.height)
