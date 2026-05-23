# Swim Ontario template (`swim_ontario_v1`)

Reference for the **Swim Ontario On-Deck Evaluation** template, as implemented in `src/templates/swim_ontario_v1.py`.

## Source

The field labels and widget names in this template are sourced **verbatim** from [`eval-gen`](https://github.com/gavinbee/eval-gen)'s `_build_fields()`, which was reverse-engineered from the official Swim Ontario blank form (`eval-gen/eval_form.pdf`). Keeping this template in lockstep with `eval-gen` means any `eval-gen` output is parsable through the form-field fast path with no extra work.

## Page properties

| Property | Value |
|---|---|
| Page size | US Letter, **landscape** (792 × 612 PDF points) |
| Rows per page | 9 officials |
| Language | English (`en`) |

> **The form is landscape, not portrait.** Easy to miss because most PDF previewers display the first page however the viewer was last sized. Any future cropping or per-row snippet logic must account for the wider-than-tall page rect.

## Fields

The template populates these canonical schema keys (see `src/schema.py`):

**Meet-level** (stable across pages):
- `competition_name` → form label `"Competition Name"`
- `host_club` → `"Host Club"`
- `coc` → `"COC"`

**Session-level** (may vary per page):
- `competition_coordinator` → `"Competition Coordinator"`
- `cc_level` → `"Level"` *(the meet-level "Level" widget — the CC's officiating level, not a per-row level)*
- `date_session` → `"Date  Session"` *(two spaces in the form's label)*
- `page_number` → `"Page"`
- `page_of` → `"of"`

**Per-row** (up to 9 rows × these 8 fields = 72 row widgets per page):
- `official_name` → `"Name of Official"`
- `club` → `"Club"`
- `position` → `"Position"`
- `lane_number` → `"Lane number"`
- `times_worked_position` → `"How many times have you worked this position"`
- `mentor` → `"Mentor Official  Session referee"` *(two spaces)*
- `level` → `"Level"` *(the per-row "Level" widget — **the mentor's officiating level**, recorded for the eval being signed off, not the level of the official being evaluated)*
- `successful` → `"Successful initial"`

## Per-row widget naming

Per-row widgets are named `<base><i>` for `i ∈ 1..9`, e.g.:

| `i` | Widget name |
|---|---|
| 1 | `Name of OfficialRow1` |
| 2 | `Name of OfficialRow2` |
| … | … |
| 9 | `Name of OfficialRow9` |

In the template's `widget_field_map`, these are stored once with a `{i}` placeholder (`"Name of OfficialRow{i}": "official_name"`) which `src.form_extract` substitutes at lookup time. The `Row` part is part of the bare widget name — it is **not** a separator added by us.

## The duplicate `"Level"` widget label

`"Level"` appears twice in the form:

- As the **meet-level** Level widget → mapped to `cc_level` (Competition Coordinator's level).
- As the **per-row** `LevelRow{i}` widgets → mapped to `level` (the **mentor's** officiating level — see the field list above).

PyMuPDF disambiguates by widget name (`Level` vs `LevelRow1`), not by label. The template's `widget_field_map` reflects that.

## Total widget count

A fully-populated page has exactly **80 widgets**: 8 meet/session-level + (9 rows × 8 per-row fields). Useful as a sanity check when validating extracted output.

## Sign-off conventions on filled forms

Per the design doc (§Schema) and the `vision_prompt_addendum` baked into this template module: evaluators have **no standard convention** for marking a deck eval as *not* successful. Forms in the wild include:

- Clear sign-off — initials in the "Successful initial" cell → `successful=true`
- Crossed-out row → `successful=false`
- Initials cell left blank but a mentor's name written in the "Mentor" cell → `successful=false` (the evaluator did the eval and the official was not signed off)
- Marginal note ("redo next meet", etc.) → judgement call, often `null`
- **Ditto marks, double-tick marks, or a down-arrow** in the Mentor / Level / Successful-initial columns, indicating "same as the row above" → these are common when one evaluator signs off many rows in a row. The model should treat them as **low-confidence sign-offs**: emit `successful=true` (carrying down the value from the row above) but lower the confidence so they surface for human review.

The vision model is instructed to consider the entire row holistically when emitting `successful`, including looking above the current row for ditto-style continuations.

## Source files

- `src/templates/swim_ontario_v1.py` — template definition
- `tests/fixtures/form_field/session_1_evals.pdf` — example eval-gen output (2 pages, 11 officials, Cunningham Classic 2026, no PII)
