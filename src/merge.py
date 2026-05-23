"""Combine per-page ``PageExtraction`` objects into a single ``ParseResult``.

This is where the form-field and vision paths converge. Inputs come in
as a ``list[PageExtraction]`` (one per page); output is the canonical
``ParseResult`` that ``src.output`` serialises.

Responsibilities:

1. **Meet-header reconciliation across pages.** Page 1 is authoritative.
   Later pages may have blank, partial, or repeated meet-level fields.
   See "Reconciliation" below for the full algorithm — and the design
   doc §Multi-page PDF semantics for the rationale.

2. **Value coercion.** The form-field path emits every value as a string
   widget read. Schema types want booleans on ``successful`` and ints on
   ``session_number``. Merge does that conversion in one place so neither
   ``form_extract`` nor ``vision_extract`` has to know about typed schema
   columns.

3. **Row-confidence composite.** ``row_confidence`` is the minimum of all
   non-null field confidences on the row plus ``meet_match.confidence``.
   Min is the harshest aggregator — a single bad cell drops the row into
   review even if everything else is clean — which is exactly what we
   want for the human-in-the-loop interactive review.

## Reconciliation

For each page N>1, decide ``meet_match``:

* All page-N meet-level fields blank or absent → ``carried`` (1.0).
  Page 1 values are reused unchanged.
* Every non-blank page-N meet-level field is **identical** to page 1's
  value → ``confirmed`` (1.0). No model needed for the fast path; this
  is what eval-gen's output looks like.
* Page N has non-blank meet-level fields that *don't* exactly match
  page 1 → ask the ``same_meet_checker`` callable for a verdict
  + confidence:

  * ``"same"`` → ``confirmed`` with the model's confidence.
  * ``"different"`` → raise ``MultiMeetError`` (exit code 4).
  * ``"unknown"`` → ``unknown`` with the model's confidence; logged as
    a warning. The row stays in the output and surfaces in interactive
    mode via the low-confidence threshold.

If no ``same_meet_checker`` is provided and we hit the third case,
merge defaults to a ``MultiMeetError`` — better to fail fast than
silently merge mismatched data. The CLI plumbs in a checker that calls
``qwen2.5:7b`` via Ollama once the runtime lands (#6).
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Callable, Literal, Optional, Protocol

from . import schema as s
from .form_extract import PageExtraction

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Public surface: protocol for the LLM "same meet?" check + exceptions.
# ---------------------------------------------------------------------------


MeetFields = dict[str, s.FieldValue]
"""``{canonical_key: FieldValue}`` for meet-level fields on one page."""


SameMeetVerdictKind = Literal["same", "different", "unknown"]


@dataclass(frozen=True)
class SameMeetVerdict:
    """What the ``same_meet_checker`` callable returns.

    The caller (merge) decides how to act on each kind:
    ``same`` → confirmed, ``different`` → MultiMeetError,
    ``unknown`` → carry forward with a warning.
    """

    verdict: SameMeetVerdictKind
    confidence: float


class SameMeetChecker(Protocol):
    """A callable that judges whether two pages refer to the same meet."""

    def __call__(
        self,
        page_one: MeetFields,
        page_n: MeetFields,
    ) -> SameMeetVerdict: ...


class MultiMeetError(Exception):
    """Raised when pages in a single PDF reference more than one meet.

    Exit code 4 (per design doc §CLI). The CLI catches this and prints
    a human-readable diagnostic listing both meets' identifiers.
    """

    def __init__(
        self,
        page_one: MeetFields,
        page_n: MeetFields,
        page_n_index: int,
        confidence: float,
    ) -> None:
        self.page_one = page_one
        self.page_n = page_n
        self.page_n_index = page_n_index
        self.confidence = confidence
        super().__init__(self._format())

    def _format(self) -> str:
        def _summary(m: MeetFields) -> str:
            parts = []
            for key in (s.COMPETITION_NAME, s.HOST_CLUB, s.COC):
                fv = m.get(key)
                if fv is not None and _has_content(fv.value):
                    parts.append(f"{key}={fv.value!r}")
            return ", ".join(parts) or "(blank)"

        return (
            f"Pages reference more than one meet "
            f"(page-1 vs page-{self.page_n_index}, "
            f"confidence={self.confidence:.2f}):\n"
            f"  page 1:        {_summary(self.page_one)}\n"
            f"  page {self.page_n_index:<8d}{_summary(self.page_n)}"
        )


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------


def merge(
    pages: list[PageExtraction],
    *,
    source_pdf: str,
    template_id: str,
    template_confidence: float,
    extraction_method: s.ExtractionMethod,
    vision_model: Optional[str] = None,
    edit_model: Optional[str] = None,
    same_meet_checker: Optional[SameMeetChecker] = None,
) -> s.ParseResult:
    """Assemble a ``ParseResult`` from per-page extractions.

    Args:
        pages: One ``PageExtraction`` per page of the source PDF, in order.
        source_pdf: Filename of the input (typically the basename, not
            a full path — written verbatim to the canonical JSON).
        template_id: Detected template id (e.g. ``"swim_ontario_v1"``).
        template_confidence: Confidence from template detection.
        extraction_method: ``"vision"``, ``"form_field"``, or ``"mixed"``.
        vision_model: Tag passed to Ollama for vision extraction, or
            ``None`` on a pure form-field run.
        edit_model: Tag used in interactive mode, or ``None``.
        same_meet_checker: Optional callable to judge same-meet between
            non-identical page headers. If omitted, merge raises
            ``MultiMeetError`` rather than silently accepting a mismatch.

    Raises:
        MultiMeetError: If pages reference more than one meet.
        ValueError: If ``pages`` is empty.
    """
    if not pages:
        raise ValueError("merge() requires at least one page")

    result = s.ParseResult(
        source_pdf=source_pdf,
        template_id=template_id,
        template_confidence=template_confidence,
        extraction_method=extraction_method,
        vision_model=vision_model,
        edit_model=edit_model,
        meet=_build_meet_header(pages[0]),
    )

    for page in pages:
        meet_match = _reconcile_page(
            page, pages[0], same_meet_checker=same_meet_checker,
        )
        for i, row in enumerate(page.rows, start=1):
            evaluation = _build_evaluation(
                page=page,
                row=row,
                row_index=i,
                meet_match=meet_match,
            )
            result.evaluations.append(evaluation)

    return result


# ---------------------------------------------------------------------------
# Meet header
# ---------------------------------------------------------------------------


def _build_meet_header(page_one: PageExtraction) -> s.MeetHeader:
    """Page 1's meet-level fields become the canonical meet header."""
    return s.MeetHeader(
        competition_name=page_one.meet.get(s.COMPETITION_NAME),
        host_club=page_one.meet.get(s.HOST_CLUB),
        coc=page_one.meet.get(s.COC),
    )


