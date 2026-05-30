"""Tests for src/vision_extract.py.

The vision model is always mocked — a fake client whose ``generate``
returns canned JSON. Tests pin: prompt construction (template addendum +
filename + schema field names present), JSON parsing into the
PageExtraction shape, the retry-on-bad-JSON path, successful/session
coercion, confidence clamping, and the raw.json cache round-trip.
"""
from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from src import schema as s, vision_extract
from src.templates import TEMPLATES
from src.vision_extract import (
    VisionExtractionError,
    build_prompt,
    extract_page,
    extract_pdf,
)


ONTARIO = TEMPLATES["swim_ontario_v1"]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _good_response(
    *,
    competition_name="Aurora Open 2026",
    n_rows=2,
    session_source="form",
) -> dict:
    rows = []
    for i in range(n_rows):
        rows.append({
            "official_name": {"value": f"Official {i+1}", "confidence": 0.9},
            "club": {"value": "AAC", "confidence": 0.95},
            "position": {"value": "Starter", "confidence": 0.88},
            "lane_number": {"value": None, "confidence": 1.0},
            "times_worked_position": {"value": "3", "confidence": 0.7},
            "mentor": {"value": "Beth Brown", "confidence": 0.8},
            "level": {"value": "Level 4", "confidence": 0.75},
            "successful": {"value": True, "confidence": 0.92,
                           "rationale": "initials present"},
        })
    return {
        "meet": {
            "competition_name": {"value": competition_name, "confidence": 0.97},
            "host_club": {"value": "AAC", "confidence": 0.9},
            "coc": {"value": "Carol Smith", "confidence": 0.85},
        },
        "session": {
            "competition_coordinator": {"value": "Alex A", "confidence": 0.9},
            "cc_level": {"value": "Level 5", "confidence": 0.8},
            "date_session": {"value": "2026-01-09", "confidence": 0.93},
            "session_number": {"value": 1, "confidence": 0.95,
                               "source": session_source},
        },
        "rows": rows,
    }


def _client_returning(*json_objects_or_text) -> MagicMock:
    """Build a fake client whose generate() returns the given payloads in
    order. Each item may be a dict (JSON-dumped) or a raw string."""
    client = MagicMock()
    responses = []
    for item in json_objects_or_text:
        text = item if isinstance(item, str) else json.dumps(item)
        responses.append(SimpleNamespace(response=text))
    client.generate.side_effect = responses
    return client


# ---------------------------------------------------------------------------
# Prompt construction
# ---------------------------------------------------------------------------


class TestBuildPrompt:
    def test_includes_filename(self):
        prompt = build_prompt(ONTARIO, "meet-session_3.pdf")
        assert "meet-session_3.pdf" in prompt

    def test_includes_template_addendum(self):
        prompt = build_prompt(ONTARIO, "x.pdf")
        # Swim Ontario's addendum mentions ditto marks and the
        # whole-row successful judgement.
        assert "Swim Ontario" in prompt
        assert "ditto" in prompt.lower()

    def test_includes_canonical_field_names(self):
        prompt = build_prompt(ONTARIO, "x.pdf")
        for field in (s.OFFICIAL_NAME, s.SUCCESSFUL, s.SESSION_NUMBER,
                      s.COMPETITION_NAME, s.MENTOR):
            assert field in prompt

    def test_explains_session_number_source(self):
        prompt = build_prompt(ONTARIO, "x.pdf")
        assert "form+filename" in prompt
        assert "source" in prompt


# ---------------------------------------------------------------------------
# Parsing a good response
# ---------------------------------------------------------------------------


class TestParseGoodResponse:
    def setup_method(self):
        self.page = vision_extract._parse_response(_good_response(), page_number=1)

    def test_page_number(self):
        assert self.page.page_number == 1

    def test_meet_fields(self):
        assert self.page.meet[s.COMPETITION_NAME].value == "Aurora Open 2026"
        assert self.page.meet[s.COMPETITION_NAME].confidence == 0.97
        assert self.page.meet[s.HOST_CLUB].value == "AAC"

    def test_session_number_with_source(self):
        sn = self.page.session[s.SESSION_NUMBER]
        assert sn.value == 1
        assert sn.source == "form"
        assert sn.confidence == 0.95

    def test_rows_parsed(self):
        assert len(self.page.rows) == 2
        assert self.page.rows[0][s.OFFICIAL_NAME].value == "Official 1"
        assert self.page.rows[0][s.POSITION].value == "Starter"

    def test_successful_is_typed_bool(self):
        suc = self.page.rows[0][s.SUCCESSFUL]
        assert suc.value is True
        assert suc.rationale == "initials present"

    def test_null_value_field_dropped(self):
        # lane_number had value=None — it should not appear in the row.
        assert s.LANE_NUMBER not in self.page.rows[0]


