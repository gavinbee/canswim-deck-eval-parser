"""The ``Template`` dataclass — what each provincial On-Deck Evaluation
template module supplies to the rest of the parser.

Adding a new provincial template is "fill in this dataclass and register it"
— see ``docs/templates/README.md`` for the contract.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal


Language = Literal["en", "fr", "en_fr"]


@dataclass(frozen=True)
class Template:
    """Static description of one provincial deck-eval form.

    Attributes:
        id:
            Stable, version-suffixed identifier used everywhere downstream
            (e.g. ``"swim_ontario_v1"``). The ``_vN`` suffix lets us evolve
            a province's template without breaking historical outputs that
            still reference the old shape.
        display_name:
            Human-readable name shown in CLI output and docs
            (e.g. ``"Swim Ontario On-Deck Evaluation"``).
        field_names:
            Maps each canonical schema key (from ``src.schema``) to the
            human label or PDF widget name as it appears on this template.
            Used by the vision prompt to tell the model what to look for,
            and by the form-field fast path via ``widget_field_map``.
        language:
            Primary language(s) on the form. Affects how the vision prompt
            phrases its instructions and which dictionaries the model
            prefers when transcribing.
        date_formats:
            Acceptable input date formats on this template (e.g.
            ``["%a, %b %-d, %Y", "%Y-%m-%d"]``). The model is always
            instructed to emit ISO ``YYYY-MM-DD`` regardless.
        vision_prompt_addendum:
            Template-specific text appended to the page-extraction prompt.
            Quote province-specific labels here ("In Quebec forms, 'Officiel'
            means 'official_name'") and any conventions ("Dates are
            DD/MM/YYYY").
        widget_field_map:
            For form-field PDFs (fillable widgets): maps the PyMuPDF widget
            name to the canonical schema key. Per-row widgets carry the
            row index as a placeholder ``{i}`` so a single entry covers all
            rows (e.g. ``"Name of OfficialRow{i}": "official_name"``).
            Empty for templates that only ever come in as scans.
        rows_per_page:
            How many official-rows one page of this template holds.
    """

    id: str
    display_name: str
    field_names: dict[str, str]
    language: Language
    date_formats: list[str]
    vision_prompt_addendum: str
    widget_field_map: dict[str, str] = field(default_factory=dict)
    rows_per_page: int = 9
