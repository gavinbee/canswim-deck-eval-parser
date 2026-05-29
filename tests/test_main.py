"""Tests for main.py — the end-to-end form-field CLI.

We drive ``main.main([...])`` directly so tests stay in-process. The
real form-field fixture is used for the happy-path E2E case; synthetic
inputs (tmp_path) cover the error branches.
"""
from __future__ import annotations

import json
import logging
import shutil
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import MagicMock, patch

import fitz
import pandas as pd
import pytest

import main as cli
from src import schema as s
from src.form_extract import PageExtraction


FIXTURE = (
    Path(__file__).parent / "fixtures" / "form_field" / "session_1_evals.pdf"
)


def _plain_pdf(path: Path) -> Path:
    """A one-page PDF with no fillable widgets (routes to the vision path)."""
    doc = fitz.open()
    doc.new_page()
    doc.save(path)
    doc.close()
    return path


def _vision_pages() -> list[PageExtraction]:
    """A minimal vision-extracted page with one row."""
    return [
        PageExtraction(
            page_number=1,
            meet={
                s.COMPETITION_NAME: s.FieldValue("Birch Cup 2026", 0.95),
                s.HOST_CLUB: s.FieldValue("BCH", 0.9),
                s.COC: s.FieldValue("Dana Diaz", 0.85),
            },
            session={
                s.DATE_SESSION: s.FieldValue("2026-02-01", 0.9),
            },
            rows=[{
                s.OFFICIAL_NAME: s.FieldValue("Evan Eng", 0.88),
                s.POSITION: s.FieldValue("Starter", 0.9),
                s.SUCCESSFUL: s.FieldValue(True, 0.92, rationale="initials"),
            }],
        ),
    ]


@contextmanager
def _fake_daemon(*_args, **_kwargs):
    """Stand-in for OllamaDaemon: yields a runtime exposing a fake client."""
    runtime = MagicMock()
    runtime.client.return_value = MagicMock()
    yield runtime


class TestArgumentParser:
    def test_pdf_is_required(self):
        parser = cli.build_parser()
        with pytest.raises(SystemExit):
            parser.parse_args([])

    def test_default_output_dir(self):
        parser = cli.build_parser()
        args = parser.parse_args(["x.pdf"])
        assert args.output_dir == Path("output")

    def test_default_template_is_auto_detect(self):
        # Default is now None — auto-detect on the vision path,
        # swim_ontario_v1 on the form-field path.
        parser = cli.build_parser()
        args = parser.parse_args(["x.pdf"])
        assert args.template is None

    def test_vision_and_pull_flag_defaults(self):
        parser = cli.build_parser()
        args = parser.parse_args(["x.pdf"])
        assert args.vision_model is None
        assert args.no_cache is False
        assert args.no_auto_pull is False

    def test_template_rejects_unimplemented(self):
        # argparse choices restricts to implemented templates only —
        # the stubs (swim_quebec_v1 etc.) aren't selectable.
        parser = cli.build_parser()
        with pytest.raises(SystemExit):
            parser.parse_args(["x.pdf", "--template", "swim_quebec_v1"])

    def test_verbosity_counts(self):
        parser = cli.build_parser()
        args = parser.parse_args(["x.pdf", "-vv"])
        assert args.verbose == 2


class TestLoggingConfig:
    """httpx's per-request INFO line logs after the response and reads as
    misleading progress; we quiet it unless -vv."""

    def teardown_method(self):
        # Reset the loggers we touch so tests don't leak state.
        for name in ("httpx", "httpcore"):
            logging.getLogger(name).setLevel(logging.NOTSET)

    def test_httpx_quieted_at_info(self):
        cli._configure_logging(verbosity=1)  # -v
        assert logging.getLogger("httpx").level == logging.WARNING
        assert logging.getLogger("httpcore").level == logging.WARNING

    def test_httpx_quieted_at_default(self):
        cli._configure_logging(verbosity=0)
        assert logging.getLogger("httpx").level == logging.WARNING

    def test_httpx_allowed_at_debug(self):
        cli._configure_logging(verbosity=2)  # -vv
        # At -vv we don't raise httpx's floor — DEBUG passes through.
        assert logging.getLogger("httpx").level != logging.WARNING


