"""Vision-path integration test against the committed synthetic golden.

This is the committable half of gh #16's golden harness (see
``docs/design/0002-synthetic-vision-fixtures.md``). It runs the **real**
vision model over ``tests/fixtures/vision_scan/session_1_evals_scan.pdf``
— a flattened, image-only PDF generated deterministically from a Faker
seed — and scores the output against the exact ``golden.json`` emitted
alongside it.

Gating:
    * ``@pytest.mark.integration`` → skipped unless ``pytest -m integration``
      (conftest enforces this). CI never runs it.
    * Skips with a clear message if Ollama isn't reachable or the model
      isn't pulled, so ``-m integration`` on a GPU-less box degrades to a
      skip rather than a hard failure.

Scoring contract (from 0001 Verification step 13): match rows by official
name, then assert ``(official, position, successful)`` tuples agree —
**modulo position-name normalization** — at a 100% accuracy floor. This
fixture is the easiest possible input (typed, clean, no handwriting), and
humans clear even the hardest handwritten forms at ~99%, so anything short
of a perfect read here is a quality bug. These tests are therefore
**expected to fail until the extraction-quality issues they surface are
fixed** — that is their job. Mismatches are reported in the assertion
message so each failure points at the offending rows.

Daemon lifecycle is self-managed: the ``parsed`` fixture drives the same
:class:`~src.ollama_runtime.OllamaDaemon` the CLI uses, so a single
``pytest -m integration`` is the whole story — it starts the daemon if
one isn't already up and stops it on the way out (leaving a
pre-existing daemon alone). No manual ``ollama serve`` two-step.

CLI options (see ``tests/conftest.py``):
    * ``--vision-model <tag>`` overrides the model (default
      ``vision_extract.DEFAULT_VISION_MODEL``).
    * ``--pull-models`` allows the test to ``ollama pull`` a missing
      model (a multi-GB download). Off by default: a missing model skips
      with a copy-paste pull hint instead of silently downloading.
"""
from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from src import merge, ollama_runtime as rt, vision_extract
from src.templates import TEMPLATES

pytestmark = pytest.mark.integration


FIXTURE_DIR = Path(__file__).parent / "fixtures" / "vision_scan"
SCAN_PDF = FIXTURE_DIR / "session_1_evals_scan.pdf"
GOLDEN_PATH = FIXTURE_DIR / "session_1_evals_scan.golden.json"

TEMPLATE_ID = "swim_ontario_v1"

# Accuracy floor for the (official, position, successful) tuple contract.
#
# This is deliberately 100%, not a "good enough" threshold. The fixture is
# the EASIEST possible input: a typed, clean, digital-export scan with no
# handwriting, no skew, no smudges. Humans transcribe even the *hardest*
# handwritten versions of these forms at ~99% accuracy, so our tolerance
# for error on the easy case is essentially zero — anything less than a
# perfect read here is a quality bug to fix, not slack to absorb.
#
# These tests are EXPECTED to fail until the underlying extraction quality
# issues they surface are fixed (see docs/design/0002-synthetic-vision-
# fixtures.md). That is the point: they are a quality gate, and the work to
# make them pass is tracked on the integration-harness issue. Do not lower
# this floor to make the suite green — fix the model/prompt/pipeline until
# it clears the bar honestly.
ACCURACY_FLOOR = 1.0


# ---------------------------------------------------------------------------
# Normalization for the scoring contract
# ---------------------------------------------------------------------------


def _norm(text: object) -> str:
    """Case/whitespace-folded comparison key."""
    return re.sub(r"\s+", " ", str(text or "").strip()).casefold()


# Minimal position canonicalization — the model may phrase a few positions
# differently than the form's exact label. Keep this small and explicit.
_POSITION_CANON = {
    "inspector of turns": "inspector of turns",
    "turn inspector": "inspector of turns",
    "turns inspector": "inspector of turns",
    "inspector of turn": "inspector of turns",
    "stroke judge": "stroke judge",
    "stroke and turn judge": "stroke judge",
    "chief timer": "chief timer",
    "head timer": "chief timer",
    "admin desk": "admin desk",
    "administration desk": "admin desk",
    "administrative desk": "admin desk",
}


def _norm_position(text: object) -> str:
    key = _norm(text)
    return _POSITION_CANON.get(key, key)


# ---------------------------------------------------------------------------
# Model-tag matching → tolerate ":latest" elision and bare-name forms
# ---------------------------------------------------------------------------


def _model_present(model: str, pulled: list[str]) -> bool:
    """True if ``model`` matches any locally-pulled tag.

    Ollama elides ``:latest`` and accepts a bare name, so ``qwen2.5vl``,
    ``qwen2.5vl:latest`` and ``qwen2.5vl:7b`` are all distinct strings we
    must reconcile. We match if the requested tag, its bare name, or its
    ``:latest`` form appears in the pulled set.
    """
    base = model.split(":")[0]
    wanted = {model, base, f"{base}:latest"}
    return bool(set(pulled) & wanted)