def _reconcile_page(
    page: PageExtraction,
    page_one: PageExtraction,
    *,
    same_meet_checker: Optional[SameMeetChecker],
) -> s.MeetMatch:
    """Decide page N's ``meet_match`` relative to page 1.

    Page 1 itself is always ``authoritative``.
    """
    if page.page_number == page_one.page_number:
        return s.MeetMatch(value="authoritative", confidence=1.0)

    # Fast paths first — they don't need an LLM.
    if _all_blank(page.meet):
        return s.MeetMatch(value="carried", confidence=1.0)

    if _exact_match(page.meet, page_one.meet):
        return s.MeetMatch(value="confirmed", confidence=1.0)

    # Slow path — page N has non-blank fields that disagree (or differ in
    # capitalization, whitespace, abbreviation, etc.). The LLM decides.
    if same_meet_checker is None:
        raise MultiMeetError(
            page_one=page_one.meet,
            page_n=page.meet,
            page_n_index=page.page_number,
            confidence=0.0,
        )

    verdict = same_meet_checker(page_one.meet, page.meet)
    if verdict.verdict == "same":
        return s.MeetMatch(value="confirmed", confidence=verdict.confidence)
    if verdict.verdict == "different":
        raise MultiMeetError(
            page_one=page_one.meet,
            page_n=page.meet,
            page_n_index=page.page_number,
            confidence=verdict.confidence,
        )
    # "unknown" — carry forward but flag for human review.
    log.warning(
        "Page %d meet-level fields could not be confidently matched to "
        "page 1 (model confidence %.2f). Carrying page 1 values forward; "
        "row will surface in interactive review.",
        page.page_number, verdict.confidence,
    )
    return s.MeetMatch(value="unknown", confidence=verdict.confidence)


# ---------------------------------------------------------------------------
# Per-row assembly
# ---------------------------------------------------------------------------


def _build_evaluation(
    *,
    page: PageExtraction,
    row: dict[str, s.FieldValue],
    row_index: int,
    meet_match: s.MeetMatch,
) -> s.Evaluation:
    """Turn one ``PageExtraction`` row into an ``Evaluation``."""
    successful = _coerce_successful(row.get(s.SUCCESSFUL))
    session_number = _coerce_session_number(page.session.get(s.SESSION_NUMBER))

    ev = s.Evaluation(
        source_page=page.page_number,
        row_index=row_index,
        meet_match=meet_match,
        session_number=session_number,
        date_session=page.session.get(s.DATE_SESSION),
        competition_coordinator=page.session.get(s.COMPETITION_COORDINATOR),
        cc_level=page.session.get(s.CC_LEVEL),
        official_name=row.get(s.OFFICIAL_NAME),
        club=row.get(s.CLUB),
        position=row.get(s.POSITION),
        lane_number=row.get(s.LANE_NUMBER),
        times_worked_position=row.get(s.TIMES_WORKED_POSITION),
        mentor=row.get(s.MENTOR),
        level=row.get(s.LEVEL),
        successful=successful,
    )
    ev.row_confidence = _row_confidence(ev, meet_match)
    return ev


