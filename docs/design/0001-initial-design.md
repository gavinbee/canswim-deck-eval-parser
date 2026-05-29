# Plan: `canswim-deck-eval-parser`

## Context

Each Canadian Provincial Section produces its own **On-Deck Evaluation** form. The way data ends up in a PDF varies: sometimes pre-printed sheets are filled in by hand during a session and then scanned; sometimes (e.g. Swim Ontario's online form option) a digital form is filled in and exported to PDF; and other provinces may use yet other workflows we haven't surveyed. Templates differ between provinces: layout, field labels, language (Quebec forms include or are entirely in French), and date formats all vary.

Today there is no automated way to get evaluation data back out of these PDFs. The downstream consumer is `rems-sync`, which records deck-evaluation credentials against officials in REMS — currently fed by a manually maintained Google Sheet. We want to close that gap with a parser that ingests one PDF, detects which provincial template it is, and emits a structured spreadsheet. v1 does **not** need to conform to rems-sync's exact input shape; it just needs a clean schema that captures every field on the form so a future bridge is straightforward.

**v1 scope: Swim Ontario template only** — it's first simply because the author is in Ontario and has the most familiarity and test data here, not because it's inherently primary. The architecture is a template registry, so adding Quebec / Alberta / BC etc. is "add a template module" rather than a refactor.

## Design

### Stack — match `canada-swim-tech-survey`

| Item | Choice |
|---|---|
| Language | Python 3.12 |
| Package mgr | pip + `requirements.txt` (no pyproject, no uv/poetry) |
| Source layout | `src/` package, `tests/` mirrors, `main.py` at root |
| Test fwk | pytest >=8 with `tests/conftest.py` |
| Linter/formatter | **none** (matches canada-swim-tech-survey) |
| License | MIT, 2026 Gavin Bee |
| Logging | `logging.basicConfig` in `main.py`, `log = logging.getLogger(__name__)` per module |
| Branch naming | `{issue-number}-{kebab-slug}` |
| Commits | Imperative, no conventional-commit prefix |
| Issue/PR templates | none (match canada-swim-tech-survey) |
| CODEOWNERS | none (match canada-swim-tech-survey) |
| Workflow | one issue → one branch named for that issue → PR referencing the issue → squash-merge to `main`. Documented in `CONTRIBUTING.md`. |

### Models — concrete pinning

| Role | Model (Ollama tag) | Size | License | Why |
|---|---|---|---|---|
| Vision extraction (per page) | **`qwen2.5vl:7b`** | ~6 GB | Apache 2.0 | Top-tier on OCRBench v2 / DocVQA among <15 B open VLMs; built for structured doc extraction; handles handwriting reasonably. |
| Template detection (first page only) | **`qwen2.5vl:7b`** (reused) | — | — | Already loaded; one extra call asking it to classify the page against a small enum of registered templates. No second model needed. |
| Text edit interpretation (interactive mode) | **`qwen2.5:7b`** | ~4.7 GB | Apache 2.0 | Qwen2.5-7B-Instruct beats Llama 3.1 8B on structured JSON / instruction following per Qwen's published benchmarks. Same model family as the vision model → user installs from one ecosystem. |
| Pseudonymization image generation | _deferred_ | — | — | Out of scope for v1; documented separately. |

### Model selection by GPU tier

Picking a default that works on a modest GPU is the safe move, but a better GPU genuinely buys accuracy on the hardest input (messy handwriting, dense pages). The parser **detects available VRAM at startup** (via `nvidia-smi` on Windows/Linux, `system_profiler SPDisplaysDataType` on macOS, falling back to "unknown") and either auto-picks an appropriate tier or just logs a one-line suggestion to upgrade.

| Tier | VRAM | Vision model | Edit model | Notes |
|---|---|---|---|---|
| **CPU-only / <6 GB** | — | `qwen2.5vl:3b` (Q4) | `qwen2.5:3b` | Works but slow (~30-60 s/page) and lower accuracy on handwriting. Documented as "smallest", not default. |
| **8–16 GB** _(default)_ | 8–16 GB | **`qwen2.5vl:7b`** (Q4) | **`qwen2.5:7b`** (Q4) | The safe default. ~5-7 GB VRAM, ~5-10 s/page on a 12 GB consumer GPU. |
| **20–24 GB** | 20–24 GB | **`qwen2.5vl:32b`** (Q4_K_M, ~20 GB) | `qwen2.5:7b` (text edits don't need the bigger model) | Meaningful accuracy uplift on hard handwriting per OCRBench v2; ~3-5× slower per page than 7B. Worth it if you're processing scans of variable quality and care about reducing manual review. |
| **≥48 GB** | dual-GPU / 48 GB+ | `qwen2.5vl:72b` (Q4) | — | Diminishing returns for this task; not recommended for v1. Document as a power-user option. |

**Auto-pick behavior:**
- Default `--vision-model` is unspecified → parser detects VRAM and picks the highest tier that fits with headroom.
- `--vision-model <tag>` always wins.
- The `OllamaDaemon` ensure-models step pulls only the chosen tag, not all of them.

**Why not always the biggest model?** Cost is per-page latency, not VRAM (within tier). For a single PDF on a 24 GB card, 32B is fine. For multi-page batch (future), the latency adds up — and the user with a 24 GB card may still want to opt down to 7B for throughput. So: tier auto-pick is the default, explicit flag is the override.

**Qwen3-VL** is the next generation of this family and is starting to land on Ollama. We pin to Qwen2.5-VL for v1 because it's mature today. Tracked as a deferred follow-up; the swap should be a one-line change in `src/ollama_runtime.py` (plus a model re-pull and a regression run against the golden fixtures).

### Template-registry architecture

The parser starts by **detecting which provincial template** the PDF is. v1 ships one entry — Swim Ontario — but the registry is the extension point.

- `src/templates/__init__.py` — `TEMPLATES: dict[str, Template]` keyed by stable IDs (e.g. `"swim_ontario_v1"`).
- `src/templates/base.py` — `Template` dataclass with:
  - `id: str`
  - `display_name: str`
  - `field_names: dict[str, str]` (canonical schema key → label/widget name as it appears on this template)
  - `language: Literal["en", "fr", "en_fr"]`
  - `date_formats: list[str]` (acceptable input formats; the model is told to emit ISO `YYYY-MM-DD` regardless)
  - `vision_prompt_addendum: str` (template-specific hints to inject into the vision prompt — e.g. "Quebec form: 'Officiel' is the official's name; dates are DD/MM/YYYY")
  - `widget_field_map: dict[str, str]` (for form-field fast path — maps PyMuPDF widget names → canonical keys)
- `src/templates/swim_ontario_v1.py` — populated from eval-gen's `_build_fields()`.

**Detection flow** (`src/template_detect.py`):
1. Rasterize page 1 of the PDF.
2. Send to `qwen2.5vl:7b` with a prompt: "Which of these provincial On-Deck Evaluation templates is this? Respond with the template ID and a confidence." The enum of IDs comes from the registry.
3. If confidence < threshold (default 0.7) or "unknown", error out with a clear message asking the user to file an issue with the PDF (and offering `--template <id>` override).
4. The detected template is then used everywhere downstream: prompts include template-specific addenda, the widget map applies to the form-field path, dates are interpreted in the template's expected formats.

**v1 templates implemented:** `swim_ontario_v1`.
**Stubs reserved (raise NotImplementedError if detected):** `swim_quebec_v1`, `swim_alberta_v1`, `swim_bc_v1` — placeholder files so the registry shape is exercised and adding them later is a contained change.

### Extraction strategy — local vision LLM + form-field fast path

Real input = scanned PDFs of forms with **handwriting on printed structure**. Constraint: must run **locally**.

- **Primary path (scans):** rasterize each PDF page with PyMuPDF, send the page image to `qwen2.5vl:7b` via the **Ollama** HTTP API along with the detected template's prompt addendum, and ask for a JSON object keyed by canonical field names.
- **Fast path (form-field PDFs, e.g. eval-gen output):** if PyMuPDF detects fillable widgets on the page, read widget values directly via the template's `widget_field_map`. Deterministic, zero-cost, perfect for unit tests and for re-parsing decks that were typed rather than scanned.

The two paths converge on the same intermediate dict per page, then go through the same validation, merging, and output code.

### Ollama lifecycle — auto-managed by the parser

Ollama itself (the binary) is a runtime dependency the user must install once. After that, **the parser manages the daemon and models for the duration of a run**:

1. **Binary present?** On startup, `which ollama` (or `where.exe ollama` on Windows). If missing, exit code 1 with a clear, copy-paste-able message:
   ```
   Ollama is not installed. Install it with one of:
       Windows:  winget install Ollama.Ollama
       macOS:    brew install ollama
       Linux:    curl -fsSL https://ollama.com/install.sh | sh
   Or run the bundled installer:  scripts/install.ps1   (Windows)
                                   scripts/install.sh    (macOS/Linux)
   Full guide: https://github.com/swimblocks/deck-eval-parser#installation
   ```
2. **Daemon running?** Probe `http://localhost:11434/api/tags`. If unreachable, spawn `ollama serve` as a subprocess via a context manager that wires up `atexit` + signal handlers to stop it on exit (clean shutdown via `Popen.terminate()` then `wait(timeout=5)` then `kill()`). If we started the daemon, we stop it. If the user already had it running, we leave it running. A small `OllamaDaemon` class in `src/ollama_runtime.py` encapsulates this.
3. **Models pulled?** Query `/api/tags`. For each required tag (`qwen2.5vl:7b`, and `qwen2.5:7b` only if `--interactive`/`--review-all`), if missing, `ollama pull <tag>` with progress streamed to stderr. Behind `--no-auto-pull` (default off) the parser exits with instructions instead.

This makes a fresh-install first run a one-line experience after the bundled installer script: `python main.py path/to/eval.pdf` and the tool boots the daemon, pulls the model on first run, parses, shuts down.

### Installation scripts

In `scripts/`:

- **`install.ps1`** (Windows): checks for Python 3.12; verifies `winget`; installs Ollama via `winget install Ollama.Ollama` if missing; creates `.venv`; `pip install -r requirements.txt`; `ollama pull qwen2.5vl:7b qwen2.5:7b`.
- **`install.sh`** (macOS/Linux): same shape using `brew` on macOS, official `curl | sh` installer on Linux.
- Both scripts are idempotent (skip steps already done) and print clear progress.

### Documentation strategy

Two principles:

1. **The README is a navigation surface, not a manual.** It exists to give a new visitor a 30-second understanding of what the tool does and a clear path to whatever specific detail they need. Detailed content lives in `docs/`.
2. **Repo docs reflect the as-implemented current architecture.** Design docs (this plan and its successors) live alongside as historical artifacts — they record *what we decided and why*, not *what the code is right now*.

**README sections (kept short):**
1. One-paragraph "what it does"
2. Quick start (install script + single example command)
3. Status table: which provincial templates are supported
4. Links into `docs/` for: installation details, usage, design docs, contributing, supported templates, troubleshooting
5. License

**Files at repo root** (GitHub-convention locations): `README.md`, `LICENSE`, `CONTRIBUTING.md`.

**`docs/` layout:**
```
docs/
├── installation.md           # install scripts (install.ps1/install.sh), manual install, Ollama prerequisites, what gets pulled, GPU/VRAM notes, troubleshooting install issues
├── usage.md                  # every CLI flag with examples; non-interactive default; --interactive walkthrough; --review-all; --template override; --no-auto-pull
├── output-schema.md          # canonical JSON shape (per-field confidence, source provenance); how CSV/XLSX are derived; how the raw.json sidecar is used
├── models.md                 # vision and edit-model choices; GPU-tier auto-pick table; how to override; performance/accuracy tradeoffs; Qwen3-VL roadmap note
├── architecture.md           # CURRENT design: template detection → Ollama lifecycle → extract (vision OR form-field) → merge → optional review → output. Updated whenever the architecture changes. Links into design docs for the *why*.
├── templates/
│   ├── README.md             # how to add a new provincial template (Template dataclass, widget_field_map, vision_prompt_addendum, registering in TEMPLATES)
│   └── swim_ontario.md       # field-by-field reference for the Ontario template (links to eval-gen)
├── troubleshooting.md        # Ollama daemon issues, model pull failures, low-confidence outputs, template detection failures, GPU not detected, low-VRAM degradation
└── design/
    ├── README.md             # index of design docs with one-line summaries and status (implemented / superseded / abandoned)
    ├── 0001-initial-design.md   # ← this plan, copied in as the first design doc
    └── ...
```

**Design-doc workflow** (codified in `CONTRIBUTING.md`):

- Any **non-trivial feature** gets a design doc in `docs/design/NNNN-short-name.md` *before* implementation, modeled on this plan: Context → Design → Verification → Open items.
- Numbering is sequential, never reused. PR that adds the feature references the doc.
- After landing, **`docs/architecture.md` is updated** to reflect the new state. Design docs themselves are append-only (status header at top: `Status: implemented | superseded by NNNN | abandoned`).
- Trivial = typo, single-line fix, dependency bump, doc edit. Non-trivial = anything that adds a module, changes the output schema, changes the model, adds a CLI flag of consequence, or touches the pipeline shape.

**GitHub Pages (conditional)**: if `docs/` outgrows easy navigation (say >10 files or deep nesting), publish it via GitHub Pages using the simple Jekyll theme. Not necessary at v1 launch; tracked as a deferred follow-up.

### Schema

The canonical schema is **template-agnostic**: it is the union of fields any template can populate, with `null` for fields a given template doesn't have. For v1 these come verbatim from `eval-gen`'s `_build_fields()` (Swim Ontario):

- Meet-level (expected stable across pages of one PDF): `competition_name`, `host_club`, `coc`
- Session-level (may vary per page): `competition_coordinator`, `cc_level`, `date_session`, `page`, `of`
- Per-official row (up to 9 per page): `official_name`, `club`, `position`, `lane_number`, `times_worked_position`, `mentor`, `level`, `successful`
- Top-level metadata: `template_id`, `template_confidence`

**`successful` is a boolean** (nullable for genuinely ambiguous cases), not a transcription of the initials. The vision model judges this **at the row level, not just the initials cell**. Evaluators have no standard convention for "not successful": some cross out the whole row, some leave initials blank but fill in a mentor name, some scribble a note. A human reading the row almost always knows what the evaluator meant; we tell the model to do the same. The prompt instructs it to consider the entire row holistically — the initials cell, any cross-outs over the row, the mentor column, the level column, marginal notes — and emit `true` (clearly signed off), `false` (clearly not), or `null` (truly ambiguous; surface for human review).

**Session inference is delegated to an LLM, not regex.** We don't know what filename conventions people will actually use. The vision model already sees the page; we additionally pass the source filename into the prompt and instruct it to populate `session_number` using whichever signal is clearest (form's date/session field, filename, or both). The model also returns `session_number_source` (one of `form`, `filename`, `form+filename`, `unknown`) for auditing. No regex, no heuristics in our code — the model does it. This costs nothing extra since the vision call is already happening per page.

### Multi-page PDF semantics

A single input PDF can take any of these shapes:
- **One page, one session** (most common: a single deck-eval page from a single meet/session)
- **Multiple pages, one session** (overflow when >9 officials)
- **Multiple pages, multiple sessions of the same meet** (someone scanned every session of a weekend meet into one PDF)
- **Multi-meet PDF** — disallowed. If a single input contains evals from more than one meet, the tool **fails with a clear error**. Both more likely scenarios — operator scanned the wrong combined stack, or our parser misread a meet identifier on one page — should surface, not be silently merged.

**Meet-level fields on later pages may be skipped or short-formed.** Page 1 is the authoritative source for `competition_name`, `host_club`, `coc`. Pages 2+ may legitimately leave those blank, abbreviate the meet name, or repeat them in full.

**Reconciliation algorithm** (`src/merge.py`):
1. Extract every page independently (vision or form-field).
2. Treat page 1's meet-level fields as authoritative; its `meet_match` is `{"value": "authoritative", "confidence": 1.0}`.
3. For each page N>1:
   - If page N's meet-level fields are blank/short → carry page 1 values forward, `meet_match={"value": "carried", "confidence": 1.0}` (deterministic, no model needed).
   - If page N has meet-level values → ask `qwen2.5:7b` (one cheap text call) "do these refer to the same meet?" given page 1's values and page N's values. The model returns a verdict (`same` / `different` / `unknown`) **and a confidence in [0, 1]**, both captured.
     - `same` → carry forward, `meet_match={"value": "confirmed", "confidence": <model>}`.
     - `different` → raise `MultiMeetError` listing page 1's meet identifiers and page N's, exit code 4.
     - `unknown` → carry forward, `meet_match={"value": "unknown", "confidence": <model>}`. Logged as a warning in non-interactive mode; surfaced in interactive mode like any low-confidence row.
4. Session-level fields (`competition_coordinator`, `cc_level`, `date_session`, `session_number`) **are allowed to vary per page** — a multi-session PDF is fine.

The `meet_match` confidence participates in **the same `low_confidence_threshold`** (default 0.75) used everywhere else: any page whose meet_match confidence falls below it surfaces in `--interactive` mode for human review, and contributes to the row's `row_confidence` composite alongside the field-level confidences.

### Output

**JSON is the canonical output** (CSV and XLSX are derived flat views). This makes per-field confidence first-class, keeps the schema extensible, and is the natural feed for a future web-app review UI.

- **`output/<pdf-stem>.json`** — canonical. Shape:
  ```json
  {
    "source_pdf": "session_3_evals.pdf",
    "template_id": "swim_ontario_v1",
    "template_confidence": 0.98,
    "extraction_method": "vision",   // "vision" | "form_field" | "mixed"
    "vision_model": "qwen2.5vl:7b",
    "edit_model": "qwen2.5:7b",      // only present if --interactive was used
    "meet": {
      "competition_name": {"value": "...", "confidence": 0.97},
      "host_club":        {"value": "...", "confidence": 0.95},
      "coc":              {"value": "...", "confidence": 0.93}
    },
    "evaluations": [
      {
        "source_page": 1,
        "row_index": 1,
        "meet_match":            {"value": "authoritative", "confidence": 1.0},
                                                       // pages 2+ get {value: "confirmed" | "carried" | "unknown", confidence: <model or 1.0>}
                                                       // meet_match.confidence participates in row_confidence and the low-confidence threshold
        "session_number":        {"value": 3,           "confidence": 0.99, "source": "form"},
        "date_session":          {"value": "Apr 11, 2026", "confidence": 0.96},
        "competition_coordinator": {"value": "...",    "confidence": 0.91},
        "cc_level":              {"value": "...",      "confidence": 0.90},
        "official_name":         {"value": "...",      "confidence": 0.94},
        "club":                  {"value": "...",      "confidence": 0.92},
        "position":              {"value": "...",      "confidence": 0.95},
        "lane_number":           {"value": "...",      "confidence": 0.88},
        "times_worked_position": {"value": "...",      "confidence": 0.70},
        "mentor":                {"value": "...",      "confidence": 0.85},
        "level":                 {"value": "...",      "confidence": 0.80},
        "successful":            {"value": true,       "confidence": 0.96, "rationale": "initials present and clear"},
        "row_confidence": 0.84   // composite (e.g. min or weighted mean of field confidences)
      }
    ]
  }
  ```
- **`output/<pdf-stem>.csv`** — derived flat view. One row per evaluation. Columns: all schema fields as plain values plus a single `confidence` column (the row composite). No embedded JSON in cells — keeps the CSV grep/diff-friendly.
- **`output/<pdf-stem>.xlsx`** — same rows as CSV, single `evaluations` sheet, written via `pandas.to_excel` (openpyxl).
- **`output/<pdf-stem>.raw.json`** — per-page raw model response sidecar for debugging and re-runs without re-invoking the model.

**Confidence sourcing.** The vision LLM is asked to emit a confidence in [0, 1] alongside each value. Local VLMs' self-reported confidence is noisy but useful as a relative signal — fine for "which rows need a human's attention." A `low_confidence_threshold` (default 0.75) controls which rows surface in interactive mode.

### CLI (v1 — single PDF)

```
python main.py <path/to/eval.pdf>
    [--output-dir output]
    [--template <id>]                  # override template detection
    [--vision-model qwen2.5vl:7b]
    [--edit-model qwen2.5:7b]
    [--no-cache]
    [--no-auto-pull]                   # don't ollama-pull missing models
    [--interactive]
    [--review-all]
    [--low-confidence-threshold 0.75]
```

**Default is non-interactive** (matches rems-sync's pattern). The tool extracts, writes JSON + CSV + XLSX, exits. No prompts. Best for scripted runs, CI, batch shells.

**`--interactive`** enters an agentic review loop after extraction but before writing output. The CLI:
1. Prints a summary of the meet/session header and row count (the "is this the right document?" guardrail).
2. Surfaces any row whose composite confidence is below `--low-confidence-threshold`, and the specific low-confidence fields within those rows.
3. Asks free-form: `Anything to change before writing the output? (press Enter to write, or describe edits in plain English)`.
4. If the user types something, a text-LLM call (`qwen2.5:7b`) interprets the natural-language instruction as a structured edit operation against the schema (e.g. "the second official on page 1 is Sarah Connor, not Sara Conor" → `evaluations[1].official_name.value = "Sarah Connor"`; "session 3, not 2" → updates the session_number on the affected rows). The edits are applied, a diff is printed, and the loop continues until the user just hits Enter.
5. On Enter, output is written and the run exits.

**`--review-all`** turns step 2 into walk-every-row mode: the CLI steps through each evaluation one at a time, shows the extracted row alongside (eventually) a cropped image of the source row, and accepts free-form edits. For building confidence on the first few real scans before any golden set exists. Implies `--interactive`.

**`--no-cache`** re-invokes the model even if `<stem>.raw.json` exists.

Exit codes: `0` success; `1` extraction failure / Ollama unreachable; `2` validation failure (no recognizable fields on any page); `3` user aborted in interactive mode (Ctrl-C / `:q`); `4` `MultiMeetError` — pages reference more than one meet.

### Project layout

```
canswim-deck-eval-parser/
├── main.py                 # argparse CLI, orchestrates pipeline
├── requirements.txt        # pymupdf, pillow, ollama, pandas, openpyxl, pytest
├── README.md
├── LICENSE                 # MIT 2026 Gavin Bee
├── .gitignore              # mirror canada-swim-tech-survey: .venv/, __pycache__/, output/* (keep .gitkeep), .claude/
├── .github/workflows/
│   └── tests.yml           # pytest on push/PR (Python 3.12)
├── scripts/
│   ├── install.ps1         # Windows: winget Ollama + venv + pip + ollama pull
│   └── install.sh          # macOS/Linux: brew or curl|sh Ollama + venv + pip + ollama pull
├── src/
│   ├── __init__.py
│   ├── schema.py              # dataclasses + canonical field-name constants
│   ├── templates/
│   │   ├── __init__.py        # TEMPLATES registry
│   │   ├── base.py            # Template dataclass
│   │   ├── swim_ontario_v1.py # populated from eval-gen _build_fields()
│   │   ├── swim_quebec_v1.py  # stub: NotImplementedError, reserved
│   │   ├── swim_alberta_v1.py # stub
│   │   └── swim_bc_v1.py      # stub
│   ├── template_detect.py     # page 1 → template_id + confidence via qwen2.5vl:7b
│   ├── ollama_runtime.py      # OllamaDaemon context manager: detect binary, start/stop daemon, ensure-models
│   ├── gpu_detect.py          # nvidia-smi / system_profiler probes → free VRAM in MB; tier picker
│   ├── pdf_io.py              # PyMuPDF: detect form fields, rasterize pages to PNG bytes
│   ├── form_extract.py        # fast path: read widget values via template.widget_field_map
│   ├── vision_extract.py      # Ollama client wrapper; per-page prompt (with template addendum) + JSON parse + retry/validation; session_number AND per-field confidences resolved by the model
│   ├── review.py              # interactive mode: summary, low-confidence surfacing, review-all walk, agentic edit loop
│   ├── edit_apply.py          # turn LLM-interpreted edit instructions into mutations on the schema dict; produce diffs
│   ├── merge.py               # combine per-page dicts into row records; carry meet-level forward; detect inconsistencies
│   └── output.py              # write JSON (canonical) + CSV + XLSX + raw.json sidecar
├── tests/
│   ├── conftest.py         # autouse fixture stubs ollama HTTP client
│   ├── fixtures/
│   │   ├── form_field/         # fillable PDFs copied from eval-gen (committed)
│   │   ├── pseudonymized/      # name-swapped scans + matching golden CSVs (committed, safe to share)
│   │   └── golden/             # JSON golden sets pulled from REMS via rems-sync (committed where pseudonymized)
│   ├── test_schema.py
│   ├── test_templates.py        # registry shape; Ontario template loads; stubs raise NotImplementedError
│   ├── test_template_detect.py  # mocked vision call; below-threshold → error
│   ├── test_ollama_runtime.py   # mocked subprocess + HTTP probe; daemon lifecycle, idempotent ensure-models
│   ├── test_gpu_detect.py       # mocked nvidia-smi output → tier picker returns expected model tag
│   ├── test_pdf_io.py
│   ├── test_form_extract.py     # uses an eval-gen-generated PDF copied into fixtures
│   ├── test_vision_extract.py   # mocks Ollama, asserts prompt (incl. template addendum) + JSON parsing (incl. session_number + confidences)
│   ├── test_review.py           # canned LLM responses; verify low-confidence surfacing and review-all walk
│   ├── test_edit_apply.py       # natural-language edit → schema mutation (mocked LLM)
│   ├── test_merge.py
│   └── test_output.py           # JSON canonical + CSV/XLSX derived parity
├── CONTRIBUTING.md         # branch naming, commit style, design-doc workflow (root, per GitHub convention)
├── docs/
│   ├── installation.md
│   ├── usage.md
│   ├── output-schema.md
│   ├── models.md           # GPU tiers, model choices, Qwen3-VL roadmap
│   ├── architecture.md     # CURRENT design — kept in sync with code
│   ├── troubleshooting.md
│   ├── templates/
│   │   ├── README.md       # how to add a province
│   │   └── swim_ontario.md # field reference
│   └── design/
│       ├── README.md       # index
│       └── 0001-initial-design.md   # this plan, copied in at repo init
├── data/
│   └── .gitkeep            # sample PDFs land here locally (git-ignored)
└── output/
    └── .gitkeep            # CSV/XLSX/JSON outputs land here (git-ignored)
```

### Critical files to reference / reuse

- `C:/Users/gavbe/src/eval-gen/eval_gen.py` — `_build_fields()` defines the exact Swim Ontario field names; copy that list into `src/schema.py` as the canonical constant. PyMuPDF widget-iteration patterns there (`page.widgets()`, `widget.field_name`, `widget.field_value`) port directly into `src/form_extract.py`.
- `C:/Users/gavbe/src/eval-gen/eval_form.pdf` — copy into `tests/fixtures/` (blank) and use one filled session output (e.g. `cc2026/session_1_evals.pdf`) as the form-field fast-path integration fixture.
- `C:/Users/gavbe/src/canada-swim-tech-survey/tests/conftest.py` — autouse-patch pattern; clone the shape but patch `ollama` calls instead of `time.sleep`.
- `C:/Users/gavbe/src/canada-swim-tech-survey/main.py` — argparse + logging.basicConfig boilerplate to mirror.
- `C:/Users/gavbe/src/canada-swim-tech-survey/.gitignore`, `LICENSE`, `README.md` — direct templates.
- `C:/Users/gavbe/src/rems-sync/docs/deck-eval-upload.md` — keep in mind for forward-compat naming; do **not** conform to it in v1, but column names should be a superset.

### What is out of scope for v1

- Batch / directory mode (single PDF only)
- Google Drive fetch
- Direct REMS upload / rems-sync integration
- Position-name normalization to REMS credential prefixes
- GPU-required models / cloud APIs
- Non-Ontario provincial templates (Quebec, Alberta, BC, etc. — stubs only; detection will reject them with a clear message)

### Test data acquisition

I **cannot** auto-pull PDFs from your Google Drive in this environment — Drive auth is out of scope for v1 (locked in earlier). The plan in three tiers:

- **Tier 1 — Synthetic fixtures (committed, no human action).** Copy a few `cc2026/session_*_evals.pdf` files from `C:/Users/gavbe/src/eval-gen/` into `tests/fixtures/form_field/`. These are fillable-PDF outputs, perfect for exercising the form-field fast path, schema, merge, and output code, and small enough to commit. Also include blank `eval_form.pdf`. **No PII** — the names in cc2026 are already from a public meet.
- **Tier 2 — Real scans + REMS golden sets (local only, you collect).** You download a handful of representative scanned PDFs from the Drive folder (`https://drive.google.com/drive/u/0/folders/0AFczXKtVbxcaUk9PVA`) into your local `data/` directory (git-ignored). For each, we use `rems-sync` to fetch the deck-eval credentials REMS already has for that meet/session and dump to a local JSON golden. Variety to aim for: different meets, different scan quality, different filename conventions, at least one multi-session PDF, at least one with crossed-out / not-successful rows. These stay on your machine; integration tests pick them up when present (`pytest.mark.integration`, skipped by default).
- **Tier 3 — Pseudonymized fixtures (committed, safe to share). _Implementation deferred_.** Reserve `tests/fixtures/pseudonymized/` in the layout now so the eventual pipeline has a home. Sketch of the future pipeline:
  1. Start from a Tier-2 scan + its REMS golden CSV.
  2. A local vision/image model identifies bounding boxes for the personal-name fields (Official Name, Mentor, COC, Competition Coordinator) on each page.
  3. Substitute generated pseudonyms (Faker or similar) — same pseudonym used across every appearance of the same real name within a meet to preserve referential integrity.
  4. A generative image model (inpainting / image-to-image, e.g. a local Stable Diffusion variant with ControlNet, or a handwriting style-transfer model) writes the pseudonym onto the scan **in a way that preserves the original character of the source** — the same handwriting style, ink color, pressure, slant, contrast against the paper texture, scan noise, skew, smudges. We explicitly do **not** want clean white-rectangle overlays with a Comic Sans-style "handwriting font" — that would erase exactly the artifacts our parser must learn to handle and make the fixtures trivially easier than real scans. Preserving the "character" of the original is the whole point.
  5. Rewrite the matching CSV cells with the same pseudonym map.
  6. Commit the (image, golden CSV) pair into `tests/fixtures/pseudonymized/` — now public and safe.
  This gives us committed, sharable, end-to-end accuracy tests without ever pushing real official names. v1 reserves the directory and documents the contract; we build the pipeline as a separate follow-up issue.

### Verification

After implementation:

1. **Fresh install on a clean machine** — run `scripts/install.ps1` (Windows) or `scripts/install.sh` (other). Verify it installs Ollama (if missing), creates `.venv`, pip-installs requirements, and pulls both `qwen2.5vl:7b` and `qwen2.5:7b`. Idempotent on re-run.
2. **Ollama lifecycle** — stop the daemon. Run `python main.py tests/fixtures/form_field/session_1_evals.pdf`. Confirm the parser auto-starts the daemon, runs (form-field path — no model needed but daemon-start is still exercised since template-detection uses the vision model), and the daemon process is gone after exit. Re-run with the daemon already running and confirm the parser leaves it running.
3. **Unit tests**: `pytest tests/ -q` — must pass with Ollama **not** running and **no** local scan fixtures (integration tests skipped). All LLM calls (vision + text) and all subprocess spawns are mocked.
4. **Form-field fast path (non-interactive default)**: `python main.py tests/fixtures/form_field/session_1_evals.pdf` — emits JSON + CSV + XLSX with 9 officials × N pages, `template_id=swim_ontario_v1`, `extraction_method=form_field`, exits without prompting.
5. **Template detection**: same fixture should yield `template_id=swim_ontario_v1` with confidence > 0.9. Forcing `--template swim_quebec_v1` should immediately raise `NotImplementedError` from the stub.
6. **GPU tier auto-pick**: with no `--vision-model` flag, the startup log should print the detected VRAM and the chosen tier (e.g. `Detected 24 GB VRAM → using qwen2.5vl:32b`). Force a smaller model with `--vision-model qwen2.5vl:7b` and confirm it overrides.
7. **Vision path smoke test (non-interactive)**: place a real scanned PDF in `data/`, run `python main.py data/<scan>.pdf`. Spot-check the JSON against the visible form. Confirm `extraction_method=vision`, `successful` correctly reflects sign-off intent across the row (initials present, crossed-out row → false, ambiguous → null), and per-field `confidence` values are populated.
8. **Interactive agentic edit loop**: `python main.py data/<scan>.pdf --interactive`. Verify the summary prints, low-confidence rows surface, and a free-form prompt like `"the third official's name is Tatiana Maslany, not Tatiana Mazlany"` is interpreted by the text-LLM and applied as a targeted mutation, with a diff shown. Pressing Enter writes output; the JSON reflects the edits.
9. **Review-all mode**: `python main.py data/<scan>.pdf --review-all` walks every evaluation one at a time and accepts free-form edits at each.
10. **Session inference (model-driven)**: rename a fixture PDF to `meet-session_3.pdf` whose form lacks an explicit session number; confirm the model returns `session_number=3` with `session_number_source=filename`.
11. **Multi-page reconciliation**: concatenate two eval-gen-generated session PDFs from the **same** meet into one input PDF — parser should succeed, each page's session_number distinct, `meet_match` `carried` or `confirmed` on later pages. Concatenate two session PDFs from **different** meets — parser must exit 4 with `MultiMeetError` citing both meet names.
12. **Output parity**: row count and per-field values in CSV and XLSX match the JSON `evaluations` array; the CSV `confidence` column equals each row's `row_confidence`.
13. **Golden integration test** (when scan + REMS golden fixtures are present locally): `pytest -m integration` — parser output matches the REMS golden set on (official, position, successful) tuples for the matched meet/session.
14. **CI**: push a branch; `.github/workflows/tests.yml` runs pytest on Python 3.12 (non-integration only) and passes.

## Implementation order — to file as GitHub issues

The first concrete action after this plan is approved is to **create the GitHub repo**, then file the issues below in order. Each becomes one issue, one branch (`{n}-{slug}`), one PR. Lower-numbered issues land first; later issues assume the earlier ones are done. Dependencies noted where they cross.

1. **Bootstrap repo** — initialize `canswim-deck-eval-parser` on GitHub, push `README.md` (stub), `LICENSE` (MIT 2026 Gavin Bee), `.gitignore` (mirror canada-swim-tech-survey), `CONTRIBUTING.md`, `requirements.txt`, `.github/workflows/tests.yml` skeleton, and copy **this design doc** to `docs/design/0001-initial-design.md`. Land before everything else.
2. **Schema + Swim Ontario template module** — `src/schema.py`, `src/templates/base.py`, `src/templates/swim_ontario_v1.py` (populated from eval-gen's `_build_fields()`), `src/templates/__init__.py` (TEMPLATES registry, plus stubs for QC/AB/BC raising `NotImplementedError`). Tests.
3. **PyMuPDF form-field fast path** — `src/pdf_io.py` (rasterize + widget detection), `src/form_extract.py`. Tests use eval-gen-generated PDF copied to `tests/fixtures/form_field/`. (Depends on #2.)
4. **Output module** — `src/output.py` writing JSON canonical + CSV + XLSX + raw.json sidecar. Tests. (Depends on #2.)
5. **Merge module** — `src/merge.py` combining per-page dicts into row records, carrying meet-level fields forward across pages, and enforcing the same-meet invariant (see "Multi-page PDF semantics" below). (Depends on #2.)
6. **End-to-end form-field CLI** — `main.py` argparse skeleton; non-interactive default; wires #2–#5 together for the form-field path only. First runnable version. (Depends on #2–#5.)
7. **Ollama runtime** — `src/ollama_runtime.py`: binary detection, daemon lifecycle (subprocess + atexit), model ensure-pull, helpful install messages. Tests mock subprocess. (Independent of #6 but needed for #9.)
8. **GPU detection + tier picker** — `src/gpu_detect.py`. Tests mock `nvidia-smi`. (Independent.)
9. **Vision extraction** — `src/vision_extract.py`: per-page prompt (with template addendum and filename hint), JSON parse, retry/validation, per-field confidence emission, session_number resolution by the model. (Depends on #7.)
10. **Template detection** — `src/template_detect.py`: page-1 classification call against the registry. (Depends on #7 and #9.)
11. **End-to-end vision CLI** — wire vision path into `main.py`; template-detect → vision-extract → merge → output. Smoke-test against the first user-supplied real scan. (Depends on #9, #10.)
12. **Install scripts** — `scripts/install.ps1`, `scripts/install.sh`. Idempotent. (Independent.)
13. **Interactive review (low-confidence surface + summary)** — `src/review.py` summary + low-confidence row print; `--interactive` flag wires it in. (Depends on #11.)
14. **Agentic edit loop** — `src/edit_apply.py` (LLM-interpreted natural-language edits → schema mutations + diff), wired into `src/review.py`. (Depends on #13.)
15. **`--review-all` walk-every-row mode** — extends `src/review.py`. (Depends on #13.)
16. **Documentation pass** — populate `docs/installation.md`, `docs/usage.md`, `docs/output-schema.md`, `docs/models.md`, `docs/architecture.md`, `docs/troubleshooting.md`, `docs/templates/README.md`, `docs/templates/swim_ontario.md`, `docs/design/README.md`. Tighten the top-level `README.md` into a navigation surface. (Depends on #1–#15; do after the surface is stable.)
17. **Golden integration test harness** — `pytest -m integration`, reads from `tests/fixtures/golden/` when present, skipped otherwise. Wire to rems-sync golden-set generation steps in `docs/troubleshooting.md` or a dedicated `docs/testing.md`. (Depends on #11.)

## Open items deferred to follow-up issues (file these too once we start)

- Batch / directory mode
- Google Drive fetch subcommand (mirror rems-sync's gspread/google-auth approach)
- Hookup to rems-sync (position-name normalization, REMS-ID resolution from an Officials roster, DD/MM/YYYY date format)
- Additional provincial templates: `swim_quebec_v1` (French, different date formats), `swim_alberta_v1`, `swim_bc_v1`, others as needed
- **Pseudonymization pipeline** — take a real scanned PDF + golden CSV and emit a committable name-swapped pair into `tests/fixtures/pseudonymized/`, using a generative image model that preserves the handwriting character of the original. Directory and contract reserved in v1; implementation later.
- **Qwen3-VL swap** — replace `qwen2.5vl:7b` / `qwen2.5vl:32b` with the Qwen3-VL equivalents once they're stable on Ollama. One-line model-tag change + regression run against golden fixtures.
- **GitHub Pages for docs** — publish `docs/` via Jekyll once it outgrows easy in-repo navigation.
- **Web app** — the JSON-canonical output is a natural backend for a browser-based review UI: drag-and-drop a PDF, see extracted rows side-by-side with cropped source-image snippets, edit in-place, export. Out of scope for v1 but worth keeping the JSON shape stable enough to support it.
