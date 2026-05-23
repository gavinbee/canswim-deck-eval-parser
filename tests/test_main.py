"""Tests for main.py — the end-to-end form-field CLI.

We drive ``main.main([...])`` directly so tests stay in-process. The
real form-field fixture is used for the happy-path E2E case; synthetic
inputs (tmp_path) cover the error branches.
"""
from __future__ import annotations

import json
import shutil
from pathlib import Path

import pandas as pd
import pytest

import main as cli


FIXTURE = (
    Path(__file__).parent / "fixtures" / "form_field" / "session_1_evals.pdf"
)


class TestArgumentParser:
    def test_pdf_is_required(self):
        parser = cli.build_parser()
        with pytest.raises(SystemExit):
            parser.parse_args([])

    def test_default_output_dir(self):
        parser = cli.build_parser()
        args = parser.parse_args(["x.pdf"])
        assert args.output_dir == Path("output")

    def test_default_template_is_ontario(self):
        parser = cli.build_parser()
        args = parser.parse_args(["x.pdf"])
        assert args.template == "swim_ontario_v1"

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

    def test_pdf_without_form_fields_routes_to_vision_error(
        self, tmp_path: Path, capsys
    ):
        # Build a plain PDF with no fillable widgets. v1 doesn't have
        # vision yet, so we expect a clear error.
        import fitz
        plain = tmp_path / "plain.pdf"
        doc = fitz.open()
        doc.new_page()
        doc.save(plain)
        doc.close()

        exit_code = cli.main([str(plain), "--output-dir", str(tmp_path / "out")])
        assert exit_code == cli.EXIT_EXTRACTION_FAILURE
        err = capsys.readouterr().err
        assert "no fillable form fields" in err
        assert "Vision extraction" in err

    def test_form_field_pdf_with_zero_rows_validation_failure(
        self, tmp_path: Path, capsys
    ):
        # Construct a PDF with at least one fillable widget but none of
        # the widget names match our template — extract_pdf returns
        # pages with empty .rows, and main treats that as a validation
        # failure rather than silently writing an empty CSV.
        import fitz
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
