"""Vision-LLM extraction for scanned / flat PDFs.

For PDFs without fillable widgets, each page is rasterized to PNG and
handed to a local vision model (Qwen2.5-VL via Ollama). The model returns
a JSON object per page that we parse into the same ``PageExtraction``
shape the form-field path emits, so ``merge`` and ``output`` don't care
which path produced a page.

What this module owns:

* **Prompt construction** — a canonical-schema JSON skeleton plus the
  detected template's ``vision_prompt_addendum`` plus the source
  filename (so the model can resolve ``session_number`` from the form
  text, the filename, or both).
* **The model call** — ``client.generate(..., format="json")`` at
  temperature 0, with the page PNG as the image.
* **Parsing + validation** — turn the model's JSON into typed
  ``FieldValue`` objects, coercing ``successful`` to bool/null and
  capturing per-field confidence. One retry on a structural parse
  failure with a clarifying nudge appended.
* **The ``.raw.json`` cache** — raw model responses are written to a
  sidecar so re-runs (and parsing-logic iteration) don't re-invoke the
  slow model. ``use_cache=False`` (the CLI's ``--no-cache``) forces a
  fresh call.
"""
from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any, Optional, Protocol

from . import pdf_io, schema as s
from .form_extract import PageExtraction
from .templates.base import Template

log = logging.getLogger(__name__)


DEFAULT_VISION_MODEL = "qwen2.5vl:7b"

# Default confidence when the model gives a value but omits a confidence.
# Mid-scale so it neither hides (too high) nor screams (too low) — the
# missing confidence itself is a mild signal something's off.
_DEFAULT_CONFIDENCE = 0.5

# Truthy / falsy spellings the model might emit for ``successful`` before
# we've forced it to a real boolean.
_TRUE_WORDS = {"true", "yes", "y", "1", "signed", "signed off", "pass", "passed"}
_FALSE_WORDS = {"false", "no", "n", "0", "fail", "failed", "unsuccessful"}


# ---------------------------------------------------------------------------
# Minimal client protocol (so tests can inject a stub)
# ---------------------------------------------------------------------------


class VisionClient(Protocol):
    """The slice of ``ollama.Client`` this module uses."""

    def generate(self, *args: Any, **kwargs: Any) -> Any: ...


class VisionExtractionError(RuntimeError):
    """Raised when the model output can't be parsed even after a retry."""


# ---------------------------------------------------------------------------
# Prompt construction
# ---------------------------------------------------------------------------


# The JSON skeleton the model is asked to fill. Built from canonical
# schema names so it can never drift from src.schema.
def _json_skeleton() -> str:
    def field(extra: str = "") -> str:
        return '{"value": <string or null>, "confidence": <0.0-1.0>' + extra + "}"

    meet = ",\n    ".join(f'"{k}": {field()}' for k in s.MEET_FIELDS)
    session_keys = [
        s.COMPETITION_COORDINATOR, s.CC_LEVEL, s.DATE_SESSION, s.SESSION_NUMBER,
    ]
    session_lines = []
    for k in session_keys:
        if k == s.SESSION_NUMBER:
            session_lines.append(
                f'"{k}": {{"value": <integer or null>, "confidence": <0.0-1.0>, '
                '"source": "form" | "filename" | "form+filename" | "unknown"}'
            )
        else:
            session_lines.append(f'"{k}": {field()}')
    session = ",\n    ".join(session_lines)

    row_keys = [
        s.OFFICIAL_NAME, s.CLUB, s.POSITION, s.LANE_NUMBER,
        s.TIMES_WORKED_POSITION, s.MENTOR, s.LEVEL,
    ]
    row = ",\n      ".join(f'"{k}": {field()}' for k in row_keys)
    successful = (
        f'"{s.SUCCESSFUL}": {{"value": true | false | null, '
        '"confidence": <0.0-1.0>, "rationale": <short string>}'
    )

    return f"""{{
  "meet": {{
    {meet}
  }},
  "session": {{
    {session}
  }},
  "rows": [
    {{
      {row},
      {successful}
    }}
  ]
}}"""