# ---------------------------------------------------------------------------
# Run the vision pipeline once per module (the model call is slow)
#
# The fixture owns the daemon for the whole module: it enters OllamaDaemon
# (starting `ollama serve` only if one isn't already up), checks the model
# is pulled INSIDE the context, runs extraction+merge, then yields — so
# daemon teardown happens after every test in the module has run.
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def parsed(request):
    """Real vision extraction + merge over the synthetic scan."""
    import ollama

    assert SCAN_PDF.is_file(), f"missing fixture {SCAN_PDF}"

    model = request.config.getoption("--vision-model") or vision_extract.DEFAULT_VISION_MODEL
    allow_pull = request.config.getoption("--pull-models")

    # Binary on PATH? Skip cleanly on a box without Ollama installed.
    try:
        rt.find_binary()
    except rt.OllamaBinaryMissingError as exc:
        pytest.skip(f"Ollama not installed; {exc}")

    # Enter the daemon WITHOUT required_models so __enter__ never pulls or
    # raises on a missing model — we want to control that ourselves below
    # so a missing model skips (with a hint) instead of failing the suite.
    try:
        daemon = rt.OllamaDaemon(host=rt.DEFAULT_HOST).__enter__()
    except rt.OllamaRuntimeError as exc:
        pytest.skip(f"Ollama daemon unavailable: {exc}")

    try:
        pulled = rt.list_pulled_models(host=rt.DEFAULT_HOST)
        if not _model_present(model, pulled):
            if allow_pull:
                rt.ensure_models([model], host=rt.DEFAULT_HOST, auto_pull=True)
            else:
                pytest.skip(
                    f"Vision model {model!r} not pulled "
                    f"(have {sorted(pulled)}); run `ollama pull {model}` "
                    f"or rerun with `--pull-models` to download it."
                )

        template = TEMPLATES[TEMPLATE_ID]
        client = daemon.client()

        pages = vision_extract.extract_pdf(
            str(SCAN_PDF),
            template,
            client=client,
            model=model,
            use_cache=False,
        )
        result = merge.merge(
            pages,
            source_pdf=SCAN_PDF.name,
            template_id=TEMPLATE_ID,
            template_confidence=1.0,
            extraction_method="vision",
            vision_model=model,
            same_meet_checker=vision_extract.make_same_meet_checker(client, model),
        )
        yield result
    finally:
        # Always tear the daemon down (stops it only if we started it).
        daemon.__exit__(None, None, None)


@pytest.fixture(scope="module")
def golden():
    return json.loads(GOLDEN_PATH.read_text(encoding="utf-8"))


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def _index_by_name(evaluations):
    out = {}
    for ev in evaluations:
        name = ev.official_name.value if ev.official_name else None
        if name:
            out[_norm(name)] = ev
    return out


def test_finds_every_official(parsed, golden):
    """Each golden official should appear in the extraction (matched by name)."""
    by_name = _index_by_name(parsed.evaluations)
    missing = [
        row["official_name"]
        for row in golden["rows"]
        if _norm(row["official_name"]) not in by_name
    ]
    # Allow a small miss on a clean scan, but most must be found.
    found = len(golden["rows"]) - len(missing)
    ratio = found / len(golden["rows"])
    assert ratio >= ACCURACY_FLOOR, (
        f"only matched {found}/{len(golden['rows'])} officials by name; "
        f"missing: {missing}"
    )


def test_tuple_contract_accuracy(parsed, golden):
    """(official, position, successful) tuples agree above the floor.

    Position is normalized; successful is compared exactly (True/False/None).
    Reports every mismatch so a failure is actionable.
    """
    by_name = _index_by_name(parsed.evaluations)

    total = 0
    correct = 0
    mismatches: list[str] = []

    for row in golden["rows"]:
        ev = by_name.get(_norm(row["official_name"]))
        if ev is None:
            # Unmatched official: counts against both position and successful.
            total += 2
            mismatches.append(f"{row['official_name']!r}: not found in output")
            continue

        # Position
        total += 1
        got_pos = ev.position.value if ev.position else None
        if _norm_position(got_pos) == _norm_position(row["position"]):
            correct += 1
        else:
            mismatches.append(
                f"{row['official_name']!r} position: got {got_pos!r}, "
                f"want {row['position']!r}"
            )

        # Successful (True / False / None)
        total += 1
        got_succ = ev.successful.value if ev.successful else None
        if got_succ == row["successful"]:
            correct += 1
        else:
            mismatches.append(
                f"{row['official_name']!r} successful: got {got_succ!r}, "
                f"want {row['successful']!r}"
            )

    accuracy = correct / total if total else 0.0
    assert accuracy >= ACCURACY_FLOOR, (
        f"tuple accuracy {accuracy:.2f} below floor {ACCURACY_FLOOR:.2f} "
        f"({correct}/{total} correct).\nMismatches:\n  "
        + "\n  ".join(mismatches)
    )


def test_meet_header(parsed, golden):
    """Meet-level header should read back (case/space-insensitive)."""
    g = golden["meet"]
    got_name = parsed.meet.competition_name.value if parsed.meet.competition_name else None
    got_host = parsed.meet.host_club.value if parsed.meet.host_club else None
    # Host club is a short code that should match exactly; competition name
    # is free text the model should at least contain the golden's words.
    assert _norm(got_host) == _norm(g["host_club"]), (
        f"host_club: got {got_host!r}, want {g['host_club']!r}"
    )
    assert _norm(g["competition_name"]) in _norm(got_name) or _norm(got_name) in _norm(g["competition_name"]), (
        f"competition_name: got {got_name!r}, want {g['competition_name']!r}"
    )
