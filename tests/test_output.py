"""Tests for src/output.py.

Builds in-memory ``ParseResult`` objects (no fixture PDFs needed — the
merge module that constructs them is a separate issue) and asserts that
JSON / CSV / XLSX writers all agree on what landed.
"""
from __future__ import annotations

import json
from pathlib import Path

import pandas as pd
import pytest

from src import output, schema as s


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _fv(value, conf=0.95, **kw) -> s.FieldValue:
    return s.FieldValue(value=value, confidence=conf, **kw)


def _two_eval_result() -> s.ParseResult:
    """A representative ParseResult: 1 meet, 2 evaluations across 2 pages,
    with a couple of low-confidence fields and a rationale on
    ``successful`` to exercise every column."""
    return s.ParseResult(
        source_pdf="aurora_open.pdf",
        template_id="swim_ontario_v1",
        template_confidence=0.98,
        extraction_method="vision",
        vision_model="qwen2.5vl:7b",
        edit_model="qwen2.5:7b",
        meet=s.MeetHeader(
            competition_name=_fv("Aurora Open 2026", 0.99),
            host_club=_fv("AAC", 0.97),
            coc=_fv("Beth Brown", 0.93),
        ),
        evaluations=[
            s.Evaluation(
                source_page=1,
                row_index=1,
                meet_match=s.MeetMatch(value="authoritative", confidence=1.0),
                session_number=_fv(1, 0.99, source="form"),
                date_session=_fv("2026-01-09", 0.96),
                competition_coordinator=_fv("Alex Anderson", 0.94),
                cc_level=_fv("Level 4", 0.90),
                official_name=_fv("Carlos Costa", 0.92),
                club=_fv("AAC", 0.97),
                position=_fv("Starter", 0.99),
                lane_number=_fv("", 1.0),
                times_worked_position=_fv("3", 0.85),
                mentor=_fv("Beth Brown", 0.88),
                level=_fv("Level 4", 0.86),
                successful=_fv(
                    True, 0.96,
                    rationale="initials present and clear",
                ),
                row_confidence=0.90,
            ),
            s.Evaluation(
                source_page=2,
                row_index=1,
                meet_match=s.MeetMatch(value="confirmed", confidence=0.91),
                session_number=_fv(2, 0.97, source="filename"),
                date_session=_fv("2026-01-09", 0.95),
                competition_coordinator=_fv("Alex Anderson", 0.93),
                cc_level=_fv("Level 4", 0.88),
                official_name=_fv("Dana Diaz", 0.62),  # low confidence
                club=_fv("AAC", 0.97),
                position=_fv("Stroke Judge", 0.99),
                lane_number=None,                       # absent
                times_worked_position=_fv("1", 0.80),
                mentor=_fv("Beth Brown", 0.85),
                level=_fv("Level 4", 0.82),
                successful=_fv(None, 0.55, rationale="initials illegible"),
                row_confidence=0.70,
            ),
        ],
    )


# ---------------------------------------------------------------------------
# JSON canonical
# ---------------------------------------------------------------------------