# ---------------------------------------------------------------------------
# Coercion: successful
# ---------------------------------------------------------------------------


class TestSuccessfulCoercion:
    @pytest.mark.parametrize("raw, expected", [
        (True, True),
        (False, False),
        (None, None),
        ("true", True),
        ("false", False),
        ("yes", True),
        ("no", False),
        ("", None),
        ("unknown", None),
        ("?", None),
        ("Pass", True),
        ("FAILED", False),
    ])
    def test_coerce_bool(self, raw, expected):
        assert vision_extract._coerce_bool(raw) == expected

    def test_stringy_false_does_not_become_true(self):
        # The important regression: a model emitting "false" must not be
        # read as a truthy non-empty string downstream.
        page = vision_extract._parse_response({
            "rows": [{
                "official_name": {"value": "X", "confidence": 0.9},
                "successful": {"value": "false", "confidence": 0.8},
            }],
        }, page_number=1)
        assert page.rows[0][s.SUCCESSFUL].value is False


# ---------------------------------------------------------------------------
# Confidence clamping
# ---------------------------------------------------------------------------


class TestConfidenceClamping:
    @pytest.mark.parametrize("raw, expected", [
        (0.9, 0.9),
        (1.5, 1.0),     # over-range clamps down
        (-0.3, 0.0),    # under-range clamps up
        ("0.7", 0.7),   # stringy float
        (None, 0.5),    # missing → default
        ("abc", 0.5),   # garbage → default
    ])
    def test_clamp(self, raw, expected):
        assert vision_extract._clamp_confidence(raw) == expected

    def test_bare_scalar_field_gets_default_confidence(self):
        # Model emitted a bare string instead of {value, confidence}.
        page = vision_extract._parse_response({
            "meet": {"competition_name": "Stray Meet"},
            "rows": [],
        }, page_number=1)
        fv = page.meet[s.COMPETITION_NAME]
        assert fv.value == "Stray Meet"
        assert fv.confidence == 0.5


# ---------------------------------------------------------------------------
# Model call + retry
# ---------------------------------------------------------------------------


class TestModelCallAndRetry:
    def test_single_call_when_valid(self):
        client = _client_returning(_good_response(n_rows=1))
        page, raw = extract_page(
            b"PNGBYTES", ONTARIO, "x.pdf", 1, client=client, model="m",
        )
        assert client.generate.call_count == 1
        assert len(page.rows) == 1

    def test_passes_image_and_json_format(self):
        client = _client_returning(_good_response(n_rows=1))
        extract_page(b"PNGBYTES", ONTARIO, "x.pdf", 1, client=client, model="qwen2.5vl:7b")
        _, kwargs = client.generate.call_args
        assert kwargs["model"] == "qwen2.5vl:7b"
        assert kwargs["images"] == [b"PNGBYTES"]
        assert kwargs["format"] == "json"
        assert kwargs["options"]["temperature"] == 0

    def test_retries_once_on_bad_json_then_succeeds(self):
        client = _client_returning("not json at all", _good_response(n_rows=1))
        page, _ = extract_page(b"x", ONTARIO, "x.pdf", 1, client=client, model="m")
        assert client.generate.call_count == 2
        assert len(page.rows) == 1

    def test_retry_nudge_appended_on_second_call(self):
        client = _client_returning("garbage", _good_response(n_rows=1))
        extract_page(b"x", ONTARIO, "x.pdf", 1, client=client, model="m")
        first_prompt = client.generate.call_args_list[0].kwargs["prompt"]
        second_prompt = client.generate.call_args_list[1].kwargs["prompt"]
        assert len(second_prompt) > len(first_prompt)
        assert "not valid JSON" in second_prompt

    def test_raises_after_two_failures(self):
        client = _client_returning("garbage", "still garbage")
        with pytest.raises(VisionExtractionError):
            extract_page(b"x", ONTARIO, "x.pdf", 1, client=client, model="m")

    def test_ollama_response_error_becomes_clean_vision_error(self):
        # A 500 from the model server (e.g. VRAM / GGML assert) must
        # surface as a VisionExtractionError with actionable guidance,
        # not a raw traceback — and we don't retry a deterministic 500.
        import ollama
        client = MagicMock()
        client.generate.side_effect = ollama.ResponseError(
            "GGML_ASSERT(a->ne[2] * 4 == b->ne[0]) failed", 500,
        )
        with pytest.raises(VisionExtractionError) as exc:
            extract_page(b"x", ONTARIO, "x.pdf", 1, client=client, model="qwen2.5vl:7b")
        msg = str(exc.value)
        assert "qwen2.5vl:7b" in msg
        assert "qwen2.5vl:3b" in msg          # smaller-model fallback hint
        # Points at the known Ollama regression + troubleshooting doc.
        assert "0.12.x" in msg
        assert "troubleshooting" in msg.lower()
        # Deterministic server error → single attempt, no retry.
        assert client.generate.call_count == 1

    def test_strips_markdown_fences(self):
        fenced = "```json\n" + json.dumps(_good_response(n_rows=1)) + "\n```"
        client = _client_returning(fenced)
        page, _ = extract_page(b"x", ONTARIO, "x.pdf", 1, client=client, model="m")
        # Parsed fine despite the fence → only one call, rows present.
        assert client.generate.call_count == 1
        assert len(page.rows) == 1

    def test_missing_rows_key_triggers_retry(self):
        # A dict without "rows" is structurally invalid → retry.
        client = _client_returning({"meet": {}}, _good_response(n_rows=1))
        extract_page(b"x", ONTARIO, "x.pdf", 1, client=client, model="m")
        assert client.generate.call_count == 2


