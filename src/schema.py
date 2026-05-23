"""Canonical, template-agnostic schema for parsed deck-evaluation data.

This module is the single source of truth for the **canonical field names**
used everywhere downstream (extraction, merge, output, review). Each
provincial template's `field_names` dict maps these canonical keys to the
labels / widget names that appear on that specific template.

The schema is the union of fields any template can populate; fields a given
template doesn't have are simply `None` in output.

The dataclasses here represent the in-memory shape of one parse. They
serialise via `dataclasses.asdict` into the canonical JSON described in
``docs/design/0001-initial-design.md`` §Output.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal, Optional


# ---------------------------------------------------------------------------
# Canonical field-name constants. Import these everywhere instead of using
# string literals so renames are a single-file change.
# ---------------------------------------------------------------------------

# Meet-level (expected stable across pages of a single PDF).
COMPETITION_NAME = "competition_name"
HOST_CLUB = "host_club"
COC = "coc"

# Session-level (may vary per page within a multi-session PDF).
COMPETITION_COORDINATOR = "competition_coordinator"
CC_LEVEL = "cc_level"               # The Competition Coordinator's level.
DATE_SESSION = "date_session"
SESSION_NUMBER = "session_number"
PAGE_NUMBER = "page_number"
PAGE_OF = "page_of"

# Per-official row.
OFFICIAL_NAME = "official_name"
CLUB = "club"
POSITION = "position"
LANE_NUMBER = "lane_number"
TIMES_WORKED_POSITION = "times_worked_position"
MENTOR = "mentor"                   # Mentor / signing-off official's name.
LEVEL = "level"                     # The MENTOR's officiating level, not
                                    # the level of the official being
                                    # evaluated.
SUCCESSFUL = "successful"


MEET_FIELDS: tuple[str, ...] = (
    COMPETITION_NAME,
    HOST_CLUB,
    COC,
)

SESSION_FIELDS: tuple[str, ...] = (
    COMPETITION_COORDINATOR,
    CC_LEVEL,
    DATE_SESSION,
    SESSION_NUMBER,
    PAGE_NUMBER,
    PAGE_OF,
)

ROW_FIELDS: tuple[str, ...] = (
    OFFICIAL_NAME,
    CLUB,
    POSITION,
    LANE_NUMBER,
    TIMES_WORKED_POSITION,
    MENTOR,
    LEVEL,
    SUCCESSFUL,
)

ALL_FIELDS: tuple[str, ...] = MEET_FIELDS + SESSION_FIELDS + ROW_FIELDS

# How many official-rows a single page of any current template can hold.
# Swim Ontario v1 is 9; if a future template differs this stays here as the
# upper bound for in-memory allocation but each template can carry its own
# `rows_per_page` value.
MAX_ROWS_PER_PAGE = 9


# ---------------------------------------------------------------------------
# Output dataclasses. Mutated in place by the merge and (interactive) edit
# steps, then serialised via `dataclasses.asdict`.
# ---------------------------------------------------------------------------


@dataclass
class FieldValue:
    """A single extracted value plus the model's self-reported confidence.

    ``value`` is whatever the field carries — most often a string, but
    booleans (e.g. ``successful``) and ints (e.g. ``session_number``) also
    appear. ``rationale`` is optional free text the model may emit to explain
    a judgement call (used today for ``successful`` to record why the model
    decided sign-off was present, absent, or ambiguous).
    """

    value: Any
    confidence: float
    rationale: Optional[str] = None
    # ``source`` is used by ``session_number`` to record where the model
    # found the answer — "form", "filename", "form+filename", "unknown".
    source: Optional[str] = None


MeetMatchKind = Literal["authoritative", "confirmed", "carried", "unknown"]


@dataclass
class MeetMatch:
    """Per-page record of whether this page is reconciled to page 1's meet.

    Page 1 is ``authoritative``. Later pages are ``confirmed`` (model said
    "same meet"), ``carried`` (page had no meet-level fields so we carried
    forward deterministically), or ``unknown`` (model wasn't sure). A
    ``different`` verdict raises ``MultiMeetError`` and never lands here.
    """

    value: MeetMatchKind
    confidence: float


@dataclass
class MeetHeader:
    """Meet-level fields — taken from page 1 and applied to every row."""

    competition_name: Optional[FieldValue] = None
    host_club: Optional[FieldValue] = None
    coc: Optional[FieldValue] = None


@dataclass
class Evaluation:
    """One official's evaluation on one page."""

    source_page: int
    row_index: int
    meet_match: MeetMatch
    # Session-level fields (may vary per page).
    session_number: Optional[FieldValue] = None
    date_session: Optional[FieldValue] = None
    competition_coordinator: Optional[FieldValue] = None
    cc_level: Optional[FieldValue] = None
    # Per-row fields.
    official_name: Optional[FieldValue] = None
    club: Optional[FieldValue] = None
    position: Optional[FieldValue] = None
    lane_number: Optional[FieldValue] = None
    times_worked_position: Optional[FieldValue] = None
    mentor: Optional[FieldValue] = None
    level: Optional[FieldValue] = None
    successful: Optional[FieldValue] = None
    # Composite — populated by merge.py from the field-level confidences.
    row_confidence: Optional[float] = None


ExtractionMethod = Literal["vision", "form_field", "mixed"]


@dataclass
class ParseResult:
    """Top-level canonical JSON for one input PDF."""

    source_pdf: str
    template_id: str
    template_confidence: float
    extraction_method: ExtractionMethod
    vision_model: Optional[str] = None
    # Only populated when ``--interactive`` / ``--review-all`` was used.
    edit_model: Optional[str] = None
    meet: MeetHeader = field(default_factory=MeetHeader)
    evaluations: list[Evaluation] = field(default_factory=list)
