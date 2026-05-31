# 0002 — Synthetic typed-scan vision fixtures + integration harness

Status: proposed

## Context

[0001 §Test data acquisition](0001-initial-design.md#test-data-acquisition) defines three tiers of test data:

- **Tier 1** — committed *fillable* PDFs. These exercise the **form-field fast path** only; the vision model is never invoked.
- **Tier 2** — real scans + REMS golden sets, kept **local** (PII), run under `pytest -m integration`. This is the only tier that scores the **vision path**, and it depends on the rems-sync hookup ([#19]) plus the author manually collecting scans.
- **Tier 3** — pseudonymized, *realistic* scans (committed, deferred). It carries a deliberate warning: clean overlays "would erase exactly the artifacts our parser must learn to handle and make the fixtures **trivially easier than real scans**."

The gap this doc closes: **there is no committed, deterministic fixture that exercises the vision path.** Today the only thing driving vision extraction is an ad-hoc `data/sample_scan.pdf` with no committed ground truth, so we cannot score the model, catch regressions, or stabilize quality on straightforward cases. gh [#16] (the golden integration test harness) is written against Tier 2, which can't land until rems-sync is wired and the author has hand-collected scans — and even then it can never be committed.

We want a committed, deterministic, vision-path integration fixture **now**, for the straightforward (typed) case, without waiting on Tier 2 or Tier 3.

## Why this isn't the "trivially easier fake" Tier 3 warns against

The Tier 3 caution is specifically about **faking handwriting** — drawing clean fonts where a real form would have messy ink, thereby erasing the artifacts the parser must learn to handle. A clean, typed, flattened PDF is a different thing: it is a **real production input shape**, not a surrogate for a handwritten one.

0001's own Context lists it explicitly:

> "sometimes (e.g. Swim Ontario's online form option) a digital form is filled in and **exported to PDF**"

A digital-export PDF that has been flattened (or printed-then-scanned-to-image) is typed text with no widget layer and no text layer — exactly the shape that routes to the vision path (`has_form_fields() == False`, no embedded text). It is genuinely *easier* than a handwritten scan, and that is the point: this tier's job is **baseline-quality stabilization and regression-catching for straightforward cases**, not handwriting robustness. Handwriting robustness stays the job of Tier 2 (real scans) and Tier 3 (pseudonymized realistic scans), both unchanged by this doc.

So this is an **addition** to the tier list, not a replacement:

| Tier | Path exercised | Committed? | Difficulty | Status |
|---|---|---|---|---|
| 1 | form-field | yes | n/a | implemented |
| **1b (this doc)** | **vision** | **yes** | **typed / clean** | **proposed** |
| 2 | vision | no (local) | real handwriting | gh #16, blocked on #19 |
| 3 | vision | yes (deferred) | realistic handwriting | gh #29 |

## Design

### Generator — `tests/fixtures/vision_scan/make_synthetic_scan.py`

Mirrors the existing `tests/fixtures/form_field/make_synthetic_fixture.py` (same Faker seed → reproducible), but produces a **flattened, image-only** PDF and an **exact golden** instead of a fillable PDF.

1. **Fill the form** from the same constrained vocabularies and Faker-seeded names the form-field generator uses, so the two fixtures stay recognizably the same meet shape.
2. **Fill the currently-blank fields too.** The form-field fixture leaves `times_worked_position`, `mentor`, `level`, and `successful` blank. This tier fills them with a deterministic pattern so the harness actually tests the interesting columns (the ones where the current model misreads — see Open items) and the holistic `successful` judgement.
3. **Flatten + rasterize.** Use PyMuPDF `doc.bake()` to flatten the AcroForm widgets into static page content, then rasterize each page to a PNG and rebuild a PDF whose pages are *only* that image. The result has no widgets and no text layer, guaranteeing it routes to the vision path (matches the observed `data/sample_scan.pdf`: `widgets=0, text_chars=0, images=1`).
4. **Emit the golden** (`golden.json`) from the same known values — no model in the loop, so the golden is exact by construction.

### Modelling `successful` honestly for a typed form

A typed digital-export form has no checkbox for "successful" — it has an initials cell. So the two *natural* typed states are:

- **initials present → `true`** (signed off)
- **blank cell → `null`** (not signed off / ambiguous)

To also exercise the holistic **`false`** judgement (and the "blank initials but a mentor was assigned" ambiguity the schema cares about), the generator deterministically draws a small number of **marks** onto the rasterized image: e.g. one row struck through end-to-end (clear `false`), and one row with a mentor filled but initials blank (genuine `null`). These are drawn programmatically with known coordinates, so the golden records the intended verdict exactly. We keep these marks minimal and clean — rich, realistic ink for these cases remains Tier 3's job; here they exist only so every `successful` branch (`true`/`false`/`null`) appears at least once.

### Golden format and the scoring contract

The golden reuses the comparison contract 0001 already specifies in Verification step 13:

> parser output matches the golden set on **`(official, position, successful)`** tuples … **modulo position-name normalization**.

`golden.json` therefore records, per row: `official_name`, `position`, `successful`, plus the meet/session header for a coarser header check. The full Faker-known values are also recorded so a future, stricter comparison can opt in without regenerating.

### Harness — `tests/test_integration_vision.py`

- Marked `@pytest.mark.integration`; **skipped unless `-m integration`** is passed (CI stays vision-free, per 0001 Verification 14). Also skips with a clear message if Ollama / the model isn't reachable, so `-m integration` on a machine without a GPU degrades to a skip rather than a failure.
- Runs the real vision path over the committed synthetic scan, then scores against `golden.json`:
  - **Row matching** by `official_name` (normalized: case/space-folded).
  - **Per-tuple assertions** on `(official, position, successful)` with **position-name normalization** (a small synonym/canonicalization map, since the model may return "Inspector of Turns" vs "Turn Inspector" etc.).
  - **Accuracy threshold, not exact match.** Vision output is non-deterministic even at temperature 0 across model/runtime versions, so the harness asserts a field-level accuracy floor (e.g. ≥ 0.9 of scored tuples correct) and reports the mismatches, rather than requiring a perfect transcription. The threshold is a named constant, tunable as we learn the model's real hit rate on this fixture.

### `pytest.ini` / markers

Register the `integration` marker (if not already) so `-m integration` is first-class and unmarked runs skip it cleanly.

## Verification

1. `python tests/fixtures/vision_scan/make_synthetic_scan.py` regenerates an identical PDF + golden for a fixed seed.
2. The generated PDF reports `has_form_fields() == False` and zero embedded text — i.e. it routes to the vision path.
3. `pytest tests/ -q` (no marker) still passes with **no** Ollama and **skips** the new integration test.
4. `pytest -m integration` on a machine with Ollama + the model runs the vision path against the committed scan and passes the accuracy threshold; with Ollama absent it **skips** with a clear message.
5. The golden's `(official, position, successful)` tuples match the Faker-known values by construction (a cheap unit test can assert the golden against the generator's in-memory data without any model).

## Open items / out of scope

- **Handwriting realism stays Tier 3** (gh #29). This tier is clean/typed by design.
- **Quality bugs surfaced while scoping**, to be filed once the harness can measure them: (a) `times_worked_position` taking the `lane_number` value (column bleed); (b) hallucinated `successful` rationales ("Row is crossed out" on blank cells). Both are likely prompt-level fixes and the harness is what lets us prove a fix helps.
- **Threshold tuning.** The initial accuracy floor is a guess; we adjust it against observed hit rates once the fixture exists.
- **gh #16 stays open** for the Tier-2 real-scan path; this doc re-scopes the *committable* portion of its harness onto the synthetic golden.

[#16]: https://github.com/swimblocks/deck-eval-parser/issues/16
[#19]: https://github.com/swimblocks/deck-eval-parser/issues/19
[#29]: https://github.com/swimblocks/deck-eval-parser/issues/29