# ---------------------------------------------------------------------------
# extract_pdf orchestration + cache
# ---------------------------------------------------------------------------


class TestExtractPdfOrchestration:
    def test_calls_model_once_per_page(self, tmp_path):
        client = _client_returning(_good_response(n_rows=2), _good_response(n_rows=1))
        with patch("src.vision_extract.pdf_io.page_count", return_value=2), \
             patch("src.vision_extract.pdf_io.rasterize_page", return_value=b"PNG"):
            pages = extract_pdf(
                "scan.pdf", ONTARIO, client=client, model="m",
                cache_path=None,
            )
        assert client.generate.call_count == 2
        assert len(pages) == 2
        assert len(pages[0].rows) == 2
        assert len(pages[1].rows) == 1

    def test_logs_per_page_progress(self, caplog):
        client = _client_returning(_good_response(n_rows=2), _good_response(n_rows=1))
        with patch("src.vision_extract.pdf_io.page_count", return_value=2), \
             patch("src.vision_extract.pdf_io.rasterize_page", return_value=b"PNG"), \
             caplog.at_level("INFO", logger="src.vision_extract"):
            extract_pdf("scan.pdf", ONTARIO, client=client, model="m")
        msgs = [r.message for r in caplog.records]
        # One "extracting" line and one "done" line per page, numbered N/total.
        assert any("Page 1/2: extracting" in m for m in msgs)
        assert any("Page 1/2: done" in m for m in msgs)
        assert any("Page 2/2: extracting" in m for m in msgs)
        # The done line reports the row count it parsed.
        assert any("Page 1/2: done" in m and "2 row(s)" in m for m in msgs)

    def test_logs_cache_hit(self, tmp_path, caplog):
        cache = tmp_path / "scan.raw.json"
        cache.write_text(json.dumps({
            "model": "m", "source_pdf": "scan.pdf",
            "pages": {"1": _good_response(n_rows=1)},
        }), encoding="utf-8")
        client = _client_returning()  # must not be called
        with patch("src.vision_extract.pdf_io.page_count", return_value=1), \
             patch("src.vision_extract.pdf_io.rasterize_page", return_value=b"PNG"), \
             caplog.at_level("INFO", logger="src.vision_extract"):
            extract_pdf("scan.pdf", ONTARIO, client=client, model="m",
                        cache_path=cache)
        assert any("Page 1/1: using cached" in r.message for r in caplog.records)


