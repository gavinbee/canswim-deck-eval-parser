"""Tests for src/schema.py."""
from dataclasses import asdict

from src import schema as s


class TestFieldConstants:
    def test_meet_session_row_fields_are_disjoint(self):
        meet = set(s.MEET_FIELDS)
        session = set(s.SESSION_FIELDS)
        row = set(s.ROW_FIELDS)
        assert meet.isdisjoint(session)
        assert meet.isdisjoint(row)
        assert session.isdisjoint(row)

    def test_all_fields_is_the_union(self):
        union = set(s.MEET_FIELDS) | set(s.SESSION_FIELDS) | set(s.ROW_FIELDS)
        assert set(s.ALL_FIELDS) == union

    def test_no_duplicate_field_names(self):
        # Catches accidental copy-paste duplicates when adding new fields.
        assert len(s.ALL_FIELDS) == len(set(s.ALL_FIELDS))

    def test_constants_match_their_string_values(self):
        # The constants exist to make renames a single-file change; the
        # string values themselves are the canonical schema keys, so the
        # constant and its value must agree.
        assert s.COMPETITION_NAME == "competition_name"
        assert s.HOST_CLUB == "host_club"
        assert s.OFFICIAL_NAME == "official_name"
        assert s.SUCCESSFUL == "successful"

    def test_max_rows_per_page_is_positive(self):
        assert s.MAX_ROWS_PER_PAGE >= 1


class TestFieldValue:
    def test_minimum_required_fields(self):
        fv = s.FieldValue(value="Cunningham Classic", confidence=0.97)
        assert fv.value == "Cunningham Classic"
        assert fv.confidence == 0.97
        assert fv.rationale is None
        assert fv.source is None

    def test_serializes_via_asdict(self):
        fv = s.FieldValue(
            value=True,
            confidence=0.96,
            rationale="initials present and clear",
        )
        d = asdict(fv)
        assert d == {
            "value": True,
            "confidence": 0.96,
            "rationale": "initials present and clear",
            "source": None,
        }

    def test_session_number_with_source(self):
        fv = s.FieldValue(value=3, confidence=0.99, source="filename")
        assert fv.source == "filename"


class TestMeetMatch:
    def test_authoritative_page_one(self):
        mm = s.MeetMatch(value="authoritative", confidence=1.0)
        assert mm.value == "authoritative"
        assert mm.confidence == 1.0

    def test_serializes_via_asdict(self):
        mm = s.MeetMatch(value="confirmed", confidence=0.91)
        assert asdict(mm) == {"value": "confirmed", "confidence": 0.91}


class TestParseResult:
    def test_minimal_serialization(self):
        result = s.ParseResult(
            source_pdf="session_3_evals.pdf",
            template_id="swim_ontario_v1",
            template_confidence=0.98,
            extraction_method="form_field",
        )
        d = asdict(result)
        assert d["source_pdf"] == "session_3_evals.pdf"
        assert d["template_id"] == "swim_ontario_v1"
        assert d["evaluations"] == []
        # meet defaults to a MeetHeader with all fields None.
        assert d["meet"] == {"competition_name": None, "host_club": None, "coc": None}

    def test_with_an_evaluation(self):
        result = s.ParseResult(
            source_pdf="x.pdf",
            template_id="swim_ontario_v1",
            template_confidence=0.99,
            extraction_method="vision",
            vision_model="qwen2.5vl:7b",
            meet=s.MeetHeader(
                competition_name=s.FieldValue(value="X Classic", confidence=0.95),
            ),
            evaluations=[
                s.Evaluation(
                    source_page=1,
                    row_index=1,
                    meet_match=s.MeetMatch(value="authoritative", confidence=1.0),
                    official_name=s.FieldValue(value="Jane Doe", confidence=0.94),
                    successful=s.FieldValue(
                        value=True,
                        confidence=0.96,
                        rationale="initials present",
                    ),
                    row_confidence=0.95,
                ),
            ],
        )
        d = asdict(result)
        assert d["vision_model"] == "qwen2.5vl:7b"
        assert d["edit_model"] is None
        assert len(d["evaluations"]) == 1
        row = d["evaluations"][0]
        assert row["official_name"]["value"] == "Jane Doe"
        assert row["successful"]["rationale"] == "initials present"
        assert row["meet_match"] == {"value": "authoritative", "confidence": 1.0}