_BASE_INSTRUCTIONS = """\
You are extracting structured data from one page of a swimming On-Deck \
Evaluation form. The page image is attached.

Return a SINGLE JSON object, nothing else, matching exactly this shape \
(one entry in "rows" per official visible on the page; omit blank rows):

{skeleton}

Rules:
- Every field is an object with a "value" and a "confidence" in [0.0, 1.0].
  Use null for "value" when a field is blank or unreadable. Set confidence
  to how sure you are of the value you read — be honest; low confidence on
  messy handwriting is expected and useful.
- "meet" fields are the same for the whole page (competition name, host
  club, COC).
- "session" fields describe this session. For "session_number", read it
  from the form's date/session text if present; otherwise infer it from
  the source filename "{filename}"; set "source" to where you got it
  ("form", "filename", "form+filename", or "unknown" if you truly can't
  tell).
- "rows" is one object per official. Read names, clubs, positions, lane
  numbers, etc. as written.
- "successful": judge the WHOLE ROW, not just the initials cell. Emit true
  if the official was clearly signed off, false if clearly not, null if
  genuinely ambiguous. Put your reasoning in "rationale".
{addendum}

Output ONLY the JSON object."""


def build_prompt(template: Template, filename: str) -> str:
    """Assemble the page-extraction prompt for a given template + file."""
    addendum = template.vision_prompt_addendum.strip()
    addendum_block = (
        f"\nTemplate-specific notes:\n{addendum}" if addendum else ""
    )
    return _BASE_INSTRUCTIONS.format(
        skeleton=_json_skeleton(),
        filename=filename,
        addendum=addendum_block,
    )


# ---------------------------------------------------------------------------
# Public extraction entry points
# ---------------------------------------------------------------------------


def extract_pdf(
    pdf_path: str,
    template: Template,
    *,
    client: VisionClient,
    model: str = DEFAULT_VISION_MODEL,
    cache_path: Optional[str | Path] = None,
    use_cache: bool = True,
    dpi: int = pdf_io.DEFAULT_DPI,
) -> list[PageExtraction]:
    """Vision-extract every page of ``pdf_path``.

    Args:
        pdf_path: Path to the (scanned / flat) PDF.
        template: Detected template — supplies the prompt addendum.
        client: Anything with a ``generate`` method (real ``ollama.Client``
            or a test stub).
        model: Ollama model tag.
        cache_path: Where to read/write the ``.raw.json`` sidecar. If
            ``None``, no caching.
        use_cache: If True and a matching cache exists, re-parse cached
            raw responses instead of calling the model.
        dpi: Rasterization DPI.

    Returns:
        One ``PageExtraction`` per page.
    """
    filename = Path(pdf_path).name
    n_pages = pdf_io.page_count(pdf_path)

    cached_raw = _load_cache(cache_path, model) if (use_cache and cache_path) else None

    pages: list[PageExtraction] = []
    raw_by_page: dict[str, Any] = {}

    for i in range(n_pages):
        page_no = i + 1
        cached = cached_raw.get(str(page_no)) if cached_raw else None
        if cached is not None:
            log.debug("Using cached vision response for page %d", page_no)
            raw = cached
        else:
            png = pdf_io.rasterize_page(pdf_path, i, dpi=dpi)
            raw = _call_model(client, model, template, filename, png)
        raw_by_page[str(page_no)] = raw
        pages.append(_parse_response(raw, page_number=page_no))

    if cache_path:
        _write_cache(cache_path, model, filename, raw_by_page)

    return pages


