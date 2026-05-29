# Contributing

This repo follows the same patterns as [`canada-swim-tech-survey`](https://github.com/swimblocks/swim-club-tech-survey).

## Workflow

1. **File or claim a GitHub issue** for what you're going to do.
2. **Create a branch** named `{issue-number}-{kebab-slug}`. Example: `12-add-quebec-template`.
3. **Open a PR** that references the issue (`Closes #12`).
4. **Squash-merge** to `main` when reviewed.

## Commit messages

Imperative present-tense. No conventional-commit prefixes (`feat:`, `fix:` etc.). Match the style of `git log` on `main`.

Examples:
- `Add Swim Ontario v1 template module`
- `Fix multi-page meet_match carry-forward for empty fields`
- `Restore the qwen2.5:7b edit-model after model rename`

## Code style

- Python 3.12.
- Standard library imports, then third-party, then local — separated by blank lines.
- No linter or formatter is enforced (matches `canada-swim-tech-survey`); just be consistent with what's already in the file.
- Each module: `log = logging.getLogger(__name__)` at the top. Use `log.info` / `log.warning` / `log.error`; `print()` is reserved for the CLI's direct user-facing output.

## Testing

```
pytest tests/ -q
```

Integration tests are skipped unless `pytest -m integration` is passed. They expect real-scan fixtures in `data/` and golden sets in `tests/fixtures/golden/`.

All LLM calls (vision and text) must be mocked in non-integration tests. The `tests/conftest.py` autouse fixture stubs the `ollama` HTTP client and any `subprocess.Popen` that would spawn `ollama serve`.

## Design-doc workflow

Any **non-trivial feature** gets a design doc in [`docs/design/`](docs/design/) **before** implementation. Use [`0001-initial-design.md`](docs/design/0001-initial-design.md) as the template: Context → Design → Verification → Open items.

- Filename: `NNNN-short-name.md`, sequential, never reused.
- PR that adds the feature references the design doc.
- After landing, **update [`docs/architecture.md`](docs/architecture.md)** to reflect the new state.
- Design docs themselves are append-only; add a status header at the top (`Status: implemented` / `Status: superseded by NNNN` / `Status: abandoned`).

**Trivial** = typo, single-line fix, dependency bump, doc edit. **Non-trivial** = adding a module, changing the output schema, changing a model, adding a CLI flag of consequence, or touching the pipeline shape.

## Templates

To add a new provincial template, see [`docs/templates/README.md`](docs/templates/README.md).
