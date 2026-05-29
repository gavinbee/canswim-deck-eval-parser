"""SwimBlocks Deck Eval Parser — CLI entry point.

Single-PDF orchestrator with two extraction paths:

* **Form-field fast path** — fillable PDFs (eval-gen output, online-form
  exports). Deterministic, no model needed.
* **Vision path** — scanned / flat PDFs. Manages the Ollama daemon,
  picks a model for the detected GPU tier, detects the provincial
  template from page 1, and extracts each page with the vision model.

Both paths converge on ``merge`` → ``output`` (JSON + CSV + XLSX).

Flags still reserved for later issues (interactive review / edit loop):
``--interactive`` / ``--review-all`` / ``--edit-model`` /
``--low-confidence-threshold`` — tracked in #12–#14.
"""
from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

from src import (
    form_extract,
    gpu_detect,
    merge,
    output,
    pdf_io,
    template_detect,
    vision_extract,
)
from src.ollama_runtime import (
    OllamaBinaryMissingError,
    OllamaDaemon,
    OllamaModelMissingError,
    OllamaRuntimeError,
)
from src.templates import TEMPLATES, get_template

log = logging.getLogger(__name__)


# Exit codes (see design doc §CLI).
EXIT_OK = 0
EXIT_EXTRACTION_FAILURE = 1
EXIT_VALIDATION_FAILURE = 2
EXIT_USER_ABORTED = 3
EXIT_MULTI_MEET = 4

# Default template used for the form-field path when the user doesn't
# pass --template. (We don't spin up the vision model just to classify a
# fillable PDF; auto-detection happens on the vision path only.)
_DEFAULT_FORM_FIELD_TEMPLATE = "swim_ontario_v1"