def extract_page(
    png_bytes: bytes,
    template: Template,
    filename: str,
    page_number: int,
    *,
    client: VisionClient,
    model: str = DEFAULT_VISION_MODEL,
) -> tuple[PageExtraction, dict]:
    """Vision-extract a single rasterized page.

    Returns the parsed ``PageExtraction`` and the raw model JSON (so
    callers can cache it).
    """
    raw = _call_model(client, model, template, filename, png_bytes)
    return _parse_response(raw, page_number=page_number), raw


# ---------------------------------------------------------------------------
# Same-meet checker (for merge's multi-page reconciliation)
# ---------------------------------------------------------------------------


_SAME_MEET_PROMPT = """\
Two pages of one scanned PDF each carry a meet header. Because the text
was read off a scan, the same meet can appear with OCR noise, different
abbreviations, or minor spelling differences. Decide whether these two
headers refer to the SAME swimming meet.

Page 1 header:
{page_one}

Other page header:
{page_n}

Respond with a SINGLE JSON object, nothing else:
{{"verdict": "same" | "different" | "unknown", "confidence": <0.0-1.0>}}

Use "same" if they're clearly the same meet (allowing for OCR noise),
"different" if they're clearly different meets, "unknown" if you can't
tell. Output ONLY the JSON object."""


def make_same_meet_checker(client: VisionClient, model: str):
    """Build a ``merge.SameMeetChecker`` backed by the loaded vision model.

    Reuses the already-loaded vision model (Qwen2.5-VL handles text-only
    prompts fine) rather than pulling a separate text model, so a
    multi-page scan whose headers differ only by OCR noise gets a real
    "same meet?" judgement instead of a spurious ``MultiMeetError``.

    Returns a callable matching ``merge.SameMeetChecker``: it takes the
    two pages' meet-field dicts and returns a ``merge.SameMeetVerdict``.
    """
    # Imported here (not at module top) to avoid a circular import:
    # merge doesn't import vision_extract, but vision_extract reaching
    # into merge at import time would couple their load order.
    from .merge import SameMeetVerdict

    def checker(page_one, page_n) -> "SameMeetVerdict":
        prompt = _SAME_MEET_PROMPT.format(
            page_one=_render_meet(page_one),
            page_n=_render_meet(page_n),
        )
        response = client.generate(
            model=model,
            prompt=prompt,
            format="json",
            options={"temperature": 0},
        )
        text = getattr(response, "response", None)
        if text is None and isinstance(response, dict):
            text = response.get("response")
        parsed = try_parse_json(text or "")

        verdict = "unknown"
        confidence = 0.0
        if isinstance(parsed, dict):
            v = parsed.get("verdict")
            if v in {"same", "different", "unknown"}:
                verdict = v
            confidence = _clamp_confidence(parsed.get("confidence"))
        return SameMeetVerdict(verdict=verdict, confidence=confidence)

    return checker


def _render_meet(meet: dict) -> str:
    """Render a ``{canonical_key: FieldValue}`` meet dict as readable text."""
    lines = []
    for key, fv in meet.items():
        value = getattr(fv, "value", fv)
        if value is None or (isinstance(value, str) and not value.strip()):
            continue
        lines.append(f"  {key}: {value}")
    return "\n".join(lines) if lines else "  (no meet fields)"


# ---------------------------------------------------------------------------
# Model invocation
# ---------------------------------------------------------------------------


_RETRY_NUDGE = (
    "\n\nYour previous reply was not valid JSON matching the requested "
    "shape. Reply with ONLY the JSON object, no prose, no markdown fences."
)


def _call_model(
    client: VisionClient,
    model: str,
    template: Template,
    filename: str,
    png_bytes: bytes,
) -> dict:
    """Call the model and return parsed JSON, retrying once on failure."""
    prompt = build_prompt(template, filename)
    text = _generate(client, model, prompt, png_bytes)
    parsed = try_parse_json(text)
    if _is_structural(parsed):
        return parsed

    log.warning("Vision model returned unparseable output; retrying once.")
    text = _generate(client, model, prompt + _RETRY_NUDGE, png_bytes)
    parsed = try_parse_json(text)
    if _is_structural(parsed):
        return parsed

    raise VisionExtractionError(
        "Vision model did not return parseable JSON after a retry. "
        f"Last response (truncated): {text[:300]!r}"
    )