class TestWriteJson:
    def test_round_trips(self, tmp_path: Path):
        result = _two_eval_result()
        path = tmp_path / "out.json"
        output.write_json(result, path)

        loaded = json.loads(path.read_text(encoding="utf-8"))
        assert loaded["source_pdf"] == "aurora_open.pdf"
        assert loaded["template_id"] == "swim_ontario_v1"
        assert loaded["template_confidence"] == 0.98
        assert loaded["extraction_method"] == "vision"
        assert loaded["vision_model"] == "qwen2.5vl:7b"
        assert loaded["edit_model"] == "qwen2.5:7b"

    def test_per_field_shape_preserved(self, tmp_path: Path):
        result = _two_eval_result()
        path = tmp_path / "out.json"
        output.write_json(result, path)

        loaded = json.loads(path.read_text(encoding="utf-8"))
        # Each populated field is a {value, confidence, rationale, source}
        # object.
        cn = loaded["meet"]["competition_name"]
        assert cn == {
            "value": "Aurora Open 2026",
            "confidence": 0.99,
            "rationale": None,
            "source": None,
        }
        # session_number carries its provenance source.
        sn = loaded["evaluations"][0]["session_number"]
        assert sn["source"] == "form"
        # successful carries its rationale.
        suc = loaded["evaluations"][0]["successful"]
        assert suc["rationale"] == "initials present and clear"

    def test_meet_match_is_nested(self, tmp_path: Path):
        result = _two_eval_result()
        path = tmp_path / "out.json"
        output.write_json(result, path)

        loaded = json.loads(path.read_text(encoding="utf-8"))
        assert loaded["evaluations"][0]["meet_match"] == {
            "value": "authoritative",
            "confidence": 1.0,
        }
        assert loaded["evaluations"][1]["meet_match"] == {
            "value": "confirmed",
            "confidence": 0.91,
        }

    def test_absent_field_serialises_to_null(self, tmp_path: Path):
        result = _two_eval_result()
        path = tmp_path / "out.json"
        output.write_json(result, path)

        loaded = json.loads(path.read_text(encoding="utf-8"))
        # ``lane_number`` was None on the second evaluation.
        assert loaded["evaluations"][1]["lane_number"] is None

    def test_indented_and_utf8(self, tmp_path: Path):
        # Strings with non-ASCII (e.g. accented French names later) must
        # round-trip without escape-only encoding so the file stays
        # human-readable.
        result = _two_eval_result()
        result.meet.coc = _fv("Émilie Côté", 0.93)
        path = tmp_path / "out.json"
        output.write_json(result, path)
        raw = path.read_text(encoding="utf-8")
        assert "Émilie Côté" in raw  # not escaped
        # Indented output: first key on its own line.
        assert raw.startswith("{\n  \"source_pdf\"")


# ---------------------------------------------------------------------------
# Flat (CSV / XLSX) parity
# ---------------------------------------------------------------------------


class TestFlatten:
    def test_one_row_per_evaluation(self):
        result = _two_eval_result()
        rows = output.to_csv_rows(result)
        assert len(rows) == len(result.evaluations) == 2

    def test_meet_fields_repeated_on_every_row(self):
        result = _two_eval_result()
        rows = output.to_csv_rows(result)
        for row in rows:
            assert row["competition_name"] == "Aurora Open 2026"
            assert row["host_club"] == "AAC"
            assert row["coc"] == "Beth Brown"

    def test_confidence_column_is_row_composite(self):
        result = _two_eval_result()
        rows = output.to_csv_rows(result)
        assert rows[0]["confidence"] == 0.90
        assert rows[1]["confidence"] == 0.70

    def test_meet_match_collapses_to_value_string(self):
        result = _two_eval_result()
        rows = output.to_csv_rows(result)
        assert rows[0]["meet_match"] == "authoritative"
        assert rows[1]["meet_match"] == "confirmed"
        # And not the dict form — CSV cells must be flat.
        assert not isinstance(rows[0]["meet_match"], dict)

    def test_successful_split_into_value_and_rationale(self):
        result = _two_eval_result()
        rows = output.to_csv_rows(result)
        assert rows[0]["successful"] is True
        assert rows[0]["successful_rationale"] == "initials present and clear"
        # And the null-ambiguous case.
        assert rows[1]["successful"] is None
        assert rows[1]["successful_rationale"] == "initials illegible"

    def test_session_number_source_in_its_own_column(self):
        result = _two_eval_result()
        rows = output.to_csv_rows(result)
        assert rows[0]["session_number"] == 1
        assert rows[0]["session_number_source"] == "form"
        assert rows[1]["session_number"] == 2
        assert rows[1]["session_number_source"] == "filename"

    def test_absent_field_flattens_to_none(self):
        result = _two_eval_result()
        rows = output.to_csv_rows(result)
        # Second eval had lane_number=None.
        assert rows[1]["lane_number"] is None


