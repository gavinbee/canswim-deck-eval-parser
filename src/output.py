"""Serialise a ``ParseResult`` to disk as JSON (canonical), CSV, and XLSX.

The canonical output is the **JSON** — full ``ParseResult`` tree with
per-field ``{value, confidence}``, ``meet_match`` provenance, model
metadata, and a composite ``row_confidence`` per evaluation. CSV and
XLSX are derived **flat** views: one row per evaluation, with all schema
fields as plain values and a single ``confidence`` column carrying the
row composite. CSV cells never contain JSON — keep them grep- and
diff-friendly.

The three files share a stem and land alongside one another in the
output directory::

    output/session_1_evals.json
    output/session_1_evals.csv
    output/session_1_evals.xlsx

The per-page raw model response sidecar (``.raw.json``) is owned by
``src.vision_extract`` (it's a caching artifact, not an output product),
not by this module.
"""
from __future__ import annotations

import json
import logging
from dataclasses import asdict
from pathlib import Path
from typing import Any, Optional

import pandas as pd

from . import schema as s

log = logging.getLogger(__name__)


# Column order for the flat CSV / XLSX. Provenance and meet-level fields
# first, then session-level, then per-row, then the composite confidence.
# Keep this stable — downstream tooling (rems-sync hookup, future web app)
# will index on it.
_CSV_COLUMNS: tuple[str, ...] = (
    "source_pdf",
    "template_id",
    "extraction_method",
    "source_page",
    "row_index",
    "meet_match",
    # Meet-level
    "competition_name",
    "host_club",
    "coc",
    # Session-level
    "competition_coordinator",
    "cc_level",
    "date_session",
    "session_number",
    "session_number_source",
    # Per-row
    "official_name",
    "club",
    "position",
    "lane_number",
    "times_worked_position",
    "mentor",
    "level",
    "successful",
    "successful_rationale",
    # Composite
    "confidence",
)


def write_all(
    result: s.ParseResult,
    output_dir: str | Path,
    stem: Optional[str] = None,
) -> dict[str, Path]:
    """Write JSON, CSV, and XLSX for a parse result.

    Args:
        result: The assembled ``ParseResult``.
        output_dir: Directory to write into. Created if needed.
        stem:
            Output filename stem (no extension). Defaults to the source
            PDF's stem so ``session_1_evals.pdf`` → ``session_1_evals.json``
            etc.

    Returns:
        ``{"json": Path, "csv": Path, "xlsx": Path}`` for the written files.
    """
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    if stem is None:
        stem = Path(result.source_pdf).stem

    paths = {
        "json": output_dir / f"{stem}.json",
        "csv":  output_dir / f"{stem}.csv",
        "xlsx": output_dir / f"{stem}.xlsx",
    }

    write_json(result, paths["json"])
    rows = _flatten(result)
    _write_csv(rows, paths["csv"])
    _write_xlsx(rows, paths["xlsx"])

    log.info(
        "Wrote %s, %s, %s (%d evaluations)",
        paths["json"].name, paths["csv"].name, paths["xlsx"].name,
        len(rows),
    )
    return paths


def write_json(result: s.ParseResult, path: str | Path) -> None:
    """Write the canonical JSON file.

    ``dataclasses.asdict`` recursively converts the ``ParseResult`` tree
    into nested dicts/lists. ``json.dump`` then serialises straight to
    disk — every ``Optional[FieldValue]`` becomes either ``None`` or a
    ``{"value": ..., "confidence": ..., "rationale": ..., "source": ...}``
    object. Indented for diff-friendliness.
    """
    data = asdict(result)
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False, sort_keys=False)
        f.write("\n")


def to_csv_rows(result: s.ParseResult) -> list[dict[str, Any]]:
    """Return the flat-table rows that the CSV / XLSX writers consume.

    Exposed publicly so tests can assert parity between the canonical
    JSON and the derived flat views without re-reading the CSV.
    """
    return _flatten(result)


# ---------------------------------------------------------------------------
# Internals
# ---------------------------------------------------------------------------


def _flatten(result: s.ParseResult) -> list[dict[str, Any]]:
    """Build the long-format row list from a ``ParseResult``.

    One row per evaluation. Meet-level fields are repeated on every row
    (consumers expect a self-contained record) and the row composite
    confidence is exposed as a plain ``confidence`` column.

    ``meet_match`` collapses to its ``value`` string (not the full
    ``{value, confidence}`` dict) — the confidence is folded into
    ``row_confidence``, and the verdict string is what humans skim for.

    ``successful`` collapses similarly: the boolean (or null) goes into
    ``successful`` and any ``rationale`` lands in its own
    ``successful_rationale`` column. The vision model's confidence on
    the sign-off contributes to ``row_confidence`` (and so to the
    ``confidence`` column).
    """
    rows: list[dict[str, Any]] = []
    meet = result.meet
    for ev in result.evaluations:
        row: dict[str, Any] = {
            "source_pdf":         result.source_pdf,
            "template_id":        result.template_id,
            "extraction_method":  result.extraction_method,
            "source_page":        ev.source_page,
            "row_index":          ev.row_index,
            "meet_match":         ev.meet_match.value,
            "competition_name":   _fv_value(meet.competition_name),
            "host_club":          _fv_value(meet.host_club),
            "coc":                _fv_value(meet.coc),
            "competition_coordinator":   _fv_value(ev.competition_coordinator),
            "cc_level":           _fv_value(ev.cc_level),
            "date_session":       _fv_value(ev.date_session),
            "session_number":     _fv_value(ev.session_number),
            "session_number_source": _fv_attr(ev.session_number, "source"),
            "official_name":      _fv_value(ev.official_name),
            "club":               _fv_value(ev.club),
            "position":           _fv_value(ev.position),
            "lane_number":        _fv_value(ev.lane_number),
            "times_worked_position": _fv_value(ev.times_worked_position),
            "mentor":             _fv_value(ev.mentor),
            "level":              _fv_value(ev.level),
            "successful":         _fv_value(ev.successful),
            "successful_rationale": _fv_attr(ev.successful, "rationale"),
            "confidence":         ev.row_confidence,
        }
        rows.append(row)
    return rows


def _fv_value(fv: Optional[s.FieldValue]) -> Any:
    """Return the inner ``.value`` of a ``FieldValue`` or ``None``."""
    return None if fv is None else fv.value


def _fv_attr(fv: Optional[s.FieldValue], attr: str) -> Any:
    """Return an attribute of a ``FieldValue`` or ``None``."""
    return None if fv is None else getattr(fv, attr)


def _write_csv(rows: list[dict[str, Any]], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    # Use pandas so the CSV/XLSX writers share a code path — and so the
    # final flat schema is column-ordered identically in both. Empty
    # evaluations list still produces a header-only CSV.
    df = pd.DataFrame(rows, columns=list(_CSV_COLUMNS))
    df.to_csv(path, index=False, encoding="utf-8")


def _write_xlsx(rows: list[dict[str, Any]], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    df = pd.DataFrame(rows, columns=list(_CSV_COLUMNS))
    # Single sheet named "evaluations" — design doc §Output.
    with pd.ExcelWriter(path, engine="openpyxl") as writer:
        df.to_excel(writer, sheet_name="evaluations", index=False)