def _generate(
    client: VisionClient,
    model: str,
    prompt: str,
    png_bytes: bytes,
) -> str:
    """One ``generate`` call. Returns the response text."""
    response = client.generate(
        model=model,
        prompt=prompt,
        images=[png_bytes],
        format="json",
        options={"temperature": 0},
    )
    # ollama-python returns a GenerateResponse with a ``.response`` attr;
    # also dict-accessible. Support both for forward/backward compat.
    text = getattr(response, "response", None)
    if text is None and isinstance(response, dict):
        text = response.get("response")
    return text or ""


def try_parse_json(text: str) -> Any:
    """Parse model text to JSON, tolerating markdown code fences.

    Returns the parsed object, or ``None`` if it isn't valid JSON.
    Shared with ``src.template_detect`` so both modules handle the
    ```json ... ``` fences some models emit the same way.
    """
    if not text:
        return None
    stripped = text.strip()
    # Strip ```json ... ``` fences some models add despite instructions.
    if stripped.startswith("```"):
        stripped = stripped.split("```", 2)
        stripped = stripped[1] if len(stripped) > 1 else ""
        if stripped.startswith("json"):
            stripped = stripped[len("json"):]
        stripped = stripped.strip().rstrip("`").strip()
    try:
        return json.loads(stripped)
    except (json.JSONDecodeError, ValueError):
        return None


def _is_structural(parsed: Any) -> bool:
    """True if ``parsed`` is at least shaped like our expected object.

    We require a dict with a "rows" list — that's the structural floor
    worth retrying for. Individual fields are allowed to be missing/null.
    """
    return isinstance(parsed, dict) and isinstance(parsed.get("rows"), list)


# ---------------------------------------------------------------------------
# Response parsing
# ---------------------------------------------------------------------------


def _parse_response(raw: dict, page_number: int) -> PageExtraction:
    """Turn one page's raw model JSON into a ``PageExtraction``."""
    result = PageExtraction(page_number=page_number)

    meet_obj = raw.get("meet") or {}
    for key in s.MEET_FIELDS:
        fv = _parse_field(meet_obj.get(key))
        if fv is not None:
            result.meet[key] = fv

    session_obj = raw.get("session") or {}
    for key in (s.COMPETITION_COORDINATOR, s.CC_LEVEL, s.DATE_SESSION):
        fv = _parse_field(session_obj.get(key))
        if fv is not None:
            result.session[key] = fv
    sn = _parse_session_number(session_obj.get(s.SESSION_NUMBER))
    if sn is not None:
        result.session[s.SESSION_NUMBER] = sn

    for row_obj in raw.get("rows") or []:
        if not isinstance(row_obj, dict):
            continue
        row: dict[str, s.FieldValue] = {}
        for key in (s.OFFICIAL_NAME, s.CLUB, s.POSITION, s.LANE_NUMBER,
                    s.TIMES_WORKED_POSITION, s.MENTOR, s.LEVEL):
            fv = _parse_field(row_obj.get(key))
            if fv is not None:
                row[key] = fv
        suc = _parse_successful(row_obj.get(s.SUCCESSFUL))
        if suc is not None:
            row[s.SUCCESSFUL] = suc
        if row:
            result.rows.append(row)

    return result