def _coerce_successful(
    fv: Optional[s.FieldValue],
) -> Optional[s.FieldValue]:
    """Convert a raw ``successful`` cell into a typed FieldValue.

    Vision path emits ``True`` / ``False`` / ``None`` already; passes
    through unchanged. Form-field path emits a string (widget value):

    * empty / whitespace → ``None`` (genuinely unknown — eval may not
      have happened yet, or evaluator left it blank as a "no"; we can't
      tell deterministically from form fields alone).
    * any non-empty string → ``True`` (initials are present, eval was
      signed off).
    """
    if fv is None:
        return None
    v = fv.value
    if isinstance(v, bool) or v is None:
        return fv  # vision-style typed value
    if isinstance(v, str):
        return s.FieldValue(
            value=True if v.strip() else None,
            confidence=fv.confidence,
            rationale=fv.rationale,
            source=fv.source,
        )
    # Unexpected type — log and let it through as-is so we don't lose data.
    log.warning(
        "Unexpected type %s for successful field; leaving as-is.", type(v).__name__,
    )
    return fv


def _coerce_session_number(
    fv: Optional[s.FieldValue],
) -> Optional[s.FieldValue]:
    """Coerce string session numbers to int when straightforward.

    Form-field path will rarely populate this — Swim Ontario buries the
    session number inside the ``Date  Session`` free-text field. Vision
    path emits ints directly. This is defensive: if a future template
    has a dedicated session-number widget, the string-to-int conversion
    works without further plumbing.
    """
    if fv is None or fv.value is None:
        return fv
    if isinstance(fv.value, int):
        return fv
    if isinstance(fv.value, str):
        v = fv.value.strip()
        if v.isdigit():
            return s.FieldValue(
                value=int(v),
                confidence=fv.confidence,
                rationale=fv.rationale,
                source=fv.source,
            )
    return fv


# ---------------------------------------------------------------------------
# Confidence composite
# ---------------------------------------------------------------------------


_FIELDS_FOR_ROW_CONFIDENCE: tuple[str, ...] = (
    "session_number",
    "date_session",
    "competition_coordinator",
    "cc_level",
    "official_name",
    "club",
    "position",
    "lane_number",
    "times_worked_position",
    "mentor",
    "level",
    "successful",
)


def _row_confidence(ev: s.Evaluation, meet_match: s.MeetMatch) -> float:
    """Composite confidence used to surface low-confidence rows for review.

    Min across every populated field's confidence, plus
    ``meet_match.confidence`` (which is folded in so a shaky page-N
    reconciliation drops every row of that page into review even when
    the per-field reads are clean).
    """
    confidences = [meet_match.confidence]
    for field in _FIELDS_FOR_ROW_CONFIDENCE:
        fv: Optional[s.FieldValue] = getattr(ev, field, None)
        if fv is None:
            continue
        confidences.append(fv.confidence)
    return min(confidences) if confidences else 1.0


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _has_content(value: object) -> bool:
    """True if a FieldValue.value carries non-blank information."""
    if value is None:
        return False
    if isinstance(value, str):
        return bool(value.strip())
    return True


def _all_blank(meet: MeetFields) -> bool:
    """True if every meet-level FieldValue in the dict is blank/absent."""
    for fv in meet.values():
        if _has_content(fv.value):
            return False
    return True


def _normalise(value: object) -> str:
    """For exact-match comparisons: trimmed, case-folded string form."""
    if value is None:
        return ""
    return str(value).strip().casefold()


def _exact_match(page_n: MeetFields, page_one: MeetFields) -> bool:
    """True if every non-blank field on page N matches page 1 verbatim.

    Page N is allowed to leave fields blank (those are deferred to the
    ``carried``/``confirmed`` decision earlier). But every value page N
    *does* populate has to agree with page 1 — modulo trimming and
    case-folding, since OCR / form-fill whitespace is noise.
    """
    for key, fv_n in page_n.items():
        if not _has_content(fv_n.value):
            continue
        fv_one = page_one.get(key)
        if fv_one is None or not _has_content(fv_one.value):
            # Page N has a value page 1 lacks — that's a disagreement.
            return False
        if _normalise(fv_n.value) != _normalise(fv_one.value):
            return False
    return True