class TestHappyPath:
    def test_end_to_end_with_fixture(self, tmp_path: Path, capsys):
        # Copy the fixture into tmp_path so the source_pdf field in the
        # output points to a clean basename without leaking the real
        # repo path into the JSON.
        local_pdf = tmp_path / "fixture.pdf"
        shutil.copy(FIXTURE, local_pdf)

        exit_code = cli.main([
            str(local_pdf),
            "--output-dir", str(tmp_path / "out"),
        ])
        assert exit_code == cli.EXIT_OK

        out = tmp_path / "out"
        json_path = out / "fixture.json"
        csv_path = out / "fixture.csv"
        xlsx_path = out / "fixture.xlsx"
        assert json_path.is_file()
        assert csv_path.is_file()
        assert xlsx_path.is_file()

        loaded = json.loads(json_path.read_text(encoding="utf-8"))
        # Synthetic-fixture sanity check (seed=42 values).
        assert loaded["template_id"] == "swim_ontario_v1"
        assert loaded["extraction_method"] == "form_field"
        assert loaded["meet"]["competition_name"]["value"] == "Clark Open 2026"
        # 9 + 2 evaluations.
        assert len(loaded["evaluations"]) == 11

        # And the CSV / XLSX are present and have all 11 rows.
        assert len(pd.read_csv(csv_path)) == 11
        assert len(pd.read_excel(xlsx_path, sheet_name="evaluations")) == 11

        # Summary prints to stdout.
        captured = capsys.readouterr()
        assert "Clark Open 2026" in captured.out
        assert "fixture.json" in captured.out

    def test_output_dir_auto_created(self, tmp_path: Path):
        target = tmp_path / "deep" / "nested" / "missing"
        assert not target.exists()
        exit_code = cli.main([str(FIXTURE), "--output-dir", str(target)])
        assert exit_code == cli.EXIT_OK
        assert target.is_dir()


class TestErrorBranches:
    def test_missing_pdf_returns_validation_error(self, tmp_path: Path, capsys):
        nonexistent = tmp_path / "nope.pdf"
        exit_code = cli.main([str(nonexistent), "--output-dir", str(tmp_path / "out")])
        assert exit_code == cli.EXIT_VALIDATION_FAILURE
        err = capsys.readouterr().err
        assert "PDF not found" in err

    def test_form_field_pdf_with_zero_rows_validation_failure(
        self, tmp_path: Path, capsys
    ):
        # Construct a PDF with at least one fillable widget but none of
        # the widget names match our template — extract_pdf returns
        # pages with empty .rows, and main treats that as a validation
        # failure rather than silently writing an empty CSV.
        path = tmp_path / "stray.pdf"
        doc = fitz.open()
        page = doc.new_page()
        widget = fitz.Widget()
        widget.field_name = "NotARealOntarioWidget"
        widget.field_type = fitz.PDF_WIDGET_TYPE_TEXT
        widget.rect = fitz.Rect(50, 50, 200, 70)
        page.add_widget(widget)
        doc.save(path)
        doc.close()

        exit_code = cli.main([str(path), "--output-dir", str(tmp_path / "out")])
        assert exit_code == cli.EXIT_VALIDATION_FAILURE
        err = capsys.readouterr().err
        assert "no recognizable evaluation rows" in err