def _parse_field(obj: Any) -> Optional[s.FieldValue]:
    """Parse a ``{"value":..,"confidence":..}`` object into a FieldValue.

    Tolerates the model emitting a bare scalar instead of the object
    form. Returns None when there's no usable value.
    """
    if obj is None:
        return None
    if isinstance(obj, dict):
        value = obj.get("value")
        if value is None:
            return None
        return s.FieldValue(
            value=value,
            confidence=_clamp_confidence(obj.get("confidence")),
        )
    # Bare scalar — model ignored the object shape for this field.
    if isinstance(obj, (str, int, float, bool)):
        if isinstance(obj, str) and not obj.strip():
            return None
        return s.FieldValue(value=obj, confidence=_DEFAULT_CONFIDENCE)
    return None


def _parse_session_number(obj: Any) -> Optional[s.FieldValue]:
    """Parse the session_number field, preserving its ``source``."""
    if obj is None:
        return None
    if isinstance(obj, dict):
        value = obj.get("value")
        if value is None:
            return None
        source = obj.get("source")
        if source not in {"form", "filename", "form+filename", "unknown"}:
            source = "unknown"
        return s.FieldValue(
            value=value,
            confidence=_clamp_confidence(obj.get("confidence")),
            source=source,
        )
    if isinstance(obj, (int, str)):
        return s.FieldValue(
            value=obj, confidence=_DEFAULT_CONFIDENCE, source="unknown",
        )
    return None


def _parse_successful(obj: Any) -> Optional[s.FieldValue]:
    """Parse ``successful`` into a bool/null FieldValue with rationale.

    The model is asked for true/false/null. We also defend against
    stringy spellings ("yes"/"no") so a slightly-off model response
    doesn't silently become a wrong boolean downstream.
    """
    if obj is None:
        return None
    if isinstance(obj, dict):
        value = _coerce_bool(obj.get("value"))
        return s.FieldValue(
            value=value,
            confidence=_clamp_confidence(obj.get("confidence")),
            rationale=_as_str_or_none(obj.get("rationale")),
        )
    # Bare scalar fallback.
    return s.FieldValue(
        value=_coerce_bool(obj),
        confidence=_DEFAULT_CONFIDENCE,
    )


# ---------------------------------------------------------------------------
# Coercion helpers
# ---------------------------------------------------------------------------


def _coerce_bool(value: Any) -> Optional[bool]:
    """Map the model's successful value to True / False / None."""
    if value is None:
        return None
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    if isinstance(value, str):
        v = value.strip().lower()
        if not v or v in {"null", "none", "n/a", "unknown", "?"}:
            return None
        if v in _TRUE_WORDS:
            return True
        if v in _FALSE_WORDS:
            return False
    # Unrecognised — treat as ambiguous rather than guessing.
    return None


def _clamp_confidence(value: Any) -> float:
    """Coerce a model-reported confidence into [0.0, 1.0]."""
    try:
        c = float(value)
    except (TypeError, ValueError):
        return _DEFAULT_CONFIDENCE
    return max(0.0, min(1.0, c))


def _as_str_or_none(value: Any) -> Optional[str]:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


# ---------------------------------------------------------------------------
# Raw-response cache (.raw.json sidecar)
# ---------------------------------------------------------------------------


def _load_cache(cache_path: str | Path, model: str) -> Optional[dict]:
    """Load the per-page raw responses if the cache matches ``model``."""
    path = Path(cache_path)
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as exc:
        log.warning("Ignoring unreadable vision cache %s: %s", path, exc)
        return None
    if data.get("model") != model:
        log.info(
            "Vision cache %s was built with model %r, not %r — ignoring.",
            path, data.get("model"), model,
        )
        return None
    pages = data.get("pages")
    return pages if isinstance(pages, dict) else None


def _write_cache(
    cache_path: str | Path,
    model: str,
    source_pdf: str,
    raw_by_page: dict[str, Any],
) -> None:
    path = Path(cache_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "model": model,
        "source_pdf": source_pdf,
        "pages": raw_by_page,
    }
    with path.open("w", encoding="utf-8") as f:
        json.dump(payload, f, indent=2, ensure_ascii=False)
        f.write("\n")
