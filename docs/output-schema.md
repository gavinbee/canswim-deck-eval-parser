# Output schema

Three files land in the output directory for every parse, sharing one stem:

```
output/<stem>.json    ← canonical
output/<stem>.csv     ← derived flat view
output/<stem>.xlsx    ← derived flat view, one sheet
```

The **JSON is canonical**. CSV and XLSX are mechanical, lossy projections — they exist to be human- and tool-friendly, but for downstream code reading parser output, prefer the JSON.

A fourth file, `output/<stem>.raw.json`, may also exist. That's a per-page sidecar of raw model responses written by `src.vision_extract` for caching. It's an internal cache, not an output product, and is not part of this schema.

## JSON

Defined by the dataclasses in [`src/schema.py`](../src/schema.py) and produced via `dataclasses.asdict`. Top-level shape:

```jsonc
{
  "source_pdf":          "session_3_evals.pdf",
  "template_id":         "swim_ontario_v1",
  "template_confidence": 0.98,
  "extraction_method":   "vision",        // "vision" | "form_field" | "mixed"
  "vision_model":        "qwen2.5vl:7b",   // or null on a pure form_field run
  "edit_model":          "qwen2.5:7b",     // null unless --interactive was used
  "meet":        { /* MeetHeader */ },
  "evaluations": [ /* Evaluation, ... */ ]
}
```

### `meet`

```jsonc
{
  "competition_name": { "value": "...", "confidence": 0.97, "rationale": null, "source": null },
  "host_club":        { "value": "...", "confidence": 0.95, "rationale": null, "source": null },
  "coc":              { "value": "...", "confidence": 0.93, "rationale": null, "source": null }
}
```

Each field is either `null` or a `{value, confidence, rationale, source}` object. `rationale` is free text the model may emit to justify a judgement (used today on `successful`). `source` is provenance — populated on `session_number` to record `"form"` / `"filename"` / `"form+filename"` / `"unknown"`.

### `evaluations` (one entry per official × page)

```jsonc
{
  "source_page": 1,
  "row_index":   1,
  "meet_match":  { "value": "authoritative", "confidence": 1.0 },
                              // pages 2+ get "confirmed" | "carried" | "unknown"
  "session_number":          { "value": 3, "confidence": 0.99, "source": "form" },
  "date_session":            { "value": "Apr 11, 2026", "confidence": 0.96 },
  "competition_coordinator": { "value": "...", "confidence": 0.91 },
  "cc_level":                { "value": "...", "confidence": 0.90 },
  "official_name":           { "value": "...", "confidence": 0.94 },
  "club":                    { "value": "...", "confidence": 0.92 },
  "position":                { "value": "...", "confidence": 0.95 },
  "lane_number":             { "value": "...", "confidence": 0.88 },
  "times_worked_position":   { "value": "...", "confidence": 0.70 },
  "mentor":                  { "value": "...", "confidence": 0.85 },
  "level":                   { "value": "...", "confidence": 0.80 },
  "successful":              { "value": true,  "confidence": 0.96,
                               "rationale": "initials present and clear" },
  "row_confidence": 0.84
}
```

- Meet-level fields (`competition_name`, `host_club`, `coc`) are at the top-level `meet`, **not** repeated on each evaluation in JSON.
- `meet_match.confidence` participates in `row_confidence` and the `--low-confidence-threshold` test.
- `successful.value` is `true`, `false`, or `null`. Per the [Swim Ontario reference](templates/swim_ontario.md#sign-off-conventions-on-filled-forms): the model considers the whole row, including ditto-style marks from rows above.

## CSV / XLSX (flat view)

One row per evaluation. Meet-level fields are repeated on every row so each line is self-contained. Single sheet in the XLSX, named `evaluations`. Column order is stable — downstream tooling (rems-sync hookup, future web app) indexes on it:

| Column | Notes |
|---|---|
| `source_pdf` | source filename |
| `template_id` | e.g. `swim_ontario_v1` |
| `extraction_method` | `vision` / `form_field` / `mixed` |
| `source_page` | 1-indexed |
| `row_index` | 1-indexed within the page |
| `meet_match` | the verdict only (`authoritative` / `confirmed` / `carried` / `unknown`); the meet_match *confidence* contributes to the row composite |
| `competition_name` | from `meet` |
| `host_club` | from `meet` |
| `coc` | from `meet` |
| `competition_coordinator` | session-level |
| `cc_level` | session-level — the Competition Coordinator's level |
| `date_session` | session-level |
| `session_number` | the number itself |
| `session_number_source` | `form` / `filename` / `form+filename` / `unknown` |
| `official_name` | |
| `club` | |
| `position` | |
| `lane_number` | |
| `times_worked_position` | |
| `mentor` | |
| `level` | the **mentor's** officiating level — see the [Swim Ontario reference](templates/swim_ontario.md#fields) |
| `successful` | `True` / `False` / blank (= null) |
| `successful_rationale` | model's reasoning for the sign-off judgement |
| `confidence` | the row composite (`row_confidence` in JSON) |

CSV cells never contain JSON — values flatten to their `.value` (`meet_match` collapses to its verdict string, `successful` collapses to its boolean, `successful_rationale` becomes its own column). The composite confidence is the single signal you'd sort or filter on for human review.

## How files are produced

```python
from src import output, schema as s

result = s.ParseResult(...)
output.write_all(result, "output/")
# → output/<stem>.json, output/<stem>.csv, output/<stem>.xlsx
```

`stem` defaults to the source PDF's stem. Pass `stem="custom"` to override.

`output.write_json(result, path)` and `output.to_csv_rows(result)` are exposed separately for callers that want only one format or want to test the flattening logic without touching disk.
