"""Tests for src/merge.py.

Most cases use small synthetic ``PageExtraction`` lists so we can
exercise the reconciliation algorithm without depending on a real PDF.
The final test does run end-to-end on the form-field fixture from
test_form_extract to confirm everything wires up cleanly with real data.
"""
from __future__ import annotations

import logging
from pathlib import Path
from typing import Optional

import pytest

from src import form_extract, merge, schema as s
from src.form_extract import PageExtraction
from src.merge import (
    MeetFields,
    MultiMeetError,
    SameMeetVerdict,
    merge as do_merge,
)
from src.templates import TEMPLATES


FIXTURE = (
    Path(__file__).parent / "fixtures" / "form_field" / "session_1_evals.pdf"
)
ONTARIO = TEMPLATES["swim_ontario_v1"]


# ---------------------------------------------------------------------------
# Builders
# ---------------------------------------------------------------------------


def _fv(value, conf=1.0) -> s.FieldValue:
    return s.FieldValue(value=value, confidence=conf)


def _meet_dict(
    competition_name: str = "Aurora Open 2026",
    host_club: str = "AAC",
    coc: str = "Beth Brown",
    conf: float = 1.0,
) -> MeetFields:
    return {
        s.COMPETITION_NAME: _fv(competition_name, conf),
        s.HOST_CLUB:        _fv(host_club, conf),
        s.COC:              _fv(coc, conf),
    }


def _session_dict(
    coordinator: str = "Alex Anderson",
    date_session: str = "Fri, Jan 9, 2026 / Session 1",
    cc_level: str = "",
) -> dict[str, s.FieldValue]:
    return {
        s.COMPETITION_COORDINATOR: _fv(coordinator),
        s.CC_LEVEL:                _fv(cc_level),
        s.DATE_SESSION:            _fv(date_session),
    }


def _row(name: str, position: str = "Starter", successful: str = "") -> dict[str, s.FieldValue]:
    return {
        s.OFFICIAL_NAME: _fv(name),
        s.CLUB:          _fv("AAC"),
        s.POSITION:      _fv(position),
        s.SUCCESSFUL:    _fv(successful),
    }


def _page(
    number: int,
    meet: Optional[MeetFields] = None,
    session: Optional[dict[str, s.FieldValue]] = None,
    rows: Optional[list[dict[str, s.FieldValue]]] = None,
) -> PageExtraction:
    return PageExtraction(
        page_number=number,
        meet=_meet_dict() if meet is None else meet,
        session=_session_dict() if session is None else session,
        rows=rows if rows is not None else [_row("Carlos Costa")],
    )


def _do_merge(pages: list[PageExtraction], **overrides) -> s.ParseResult:
    kwargs = dict(
        source_pdf="test.pdf",
        template_id="swim_ontario_v1",
        template_confidence=0.99,
        extraction_method="form_field",
    )
    kwargs.update(overrides)
    return do_merge(pages, **kwargs)


# ---------------------------------------------------------------------------
# Empty / single-page
# ---------------------------------------------------------------------------


class TestEmpty:
    def test_no_pages_raises(self):
        with pytest.raises(ValueError):
            do_merge(
                [],
                source_pdf="test.pdf",
                template_id="swim_ontario_v1",
                template_confidence=0.99,
                extraction_method="form_field",
            )


class TestSinglePage:
    def setup_method(self):
        self.page = _page(
            1,
            rows=[
                _row("Carlos Costa", position="Starter", successful="cc"),
                _row("Dana Diaz", position="Stroke Judge", successful=""),
            ],
        )
        self.result = _do_merge([self.page])

    def test_top_level_fields(self):
        assert self.result.source_pdf == "test.pdf"
        assert self.result.template_id == "swim_ontario_v1"
        assert self.result.template_confidence == 0.99
        assert self.result.extraction_method == "form_field"

    def test_meet_header_taken_from_page_one(self):
        assert self.result.meet.competition_name.value == "Aurora Open 2026"
        assert self.result.meet.host_club.value == "AAC"
        assert self.result.meet.coc.value == "Beth Brown"

    def test_one_evaluation_per_row(self):
        assert len(self.result.evaluations) == 2

    def test_page_one_is_authoritative(self):
        for ev in self.result.evaluations:
            assert ev.meet_match.value == "authoritative"
            assert ev.meet_match.confidence == 1.0

    def test_row_index_one_indexed(self):
        assert [ev.row_index for ev in self.result.evaluations] == [1, 2]


# ---------------------------------------------------------------------------
# Reconciliation: identical meet on every page
# ---------------------------------------------------------------------------


class TestExactMatch:
    def test_identical_pages_confirm_without_a_checker(self):
        # eval-gen output is exactly this case: every page has the same
        # meet header. No LLM call should be needed.
        p1 = _page(1)
        p2 = _page(2)
        result = _do_merge([p1, p2])
        meet_matches = [ev.meet_match.value for ev in result.evaluations]
        # Page 1 → authoritative; page 2 → confirmed.
        assert meet_matches == ["authoritative", "confirmed"]
        confs = [ev.meet_match.confidence for ev in result.evaluations]
        assert confs == [1.0, 1.0]

    def test_match_is_case_and_whitespace_insensitive(self):
        # "ROW" vs " row " should still be confirmed without consulting
        # the LLM — these are obviously the same identifier.
        p1 = _page(1, meet=_meet_dict(host_club="ROW"))
        p2 = _page(2, meet=_meet_dict(host_club=" row "))
        result = _do_merge([p1, p2])
        assert result.evaluations[1].meet_match.value == "confirmed"


