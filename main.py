"""canswim-deck-eval-parser — CLI entry point.

Single-PDF orchestrator. v1 supports the **form-field fast path** only:
fillable PDFs (e.g. eval-gen output, online-form exports) parse cleanly;
scanned/flat PDFs exit with a clear "vision path not yet implemented"
message that lists the issues to follow.

Future work — and the corresponding flags reserved in the design doc but
not yet wired here — is tracked on GitHub:

    --vision-model / --edit-model / --no-cache / --no-auto-pull
        Vision extraction + Ollama lifecycle (#6, #8, #10)
    --interactive / --review-all / --low-confidence-threshold
        Interactive review + agentic edit loop (#12, #13, #14)
"""
from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

from src import form_extract, merge, output, pdf_io
from src.templates import TEMPLATES, get_template

log = logging.getLogger(__name__)


# Exit codes (see design doc §CLI).
EXIT_OK = 0
EXIT_EXTRACTION_FAILURE = 1
EXIT_VALIDATION_FAILURE = 2
EXIT_USER_ABORTED = 3
EXIT_MULTI_MEET = 4


def build_parser() -> argparse.ArgumentParser:
    """Construct the argparse parser.

    Exposed separately so tests can introspect / invoke without spawning
    a subprocess.
    """
    p = argparse.ArgumentParser(
        prog="canswim-deck-eval-parser",
        description=(
            "Parse a Canadian swimming On-Deck Evaluation PDF into a "
            "structured JSON + CSV + XLSX. v1 supports fillable PDFs "
            "(e.g. eval-gen output) only — scanned PDFs route through "
            "a local vision model and are not yet implemented."
        ),
    )
    p.add_argument(
        "pdf",
        type=Path,
        help="Path to the input PDF.",
    )
    p.add_argument(
        "--output-dir",
        type=Path,
        default=Path("output"),
        help="Directory to write JSON/CSV/XLSX into. Created if missing. "
             "Default: ./output",
    )
    p.add_argument(
        "--template",
        choices=sorted(TEMPLATES),
        default="swim_ontario_v1",
        help="Provincial template to parse against. Default: "
             "swim_ontario_v1. Once template detection lands (#9) the "
             "default becomes 'auto-detect'.",
    )
    p.add_argument(
        "-v", "--verbose",
        action="count",
        default=0,
        help="Increase log verbosity. -v for INFO, -vv for DEBUG.",
    )
    return p


def _configure_logging(verbosity: int) -> None:
    level = logging.WARNING
    if verbosity == 1:
        level = logging.INFO
    elif verbosity >= 2:
        level = logging.DEBUG
    logging.basicConfig(
        level=level,
        format="%(asctime)s  %(levelname)-7s  %(message)s",
        datefmt="%H:%M:%S",
    )


def run(args: argparse.Namespace) -> int:
    """Execute the parse and write outputs.

    Returns the process exit code (0 on success, otherwise one of the
    EXIT_* constants). Pulled out of ``main()`` so tests can drive the
    pipeline without going through ``sys.exit``.
    """
    pdf_path: Path = args.pdf
    if not pdf_path.is_file():
        print(f"error: PDF not found: {pdf_path}", file=sys.stderr)
        return EXIT_VALIDATION_FAILURE

    template = get_template(args.template)
    log.info("Using template %s", template.id)

    # Form-field fast path only in v1.
    if not pdf_io.has_form_fields(pdf_path):
        print(
            f"error: {pdf_path} has no fillable form fields.\n"
            "       Vision extraction for scanned PDFs is not yet "
            "implemented (see issues #6 → #10).",
            file=sys.stderr,
        )
        return EXIT_EXTRACTION_FAILURE

    log.info("Detected fillable PDF — using form-field path")
    pages = form_extract.extract_pdf(str(pdf_path), template)
    if not pages or all(not p.rows for p in pages):
        print(
            f"error: {pdf_path} has form fields but no recognizable "
            "evaluation rows. Is this the right template?",
            file=sys.stderr,
        )
        return EXIT_VALIDATION_FAILURE

    try:
        result = merge.merge(
            pages,
            source_pdf=pdf_path.name,
            template_id=template.id,
            template_confidence=1.0,  # explicit user choice (no detection yet)
            extraction_method="form_field",
        )
    except merge.MultiMeetError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return EXIT_MULTI_MEET

    paths = output.write_all(result, args.output_dir)
    _print_summary(result, paths)
    return EXIT_OK


def _print_summary(result, paths: dict[str, Path]) -> None:
    """User-facing recap on stdout after a successful parse."""
    meet = result.meet
    cn = meet.competition_name.value if meet.competition_name else "(unknown)"
    hc = meet.host_club.value if meet.host_club else "(unknown)"
    n = len(result.evaluations)
    pages = max((ev.source_page for ev in result.evaluations), default=0)
    print(
        f"\nParsed {pdf_label(result.source_pdf)}:\n"
        f"  Competition:   {cn}\n"
        f"  Host club:     {hc}\n"
        f"  Template:      {result.template_id}\n"
        f"  Extraction:    {result.extraction_method}\n"
        f"  Evaluations:   {n} across {pages} page(s)\n"
        f"  Wrote:         {paths['json'].name}, {paths['csv'].name}, {paths['xlsx'].name}\n"
        f"  Output dir:    {paths['json'].parent}"
    )


def pdf_label(source_pdf: str) -> str:
    """Quote-or-truncate a source filename for clean summary output."""
    return source_pdf if len(source_pdf) <= 60 else source_pdf[:57] + "..."


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    _configure_logging(args.verbose)
    return run(args)


if __name__ == "__main__":
    sys.exit(main())