def build_parser() -> argparse.ArgumentParser:
    """Construct the argparse parser.

    Exposed separately so tests can introspect / invoke without spawning
    a subprocess.
    """
    p = argparse.ArgumentParser(
        prog="deck-eval-parser",
        description=(
            "Parse a Canadian swimming On-Deck Evaluation PDF into a "
            "structured JSON + CSV + XLSX. Fillable PDFs use a fast "
            "deterministic path; scanned PDFs use a local vision model."
        ),
    )
    p.add_argument("pdf", type=Path, help="Path to the input PDF.")
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
        default=None,
        help="Force a provincial template. Default: auto-detect from "
             "page 1 (vision path) or swim_ontario_v1 (form-field path).",
    )
    p.add_argument(
        "--vision-model",
        default=None,
        help="Ollama vision model tag. Default: auto-picked from "
             "detected GPU VRAM (see docs/models.md).",
    )
    p.add_argument(
        "--no-cache",
        action="store_true",
        help="Re-invoke the vision model even if a <stem>.raw.json cache "
             "exists.",
    )
    p.add_argument(
        "--no-auto-pull",
        action="store_true",
        help="Don't auto-pull missing Ollama models; error with the "
             "manual `ollama pull` command instead.",
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

    Returns the process exit code. Pulled out of ``main()`` so tests can
    drive the pipeline without going through ``sys.exit``.
    """
    pdf_path: Path = args.pdf
    if not pdf_path.is_file():
        print(f"error: PDF not found: {pdf_path}", file=sys.stderr)
        return EXIT_VALIDATION_FAILURE

    if pdf_io.has_form_fields(pdf_path):
        return _run_form_field(args, pdf_path)
    return _run_vision(args, pdf_path)


# ---------------------------------------------------------------------------
# Form-field path
# ---------------------------------------------------------------------------


def _run_form_field(args: argparse.Namespace, pdf_path: Path) -> int:
    template_id = args.template or _DEFAULT_FORM_FIELD_TEMPLATE
    if args.template is None:
        log.info(
            "Fillable PDF with no --template; defaulting to %s "
            "(pass --template to override).", template_id,
        )
    template = get_template(template_id)

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
            template_confidence=1.0,  # explicit choice / default, no detection
            extraction_method="form_field",
        )
    except merge.MultiMeetError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return EXIT_MULTI_MEET

    paths = output.write_all(result, args.output_dir)
    _print_summary(result, paths)
    return EXIT_OK


# ---------------------------------------------------------------------------
# Vision path
# ---------------------------------------------------------------------------


def _run_vision(args: argparse.Namespace, pdf_path: Path) -> int:
    log.info("No fillable form fields — using vision path")

    vision_model = _resolve_vision_model(args)

    try:
        with OllamaDaemon(
            required_models=[vision_model],
            auto_pull=not args.no_auto_pull,
        ) as runtime:
            client = runtime.client()
            return _vision_pipeline(args, pdf_path, client, vision_model)
    except OllamaBinaryMissingError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return EXIT_EXTRACTION_FAILURE
    except OllamaModelMissingError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return EXIT_EXTRACTION_FAILURE
    except OllamaRuntimeError as exc:
        print(f"error: Ollama runtime problem: {exc}", file=sys.stderr)
        return EXIT_EXTRACTION_FAILURE


def _resolve_vision_model(args: argparse.Namespace) -> str:
    """Pick the vision model: explicit flag, else GPU-tier auto-pick."""
    if args.vision_model:
        log.info("Using vision model %s (from --vision-model)", args.vision_model)
        return args.vision_model
    gpus = gpu_detect.detect_gpus()
    tier = gpu_detect.pick_tier(gpus)
    vision_model, _ = gpu_detect.recommended_models(tier)
    log.info("%s", gpu_detect.describe(gpus, tier))
    return vision_model


def _vision_pipeline(
    args: argparse.Namespace,
    pdf_path: Path,
    client,
    vision_model: str,
) -> int:
    # 1. Template: explicit override, or detect from page 1.
    try:
        template, template_confidence = _resolve_template(
            args, pdf_path, client, vision_model,
        )
    except template_detect.TemplateDetectionError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return EXIT_VALIDATION_FAILURE
    except NotImplementedError as exc:
        # A recognized-but-stubbed province (e.g. Quebec).
        print(f"error: {exc}", file=sys.stderr)
        return EXIT_VALIDATION_FAILURE

    # 2. Extract every page with the vision model (cached to .raw.json).
    cache_path = args.output_dir / f"{pdf_path.stem}.raw.json"
    try:
        pages = vision_extract.extract_pdf(
            str(pdf_path),
            template,
            client=client,
            model=vision_model,
            cache_path=cache_path,
            use_cache=not args.no_cache,
        )
    except vision_extract.VisionExtractionError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return EXIT_EXTRACTION_FAILURE

    if not pages or all(not p.rows for p in pages):
        print(
            f"error: the vision model found no evaluation rows in {pdf_path}. "
            "Check the scan quality, or try a larger --vision-model.",
            file=sys.stderr,
        )
        return EXIT_VALIDATION_FAILURE

    # 3. Merge — with a model-backed same-meet checker so multi-page
    #    scans whose headers differ only by OCR noise don't spuriously
    #    fail. Reuses the already-loaded vision model.
    checker = vision_extract.make_same_meet_checker(client, vision_model)
    try:
        result = merge.merge(
            pages,
            source_pdf=pdf_path.name,
            template_id=template.id,
            template_confidence=template_confidence,
            extraction_method="vision",
            vision_model=vision_model,
            same_meet_checker=checker,
        )
    except merge.MultiMeetError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return EXIT_MULTI_MEET

    paths = output.write_all(result, args.output_dir)
    _print_summary(result, paths)
    return EXIT_OK


def _resolve_template(
    args: argparse.Namespace,
    pdf_path: Path,
    client,
    vision_model: str,
):
    """Return ``(template, confidence)`` for the vision path."""
    if args.template:
        log.info("Using template %s (from --template)", args.template)
        return get_template(args.template), 1.0
    detection = template_detect.detect_template(
        str(pdf_path), client=client, model=vision_model,
    )
    # get_template raises NotImplementedError for a recognized stub.
    return get_template(detection.template_id), detection.confidence


# ---------------------------------------------------------------------------
# Summary
# ---------------------------------------------------------------------------


def _print_summary(result, paths: dict[str, Path]) -> None:
    """User-facing recap on stdout after a successful parse."""
    meet = result.meet
    cn = meet.competition_name.value if meet.competition_name else "(unknown)"
    hc = meet.host_club.value if meet.host_club else "(unknown)"
    n = len(result.evaluations)
    pages = max((ev.source_page for ev in result.evaluations), default=0)
    model_line = (
        f"  Vision model:  {result.vision_model}\n"
        if result.vision_model else ""
    )
    print(
        f"\nParsed {pdf_label(result.source_pdf)}:\n"
        f"  Competition:   {cn}\n"
        f"  Host club:     {hc}\n"
        f"  Template:      {result.template_id} "
        f"(confidence {result.template_confidence:.2f})\n"
        f"  Extraction:    {result.extraction_method}\n"
        f"{model_line}"
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