# ---------------------------------------------------------------------------
# Reconciliation: blank pages → carried
# ---------------------------------------------------------------------------


class TestCarried:
    def test_blank_page_carries_page_one(self):
        # Per-row carrying is implicit — we just don't fail. The
        # meet_match value tracks the reconciliation outcome.
        p1 = _page(1)
        p2 = _page(2, meet={
            s.COMPETITION_NAME: _fv(""),
            s.HOST_CLUB:        _fv(""),
            s.COC:              _fv(""),
        })
        result = _do_merge([p1, p2])
        assert result.evaluations[1].meet_match.value == "carried"
        assert result.evaluations[1].meet_match.confidence == 1.0

    def test_missing_keys_also_carry(self):
        # A page with no meet-level fields *at all* should also carry.
        p1 = _page(1)
        p2 = _page(2, meet={})
        result = _do_merge([p1, p2])
        assert result.evaluations[1].meet_match.value == "carried"


# ---------------------------------------------------------------------------
# Reconciliation: model-mediated decisions
# ---------------------------------------------------------------------------


class TestSameMeetCheckerSame:
    def test_confirmed_with_model_confidence(self):
        # Headers differ enough that the fast paths don't apply, but
        # the LLM says they refer to the same meet.
        seen = []
        def checker(p1, pn):
            seen.append((p1, pn))
            return SameMeetVerdict(verdict="same", confidence=0.88)

        p1 = _page(1, meet=_meet_dict(competition_name="Aurora Open 2026"))
        p2 = _page(2, meet=_meet_dict(competition_name="Aurora Open 26"))
        result = _do_merge([p1, p2], same_meet_checker=checker)
        assert result.evaluations[1].meet_match.value == "confirmed"
        assert result.evaluations[1].meet_match.confidence == 0.88
        assert len(seen) == 1


class TestSameMeetCheckerDifferent:
    def test_different_raises_multi_meet_error(self):
        def checker(p1, pn):
            return SameMeetVerdict(verdict="different", confidence=0.92)

        p1 = _page(1, meet=_meet_dict(competition_name="Aurora Open 2026"))
        p2 = _page(2, meet=_meet_dict(competition_name="Birch Cup 2026"))
        with pytest.raises(MultiMeetError) as exc:
            _do_merge([p1, p2], same_meet_checker=checker)
        msg = str(exc.value)
        assert "Aurora Open 2026" in msg
        assert "Birch Cup 2026" in msg
        assert exc.value.page_n_index == 2
        assert exc.value.confidence == 0.92


class TestSameMeetCheckerUnknown:
    def test_unknown_carries_with_warning(self, caplog):
        def checker(p1, pn):
            return SameMeetVerdict(verdict="unknown", confidence=0.40)

        p1 = _page(1, meet=_meet_dict(competition_name="Aurora Open 2026"))
        p2 = _page(2, meet=_meet_dict(competition_name="Smudged Name"))

        with caplog.at_level(logging.WARNING, logger="src.merge"):
            result = _do_merge([p1, p2], same_meet_checker=checker)

        assert result.evaluations[1].meet_match.value == "unknown"
        assert result.evaluations[1].meet_match.confidence == 0.40
        # The warning gives the user something to grep for in a CI log.
        assert any("could not be confidently matched" in r.message for r in caplog.records)


class TestNoCheckerOnConflict:
    def test_raises_multi_meet_error_when_no_checker(self):
        # Without a checker we refuse to silently accept disagreement.
        p1 = _page(1, meet=_meet_dict(competition_name="Aurora Open 2026"))
        p2 = _page(2, meet=_meet_dict(competition_name="Birch Cup 2026"))
        with pytest.raises(MultiMeetError):
            _do_merge([p1, p2])


# ---------------------------------------------------------------------------
# Value coercion
# ---------------------------------------------------------------------------


class TestSuccessfulCoercion:
    def test_empty_string_becomes_none(self):
        page = _page(1, rows=[_row("X", successful="")])
        result = _do_merge([page])
        ev = result.evaluations[0]
        assert ev.successful is not None  # FieldValue itself isn't dropped
        assert ev.successful.value is None
        assert ev.successful.confidence == 1.0

    def test_whitespace_only_becomes_none(self):
        page = _page(1, rows=[_row("X", successful="   ")])
        result = _do_merge([page])
        assert result.evaluations[0].successful.value is None

    def test_non_empty_string_becomes_true(self):
        page = _page(1, rows=[_row("X", successful="ab")])
        result = _do_merge([page])
        assert result.evaluations[0].successful.value is True

    def test_typed_true_passthrough(self):
        page = _page(1, rows=[{
            s.OFFICIAL_NAME: _fv("X"),
            s.SUCCESSFUL:    _fv(True, conf=0.9),
        }])
        result = _do_merge([page])
        assert result.evaluations[0].successful.value is True
        assert result.evaluations[0].successful.confidence == 0.9

    def test_typed_false_passthrough(self):
        page = _page(1, rows=[{
            s.OFFICIAL_NAME: _fv("X"),
            s.SUCCESSFUL:    _fv(False, conf=0.95),
        }])
        result = _do_merge([page])
        assert result.evaluations[0].successful.value is False