class TestCache:
    def test_writes_cache_then_reuses_without_calling_model(self, tmp_path):
        cache = tmp_path / "scan.raw.json"

        # First run: model called once per page, cache written.
        client1 = _client_returning(_good_response(n_rows=1))
        with patch("src.vision_extract.pdf_io.page_count", return_value=1), \
             patch("src.vision_extract.pdf_io.rasterize_page", return_value=b"PNG"):
            extract_pdf("scan.pdf", ONTARIO, client=client1, model="m",
                        cache_path=cache)
        assert client1.generate.call_count == 1
        assert cache.is_file()

        # Second run: cache hit → model NOT called.
        client2 = _client_returning(_good_response(n_rows=1))
        with patch("src.vision_extract.pdf_io.page_count", return_value=1), \
             patch("src.vision_extract.pdf_io.rasterize_page", return_value=b"PNG"):
            pages = extract_pdf("scan.pdf", ONTARIO, client=client2, model="m",
                                cache_path=cache)
        client2.generate.assert_not_called()
        assert len(pages[0].rows) == 1

    def test_no_cache_flag_forces_fresh_call(self, tmp_path):
        cache = tmp_path / "scan.raw.json"
        # Pre-seed a cache.
        cache.write_text(json.dumps({
            "model": "m", "source_pdf": "scan.pdf",
            "pages": {"1": _good_response(n_rows=5)},
        }), encoding="utf-8")

        client = _client_returning(_good_response(n_rows=1))
        with patch("src.vision_extract.pdf_io.page_count", return_value=1), \
             patch("src.vision_extract.pdf_io.rasterize_page", return_value=b"PNG"):
            pages = extract_pdf("scan.pdf", ONTARIO, client=client, model="m",
                                cache_path=cache, use_cache=False)
        # use_cache=False → model called, and we get 1 row not the
        # cached 5.
        assert client.generate.call_count == 1
        assert len(pages[0].rows) == 1

class TestSameMeetChecker:
    """make_same_meet_checker wraps the vision model as a merge.SameMeetChecker."""

    def _meet(self, name):
        return {
            s.COMPETITION_NAME: s.FieldValue(name, 0.9),
            s.HOST_CLUB: s.FieldValue("AAC", 0.9),
        }

    def test_same_verdict(self):
        client = _client_returning({"verdict": "same", "confidence": 0.88})
        checker = vision_extract.make_same_meet_checker(client, "m")
        verdict = checker(self._meet("Aurora Open 2026"),
                          self._meet("Aurora Open 2O26"))  # OCR noise
        assert verdict.verdict == "same"
        assert verdict.confidence == 0.88

    def test_different_verdict(self):
        client = _client_returning({"verdict": "different", "confidence": 0.95})
        checker = vision_extract.make_same_meet_checker(client, "m")
        verdict = checker(self._meet("Aurora Open"), self._meet("Birch Cup"))
        assert verdict.verdict == "different"

    def test_text_only_call_has_no_image(self):
        client = _client_returning({"verdict": "same", "confidence": 0.9})
        checker = vision_extract.make_same_meet_checker(client, "m")
        checker(self._meet("X"), self._meet("X"))
        # The same-meet check is text-only — no image attached.
        assert "images" not in client.generate.call_args.kwargs

    def test_garbage_response_is_unknown(self):
        client = _client_returning("not json")
        checker = vision_extract.make_same_meet_checker(client, "m")
        verdict = checker(self._meet("X"), self._meet("Y"))
        assert verdict.verdict == "unknown"
        assert verdict.confidence == 0.0

    def test_bad_verdict_string_is_unknown(self):
        client = _client_returning({"verdict": "maybe", "confidence": 0.5})
        checker = vision_extract.make_same_meet_checker(client, "m")
        verdict = checker(self._meet("X"), self._meet("Y"))
        assert verdict.verdict == "unknown"

    def test_response_error_degrades_to_unknown(self):
        # A model error in the same-meet check must NOT abort the parse —
        # degrade to "unknown" so the page carries forward for review.
        import ollama
        client = MagicMock()
        client.generate.side_effect = ollama.ResponseError("boom", 500)
        checker = vision_extract.make_same_meet_checker(client, "m")
        verdict = checker(self._meet("X"), self._meet("Y"))
        assert verdict.verdict == "unknown"
        assert verdict.confidence == 0.0


class TestCacheMore:
    def test_cache_ignored_when_model_differs(self, tmp_path):
        cache = tmp_path / "scan.raw.json"
        cache.write_text(json.dumps({
            "model": "OLD-MODEL", "source_pdf": "scan.pdf",
            "pages": {"1": _good_response(n_rows=5)},
        }), encoding="utf-8")

        client = _client_returning(_good_response(n_rows=1))
        with patch("src.vision_extract.pdf_io.page_count", return_value=1), \
             patch("src.vision_extract.pdf_io.rasterize_page", return_value=b"PNG"):
            pages = extract_pdf("scan.pdf", ONTARIO, client=client, model="NEW-MODEL",
                                cache_path=cache)
        # Different model → cache ignored → fresh call.
        assert client.generate.call_count == 1
        assert len(pages[0].rows) == 1
