"""Swim Ontario On-Deck Evaluation form — version 1.

Field labels and widget names are sourced verbatim from
``eval-gen/eval_gen.py``'s ``_build_fields()``, which itself was reverse-
engineered from ``eval-gen/eval_form.pdf`` (the official Swim Ontario blank
template). Keeping this module in lockstep with eval-gen means an eval-gen
output is parsable through the form-field fast path with no extra work.
"""
from __future__ import annotations

from .. import schema as s
from .base import Template


# Human / widget labels on the Swim Ontario form. Keys are canonical schema
# names from ``src.schema``; values are the strings that appear on the form.
_FIELD_NAMES: dict[str, str] = {
    s.COMPETITION_NAME:        "Competition Name",
    s.HOST_CLUB:               "Host Club",
    s.COC:                     "COC",
    s.COMPETITION_COORDINATOR: "Competition Coordinator",
    s.CC_LEVEL:                "Level",
    s.DATE_SESSION:            "Date  Session",
    s.PAGE_NUMBER:             "Page",
    s.PAGE_OF:                 "of",
    s.OFFICIAL_NAME:           "Name of Official",
    s.CLUB:                    "Club",
    s.POSITION:                "Position",
    s.LANE_NUMBER:             "Lane number",
    s.TIMES_WORKED_POSITION:   "How many times have you worked this position",
    s.MENTOR:                  "Mentor Official  Session referee",
    s.LEVEL:                   "Level",
    s.SUCCESSFUL:              "Successful initial",
}


# PyMuPDF widget names on the fillable template. Per-row widgets in the
# Swim Ontario template are named ``"<base>Row<i>"`` where i is 1..9; the
# ``{i}`` placeholder lets the form-extract module substitute the row index
# at lookup time.
_WIDGET_FIELD_MAP: dict[str, str] = {
    # Meet- and session-level (single widget each, no row suffix).
    "Competition Name":        s.COMPETITION_NAME,
    "Competition Coordinator": s.COMPETITION_COORDINATOR,
    "Level":                   s.CC_LEVEL,
    "Date  Session":           s.DATE_SESSION,
    "Host Club":               s.HOST_CLUB,
    "COC":                     s.COC,
    "Page":                    s.PAGE_NUMBER,
    "of":                      s.PAGE_OF,
    # Per-row widgets — ``{i}`` is the row index 1..rows_per_page.
    "Name of OfficialRow{i}":                            s.OFFICIAL_NAME,
    "ClubRow{i}":                                        s.CLUB,
    "PositionRow{i}":                                    s.POSITION,
    "Lane numberRow{i}":                                 s.LANE_NUMBER,
    "How many times have you worked this positionRow{i}": s.TIMES_WORKED_POSITION,
    "Mentor Official  Session refereeRow{i}":            s.MENTOR,
    "LevelRow{i}":                                       s.LEVEL,
    "Successful initialRow{i}":                          s.SUCCESSFUL,
}


_VISION_PROMPT_ADDENDUM = """\
This is a Swim Ontario On-Deck Evaluation form.

Layout: up to 9 official-rows per page. Each row contains the official's
name, club abbreviation, position (e.g. "Chief Timer", "Inspector of Turns",
"Starter", "Referee"), lane assignment, count of times they've worked the
position, mentor's name, the mentor's officiating level (NOT the
evaluated official's level), and a "Successful initial" cell where the
mentor signs off.

For the ``successful`` field, look at the entire row, not just the initials
cell — and also at the row above when interpreting ditto-style marks.
Evaluators have no standard "not successful" convention:

- Initials clearly present in the "Successful initial" cell → emit
  successful=true with high confidence.
- Whole row is crossed out, or initials cell explicitly says "no" /
  "redo" → emit successful=false.
- Initials cell is blank but a mentor name is filled in → typically
  successful=false (the eval happened but no sign-off was given).
- Ditto marks, double-tick marks (″, ″, ditto), or a down-arrow in the
  Mentor, Level, or Successful-initial cells — these mean "same as the
  row above". Carry the value from the row above for the affected
  columns, but emit a LOWER confidence (e.g. 0.6) so the row surfaces
  for human review. This pattern is common when one evaluator signs off
  many rows at once.
- Marginal notes ("retry next meet", etc.) → judgement call, often
  successful=null with a rationale.

Dates on this form are typically formatted like "Sat, Apr 11, 2026" or
"April 11, 2026". Emit them as ISO YYYY-MM-DD.
"""


TEMPLATE = Template(
    id="swim_ontario_v1",
    display_name="Swim Ontario On-Deck Evaluation",
    field_names=_FIELD_NAMES,
    language="en",
    date_formats=[
        "%a, %b %d, %Y",        # Sat, Apr 11, 2026
        "%A, %B %d, %Y",        # Saturday, April 11, 2026
        "%B %d, %Y",            # April 11, 2026
        "%Y-%m-%d",             # 2026-04-11
    ],
    vision_prompt_addendum=_VISION_PROMPT_ADDENDUM,
    widget_field_map=_WIDGET_FIELD_MAP,
    rows_per_page=9,
)