class TestWriteAll:
    def setup_method(self):
        self.result = _two_eval_result()

    def test_all_three_files_written(self, tmp_path: Path):
        paths = output.write_all(self.result, tmp_path)
        assert paths["json"].is_file()
        assert paths["csv"].is_file()
        assert paths["xlsx"].is_file()

    def test_default_stem_from_source_pdf(self, tmp_path: Path):
        paths = output.write_all(self.result, tmp_path)
        assert paths["json"].name == "aurora_open.json"
        assert paths["csv"].name == "aurora_open.csv"
        assert paths["xlsx"].name == "aurora_open.xlsx"

    def test_explicit_stem_overrides_default(self, tmp_path: Path):
        paths = output.write_all(self.result, tmp_path, stem="custom_name")
        assert paths["json"].name == "custom_name.json"
        assert paths["csv"].name == "custom_name.csv"
        assert paths["xlsx"].name == "custom_name.xlsx"

    def test_output_dir_created_if_missing(self, tmp_path: Path):
        target = tmp_path / "deep" / "nested" / "dir"
        assert not target.exists()
        output.write_all(self.result, target)
        assert target.is_dir()
        assert (target / "aurora_open.json").is_file()

    def test_csv_columns_match_canonical_order(self, tmp_path: Path):
        paths = output.write_all(self.result, tmp_path)
        df = pd.read_csv(paths["csv"])
        # CSV column order is the public contract — tighten now so a
        # future refactor doesn't silently reshuffle it.
        assert list(df.columns) == list(output._CSV_COLUMNS)

    def test_xlsx_has_evaluations_sheet(self, tmp_path: Path):
        paths = output.write_all(self.result, tmp_path)
        df = pd.read_excel(paths["xlsx"], sheet_name="evaluations")
        assert len(df) == 2
        assert list(df.columns) == list(output._CSV_COLUMNS)

    def test_csv_and_xlsx_row_parity(self, tmp_path: Path):
        paths = output.write_all(self.result, tmp_path)
        csv_df = pd.read_csv(paths["csv"])
        xlsx_df = pd.read_excel(paths["xlsx"], sheet_name="evaluations")
        # Same shape, same values. (Object dtypes may differ slightly
        # between csv and xlsx round-trips — string-compare to neutralize.)
        assert csv_df.shape == xlsx_df.shape
        assert csv_df.astype(object).fillna("").values.tolist() \
            == xlsx_df.astype(object).fillna("").values.tolist()

    def test_csv_values_match_flatten_output(self, tmp_path: Path):
        paths = output.write_all(self.result, tmp_path)
        df = pd.read_csv(paths["csv"])
        # Spot-check a handful of fields end-to-end through the CSV.
        assert df.iloc[0]["competition_name"] == "Aurora Open 2026"
        assert df.iloc[0]["meet_match"] == "authoritative"
        assert df.iloc[0]["successful_rationale"] == "initials present and clear"
        # Confidence is numeric.
        assert df.iloc[0]["confidence"] == pytest.approx(0.90)
        assert df.iloc[1]["confidence"] == pytest.approx(0.70)


class TestEmptyResult:
    """A ParseResult with zero evaluations still produces valid files."""

    def _empty(self) -> s.ParseResult:
        return s.ParseResult(
            source_pdf="empty.pdf",
            template_id="swim_ontario_v1",
            template_confidence=0.98,
            extraction_method="form_field",
        )

    def test_json_has_empty_evaluations_list(self, tmp_path: Path):
        result = self._empty()
        path = tmp_path / "empty.json"
        output.write_json(result, path)
        loaded = json.loads(path.read_text(encoding="utf-8"))
        assert loaded["evaluations"] == []

    def test_csv_is_header_only(self, tmp_path: Path):
        result = self._empty()
        paths = output.write_all(result, tmp_path)
        df = pd.read_csv(paths["csv"])
        assert len(df) == 0
        assert list(df.columns) == list(output._CSV_COLUMNS)

    def test_xlsx_is_header_only(self, tmp_path: Path):
        result = self._empty()
        paths = output.write_all(result, tmp_path)
        df = pd.read_excel(paths["xlsx"], sheet_name="evaluations")
        assert len(df) == 0
