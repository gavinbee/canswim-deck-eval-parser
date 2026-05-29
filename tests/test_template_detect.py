"""Tests for src/template_detect.py.

The vision client is mocked. We patch pdf_io.rasterize_page so no real
PDF is needed for the classification-logic tests.
"""
from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from src import template_detect
from src.template_detect import (
    DEFAULT_CONFIDENCE_THRESHOLD,
    TemplateDetection,
    TemplateDetectionError,
    build_prompt,
    detect_template,
)


def _client_returning(payload) -> MagicMock:
    client = MagicMock()
    text = payload if isinstance(payload, str) else json.dumps(payload)
    client.generate.return_value = SimpleNamespace(response=text)
    return client


def _detect(payload, **kwargs):
    client = _client_returning(payload)
    with patch("src.template_detect.pdf_io.rasterize_page", return_value=b"PNG"):
        return detect_template("scan.pdf", client=client, model="m", **kwargs), client


# ---------------------------------------------------------------------------
# Prompt
# ---------------------------------------------------------------------------


class TestBuildPrompt:
    def test_lists_implemented_and_stub_ids(self):
        prompt = build_prompt()
        assert "swim_ontario_v1" in prompt
        assert "swim_quebec_v1" in prompt
        assert "swim_alberta_v1" in prompt
        assert "swim_bc_v1" in prompt

    def test_includes_display_names(self):
        prompt = build_prompt()
        assert "Swim Ontario On-Deck Evaluation" in prompt
        assert "Natation Québec" in prompt

    def test_mentions_unknown_escape_hatch(self):
        prompt = build_prompt()
        assert "unknown" in prompt


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------


class TestDetectImplementedTemplate:
    def test_returns_detection_for_ontario(self):
        detection, client = _detect(
            {"template_id": "swim_ontario_v1", "confidence": 0.96}
        )
        assert detection.template_id == "swim_ontario_v1"
        assert detection.confidence == 0.96
        assert detection.is_implemented is True
        client.generate.assert_called_once()

    def test_passes_image_and_json_format(self):
        _, client = _detect({"template_id": "swim_ontario_v1", "confidence": 0.9})
        kwargs = client.generate.call_args.kwargs
        assert kwargs["images"] == [b"PNG"]
        assert kwargs["format"] == "json"
        assert kwargs["options"]["temperature"] == 0


class TestDetectStubTemplate:
    def test_stub_detected_but_marked_unimplemented(self):
        # The model confidently recognises a Quebec form. We return it
        # (so the caller can show the specific "not yet implemented"
        # message) but flag is_implemented=False.
        detection, _ = _detect(
            {"template_id": "swim_quebec_v1", "confidence": 0.93}
        )
        assert detection.template_id == "swim_quebec_v1"
        assert detection.is_implemented is False


# ---------------------------------------------------------------------------
# Error paths
# ---------------------------------------------------------------------------


class TestDetectionErrors:
    def test_unknown_raises(self):
        with pytest.raises(TemplateDetectionError) as exc:
            _detect({"template_id": "unknown", "confidence": 0.1})
        assert "--template" in str(exc.value)
        assert "file an issue" in str(exc.value)

    def test_unrecognised_id_raises(self):
        # Model hallucinates a province we don't have registered.
        with pytest.raises(TemplateDetectionError):
            _detect({"template_id": "swim_yukon_v1", "confidence": 0.99})

    def test_below_threshold_raises(self):
        with pytest.raises(TemplateDetectionError) as exc:
            _detect({"template_id": "swim_ontario_v1", "confidence": 0.5})
        assert "Low-confidence" in str(exc.value)
        assert "0.50" in str(exc.value)

    def test_custom_threshold_respected(self):
        # 0.5 passes if the caller lowers the bar.
        detection, _ = _detect(
            {"template_id": "swim_ontario_v1", "confidence": 0.55},
            threshold=0.5,
        )
        assert detection.template_id == "swim_ontario_v1"

    def test_default_threshold_value(self):
        # Guard against an accidental change to the default.
        assert DEFAULT_CONFIDENCE_THRESHOLD == 0.7

    def test_non_dict_response_raises(self):
        with pytest.raises(TemplateDetectionError) as exc:
            _detect("not json at all")
        assert "did not return a usable classification" in str(exc.value)

    def test_missing_confidence_treated_as_zero(self):
        # No confidence key → 0.0 → below threshold → raises.
        with pytest.raises(TemplateDetectionError):
            _detect({"template_id": "swim_ontario_v1"})

    def test_ollama_response_error_becomes_clean_detection_error(self):
        # A model/server error during detection surfaces as a clean
        # TemplateDetectionError with smaller-model guidance, not a
        # traceback.
        import ollama
        client = MagicMock()
        client.generate.side_effect = ollama.ResponseError("GGML assert", 500)
        with patch("src.template_detect.pdf_io.rasterize_page", return_value=b"PNG"):
            with pytest.raises(TemplateDetectionError) as exc:
                detect_template("scan.pdf", client=client, model="qwen2.5vl:7b")
        assert "qwen2.5vl:3b" in str(exc.value)


# ---------------------------------------------------------------------------
# Confidence clamping
# ---------------------------------------------------------------------------


class TestClamp:
    @pytest.mark.parametrize("raw, expected", [
        (0.8, 0.8),
        (1.4, 1.0),
        (-0.2, 0.0),
        ("0.9", 0.9),
        (None, 0.0),
        ("garbage", 0.0),
    ])
    def test_clamp(self, raw, expected):
        assert template_detect._clamp(raw) == expected
