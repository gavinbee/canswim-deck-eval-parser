"""Shared pytest fixtures and collection hooks.

For now this houses the bookkeeping shape; the Ollama HTTP-client stub and
subprocess-spawn stub land alongside their respective modules
(``src.ollama_runtime``) so that test_*.py files can opt in or out.

It also gates ``@pytest.mark.integration`` tests: they invoke the real
vision model via Ollama and are **skipped unless ``pytest -m integration``
is passed** (see CONTRIBUTING.md). A plain ``pytest`` run — including CI —
collects them but skips, so the default suite never needs a GPU or a
running daemon.
"""
import pytest


def pytest_addoption(parser):
    """CLI options for the integration suite (see test_integration_vision)."""
    group = parser.getgroup("integration")
    group.addoption(
        "--vision-model",
        action="store",
        default=None,
        help="Vision model tag for integration tests "
             "(default: vision_extract.DEFAULT_VISION_MODEL).",
    )
    group.addoption(
        "--pull-models",
        action="store_true",
        default=False,
        help="Allow integration tests to `ollama pull` a missing model "
             "(a multi-GB download). Off by default: a missing model skips "
             "with a copy-paste pull hint instead.",
    )


def pytest_collection_modifyitems(config, items):
    """Skip integration tests unless ``-m integration`` was requested.

    We key off the ``-m`` marker expression rather than inventing a custom
    flag so the documented invocation (``pytest -m integration``) is the
    single gate. When the user explicitly selects integration tests, pytest
    already deselects everything else, so we leave the items untouched.
    """
    markexpr = config.option.markexpr or ""
    if "integration" in markexpr:
        return
    skip_integration = pytest.mark.skip(
        reason="integration test; run with `pytest -m integration`"
    )
    for item in items:
        if "integration" in item.keywords:
            item.add_marker(skip_integration)