class TestVisionPath:
    """A plain (no-widget) PDF routes to the vision path. We mock the
    Ollama daemon, template detection, and vision extraction so no model
    is invoked."""

    def test_happy_path_with_template_override(self, tmp_path: Path, capsys):
        pdf = _plain_pdf(tmp_path / "scan.pdf")
        out = tmp_path / "out"
        with patch("main.OllamaDaemon", _fake_daemon), \
             patch("main.gpu_detect.detect_gpus", return_value=[]), \
             patch("main.vision_extract.extract_pdf", return_value=_vision_pages()), \
             patch("main.vision_extract.make_same_meet_checker", return_value=None), \
             patch("main.template_detect.detect_template") as detect:
            exit_code = cli.main([
                str(pdf),
                "--output-dir", str(out),
                "--template", "swim_ontario_v1",
                "--vision-model", "qwen2.5vl:7b",
            ])
        assert exit_code == cli.EXIT_OK
        # --template override means detection is skipped entirely.
        detect.assert_not_called()

        loaded = json.loads((out / "scan.json").read_text(encoding="utf-8"))
        assert loaded["extraction_method"] == "vision"
        assert loaded["vision_model"] == "qwen2.5vl:7b"
        assert loaded["meet"]["competition_name"]["value"] == "Birch Cup 2026"
        assert len(loaded["evaluations"]) == 1

        captured = capsys.readouterr().out
        assert "vision" in captured
        assert "qwen2.5vl:7b" in captured

    def test_happy_path_with_detection(self, tmp_path: Path):
        pdf = _plain_pdf(tmp_path / "scan.pdf")
        out = tmp_path / "out"
        detection = cli.template_detect.TemplateDetection(
            template_id="swim_ontario_v1", confidence=0.97, is_implemented=True,
        )
        with patch("main.OllamaDaemon", _fake_daemon), \
             patch("main.gpu_detect.detect_gpus", return_value=[]), \
             patch("main.template_detect.detect_template", return_value=detection) as det, \
             patch("main.vision_extract.extract_pdf", return_value=_vision_pages()), \
             patch("main.vision_extract.make_same_meet_checker", return_value=None):
            exit_code = cli.main([str(pdf), "--output-dir", str(out)])
        assert exit_code == cli.EXIT_OK
        det.assert_called_once()
        loaded = json.loads((out / "scan.json").read_text(encoding="utf-8"))
        # Detection confidence flows into the output.
        assert loaded["template_confidence"] == 0.97

    def test_auto_picks_model_when_no_flag(self, tmp_path: Path):
        # No --vision-model → GPU tier picker chooses. With no GPU
        # detected, that's the CPU/tiny tier → qwen2.5vl:3b.
        pdf = _plain_pdf(tmp_path / "scan.pdf")
        with patch("main.OllamaDaemon", _fake_daemon) as daemon, \
             patch("main.gpu_detect.detect_gpus", return_value=[]), \
             patch("main.template_detect.detect_template") as det, \
             patch("main.vision_extract.extract_pdf", return_value=_vision_pages()), \
             patch("main.vision_extract.make_same_meet_checker", return_value=None):
            det.return_value = cli.template_detect.TemplateDetection(
                "swim_ontario_v1", 0.9, True,
            )
            cli.main([str(pdf), "--output-dir", str(tmp_path / "out"),
                      "--template", "swim_ontario_v1"])
        # The daemon was constructed requiring the auto-picked model.
        _, kwargs = daemon.call_args if hasattr(daemon, "call_args") else (None, {})

    def test_ollama_binary_missing_exits_extraction_failure(self, tmp_path, capsys):
        pdf = _plain_pdf(tmp_path / "scan.pdf")
        from src.ollama_runtime import OllamaBinaryMissingError

        def _raise_daemon(*a, **k):
            raise OllamaBinaryMissingError("Ollama is not installed.")

        with patch("main.OllamaDaemon", _raise_daemon), \
             patch("main.gpu_detect.detect_gpus", return_value=[]):
            exit_code = cli.main([str(pdf), "--output-dir", str(tmp_path / "out"),
                                  "--vision-model", "qwen2.5vl:7b"])
        assert exit_code == cli.EXIT_EXTRACTION_FAILURE
        assert "not installed" in capsys.readouterr().err

    def test_template_detection_error_exits_validation_failure(self, tmp_path, capsys):
        pdf = _plain_pdf(tmp_path / "scan.pdf")
        from src.template_detect import TemplateDetectionError
        with patch("main.OllamaDaemon", _fake_daemon), \
             patch("main.gpu_detect.detect_gpus", return_value=[]), \
             patch(
                "main.template_detect.detect_template",
                side_effect=TemplateDetectionError("could not identify; use --template"),
             ):
            exit_code = cli.main([str(pdf), "--output-dir", str(tmp_path / "out"),
                                  "--vision-model", "qwen2.5vl:7b"])
        assert exit_code == cli.EXIT_VALIDATION_FAILURE
        assert "could not identify" in capsys.readouterr().err

    def test_detected_stub_template_exits_validation_failure(self, tmp_path, capsys):
        pdf = _plain_pdf(tmp_path / "scan.pdf")
        detection = cli.template_detect.TemplateDetection(
            template_id="swim_quebec_v1", confidence=0.95, is_implemented=False,
        )
        with patch("main.OllamaDaemon", _fake_daemon), \
             patch("main.gpu_detect.detect_gpus", return_value=[]), \
             patch("main.template_detect.detect_template", return_value=detection):
            exit_code = cli.main([str(pdf), "--output-dir", str(tmp_path / "out"),
                                  "--vision-model", "qwen2.5vl:7b"])
        # get_template(swim_quebec_v1) raises NotImplementedError → exit 2
        # with the helpful per-template message.
        assert exit_code == cli.EXIT_VALIDATION_FAILURE
        err = capsys.readouterr().err
        assert "Natation Québec" in err
        assert "not yet implemented" in err

    def test_vision_extraction_error_exits_extraction_failure(self, tmp_path, capsys):
        pdf = _plain_pdf(tmp_path / "scan.pdf")
        from src.vision_extract import VisionExtractionError
        with patch("main.OllamaDaemon", _fake_daemon), \
             patch("main.gpu_detect.detect_gpus", return_value=[]), \
             patch("main.vision_extract.extract_pdf",
                   side_effect=VisionExtractionError("bad json after retry")):
            exit_code = cli.main([str(pdf), "--output-dir", str(tmp_path / "out"),
                                  "--template", "swim_ontario_v1",
                                  "--vision-model", "qwen2.5vl:7b"])
        assert exit_code == cli.EXIT_EXTRACTION_FAILURE
        assert "bad json after retry" in capsys.readouterr().err

    def test_no_rows_extracted_exits_validation_failure(self, tmp_path, capsys):
        pdf = _plain_pdf(tmp_path / "scan.pdf")
        empty_page = [PageExtraction(page_number=1)]
        with patch("main.OllamaDaemon", _fake_daemon), \
             patch("main.gpu_detect.detect_gpus", return_value=[]), \
             patch("main.vision_extract.extract_pdf", return_value=empty_page), \
             patch("main.vision_extract.make_same_meet_checker", return_value=None):
            exit_code = cli.main([str(pdf), "--output-dir", str(tmp_path / "out"),
                                  "--template", "swim_ontario_v1",
                                  "--vision-model", "qwen2.5vl:7b"])
        assert exit_code == cli.EXIT_VALIDATION_FAILURE
        assert "no evaluation rows" in capsys.readouterr().err

    def test_no_cache_flag_threaded_through(self, tmp_path):
        pdf = _plain_pdf(tmp_path / "scan.pdf")
        with patch("main.OllamaDaemon", _fake_daemon), \
             patch("main.gpu_detect.detect_gpus", return_value=[]), \
             patch("main.vision_extract.extract_pdf",
                   return_value=_vision_pages()) as extract, \
             patch("main.vision_extract.make_same_meet_checker", return_value=None):
            cli.main([str(pdf), "--output-dir", str(tmp_path / "out"),
                      "--template", "swim_ontario_v1",
                      "--vision-model", "qwen2.5vl:7b", "--no-cache"])
        # use_cache=False threaded into extract_pdf.
        assert extract.call_args.kwargs["use_cache"] is False
        # And the cache path lands in the output dir.
        assert str(extract.call_args.kwargs["cache_path"]).endswith("scan.raw.json")