class TestSessionNumberCoercion:
    def test_digit_string_becomes_int(self):
        page = _page(1, session={
            s.SESSION_NUMBER: _fv("3", conf=0.97),
            s.DATE_SESSION:   _fv("Apr 11, 2026"),
        })
        result = _do_merge([page])
        ev = result.evaluations[0]
        assert ev.session_number.value == 3
        assert isinstance(ev.session_number.value, int)
        assert ev.session_number.confidence == 0.97

    def test_int_passthrough(self):
        page = _page(1, session={s.SESSION_NUMBER: _fv(2, conf=0.99)})
        result = _do_merge([page])
        assert result.evaluations[0].session_number.value == 2

    def test_non_digit_string_stays_string(self):
        # Vision model might emit "?" or "unknown"; preserve verbatim.
        page = _page(1, session={s.SESSION_NUMBER: _fv("?", conf=0.3)})
        result = _do_merge([page])
        assert result.evaluations[0].session_number.value == "?"


# ---------------------------------------------------------------------------
# Row confidence composite
# ---------------------------------------------------------------------------


class TestRowConfidence:
    def test_all_certain_form_field_run_yields_one(self):
        # Form-field path emits 1.0 across the board; row_confidence
        # should be exactly 1.0.
        page = _page(1)
        result = _do_merge([page])
        assert result.evaluations[0].row_confidence == 1.0

    def test_picks_the_minimum_field_confidence(self):
        page = _page(1, rows=[{
            s.OFFICIAL_NAME: _fv("X", conf=0.95),
            s.POSITION:      _fv("Starter", conf=0.62),  # the weak link
            s.CLUB:          _fv("AAC", conf=0.9),
        }])
        result = _do_merge([page])
        assert result.evaluations[0].row_confidence == 0.62

    def test_meet_match_confidence_is_in_the_mix(self):
        def checker(p1, pn):
            return SameMeetVerdict(verdict="unknown", confidence=0.3)

        p1 = _page(1)
        p2 = _page(2, meet=_meet_dict(competition_name="Different"))
        result = _do_merge([p1, p2], same_meet_checker=checker)
        # Page-2 row inherits meet_match.confidence=0.3, dragging
        # row_confidence down to 0.3.
        assert result.evaluations[1].row_confidence == 0.3


# ---------------------------------------------------------------------------
# Top-level metadata propagation
# ---------------------------------------------------------------------------


class TestMetadataPropagation:
    def test_models_recorded(self):
        page = _page(1)
        result = _do_merge(
            [page],
            extraction_method="vision",
            vision_model="qwen2.5vl:7b",
            edit_model="qwen2.5:7b",
        )
        assert result.vision_model == "qwen2.5vl:7b"
        assert result.edit_model == "qwen2.5:7b"

    def test_form_field_default_models_are_none(self):
        page = _page(1)
        result = _do_merge([page])
        assert result.vision_model is None
        assert result.edit_model is None


# ---------------------------------------------------------------------------
# End-to-end with the real fixture
# ---------------------------------------------------------------------------


class TestEndToEndFixture:
    def setup_method(self):
        pages = form_extract.extract_pdf(str(FIXTURE), ONTARIO)
        self.result = do_merge(
            pages,
            source_pdf="session_1_evals.pdf",
            template_id="swim_ontario_v1",
            template_confidence=0.99,
            extraction_method="form_field",
        )

    def test_eleven_evaluations(self):
        # 9 on page 1 + 2 on page 2.
        assert len(self.result.evaluations) == 11

    def test_meet_header_from_synthetic_fixture(self):
        assert self.result.meet.competition_name.value == "Clark Open 2026"
        assert self.result.meet.host_club.value == "TUF"
        assert self.result.meet.coc.value == "Danielle Ford"

    def test_meet_match_distribution(self):
        # 9 evals on page 1 → authoritative; 2 evals on page 2 →
        # confirmed (identical headers).
        values = [ev.meet_match.value for ev in self.result.evaluations]
        assert values.count("authoritative") == 9
        assert values.count("confirmed") == 2

    def test_no_successful_signoffs_on_blank_form(self):
        # The synthetic fixture has empty "Successful initial" cells.
        # Form-field path coerces blank → null (genuinely unknown).
        for ev in self.result.evaluations:
            assert ev.successful is not None
            assert ev.successful.value is None

    def test_row_confidence_is_one(self):
        # Form-field path: every field is 1.0, meet_match is 1.0,
        # composite is 1.0.
        for ev in self.result.evaluations:
            assert ev.row_confidence == 1.0
