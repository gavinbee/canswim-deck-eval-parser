"""Detect which provincial template a PDF uses, from page 1.

The first thing the pipeline does with a scanned PDF is figure out which
provincial On-Deck Evaluation template it is — that choice drives the
field labels, the vision-prompt addendum, the date formats, and the
form-field widget map for everything downstream.

We rasterize page 1, show it to the vision model, and ask it to classify
against the registry's known template IDs (implemented *and* stubs).
Recognising a stub is useful: if the model confidently says "this is a
Natation Québec form", the caller can surface the specific
"not implemented yet, see issue X" message via
``templates.get_template`` rather than a vague "couldn't identify".

If the model isn't confident (below threshold) or says ``unknown``, we
raise :class:`TemplateDetectionError` telling the user to re-run with an
explicit ``--template`` override.
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

import ollama

from . import pdf_io
from .templates import TEMPLATES, TEMPLATE_STUBS, known_template_ids
from .vision_extract import (
    DEFAULT_VISION_MODEL,
    VisionClient,
    describe_model_error,
    try_parse_json,
)

log = logging.getLogger(__name__)


# Below this, we refuse to guess and ask the user to pass --template.
DEFAULT_CONFIDENCE_THRESHOLD = 0.7


@dataclass(frozen=True)
class TemplateDetection:
    """Result of classifying page 1.

    ``is_implemented`` distinguishes a detected-and-parseable template
    from a detected-but-stubbed one (e.g. Quebec), so the caller can
    choose the right message.
    """

    template_id: str
    confidence: float
    is_implemented: bool


class TemplateDetectionError(RuntimeError):
    """Raised when the template can't be identified confidently."""


def _template_descriptions() -> dict[str, str]:
    """``{id: display_name}`` for every known template (impl + stubs)."""
    out: dict[str, str] = {tid: t.display_name for tid, t in TEMPLATES.items()}
    out.update(TEMPLATE_STUBS)
    return dict(sorted(out.items()))


def build_prompt() -> str:
    """Classification prompt listing every known template ID."""
    lines = [
        f'  - "{tid}": {name}'
        for tid, name in _template_descriptions().items()
    ]
    catalog = "\n".join(lines)
    return f"""\
You are looking at page 1 of a Canadian swimming On-Deck Evaluation form.
Identify which provincial template it is. The known templates are:

{catalog}

Respond with a SINGLE JSON object, nothing else:

{{"template_id": "<one of the IDs above, or \\"unknown\\">", "confidence": <0.0-1.0>}}

Base your answer on the form's title, the issuing organisation's name or
logo, the language, and the field layout. If you genuinely can't tell,
use "unknown" with a low confidence. Output ONLY the JSON object."""


def detect_template(
    pdf_path: str,
    *,
    client: VisionClient,
    model: str = DEFAULT_VISION_MODEL,
    threshold: float = DEFAULT_CONFIDENCE_THRESHOLD,
    dpi: int = pdf_io.DEFAULT_DPI,
) -> TemplateDetection:
    """Classify page 1 of ``pdf_path`` against the template registry.

    Args:
        pdf_path: Path to the PDF.
        client: Vision client (real ``ollama.Client`` or a test stub).
        model: Vision model tag.
        threshold: Minimum confidence to accept a classification.
        dpi: Rasterization DPI for page 1.

    Returns:
        A :class:`TemplateDetection`. The id may be a not-yet-implemented
        stub — the caller decides how to handle that (typically by
        calling ``templates.get_template`` which raises a helpful
        ``NotImplementedError``).

    Raises:
        TemplateDetectionError: if the model returns ``unknown``, an
            unrecognised id, or a confidence below ``threshold``.
    """
    log.info(
        "Detecting template from page 1 of %s with %s …",
        Path(pdf_path).name, model,
    )
    start = time.monotonic()
    png = pdf_io.rasterize_page(pdf_path, 0, dpi=dpi)
    prompt = build_prompt()

    try:
        response = client.generate(
            model=model,
            prompt=prompt,
            images=[png],
            format="json",
            options={"temperature": 0},
        )
    except ollama.ResponseError as exc:
        # A model/server error during detection is a runtime failure, not
        # an "unidentifiable template" — surface it with the same
        # smaller-model / update guidance as the extraction path.
        raise TemplateDetectionError(describe_model_error(model, exc)) from exc
    text = getattr(response, "response", None)
    if text is None and isinstance(response, dict):
        text = response.get("response")
    parsed = try_parse_json(text or "")

    detection = _interpret(parsed, threshold=threshold, pdf_path=pdf_path)
    log.info(
        "Detected template %s (confidence %.2f) in %.1fs",
        detection.template_id, detection.confidence, time.monotonic() - start,
    )
    return detection


def _interpret(
    parsed: object,
    *,
    threshold: float,
    pdf_path: str,
) -> TemplateDetection:
    """Validate the model's JSON and turn it into a TemplateDetection."""
    name = Path(pdf_path).name
    override_hint = (
        "Re-run with an explicit template, e.g. "
        "`--template swim_ontario_v1`."
    )

    if not isinstance(parsed, dict):
        raise TemplateDetectionError(
            f"Could not identify the template for {name}: the model did "
            f"not return a usable classification. {override_hint}"
        )

    template_id = parsed.get("template_id")
    confidence = _clamp(parsed.get("confidence"))

    if template_id == "unknown" or template_id not in known_template_ids():
        raise TemplateDetectionError(
            f"Could not confidently identify the template for {name} "
            f"(model said {template_id!r}). {override_hint} "
            "If this is a province we don't support yet, please file an "
            "issue with a sample PDF: "
            "https://github.com/gavinbee/canswim-deck-eval-parser/issues/new"
        )

    if confidence < threshold:
        raise TemplateDetectionError(
            f"Low-confidence template detection for {name}: best guess "
            f"was {template_id!r} at {confidence:.2f} (threshold "
            f"{threshold:.2f}). {override_hint}"
        )

    return TemplateDetection(
        template_id=template_id,
        confidence=confidence,
        is_implemented=template_id in TEMPLATES,
    )


def _clamp(value: object) -> float:
    try:
        c = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return 0.0
    return max(0.0, min(1.0, c))
