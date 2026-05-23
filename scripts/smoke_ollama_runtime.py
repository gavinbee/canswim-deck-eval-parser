"""Manual smoke test for ``src.ollama_runtime``.

Validates the parts that mocks can't fully cover: real binary discovery,
real subprocess.Popen + Windows process-group signaling, real HTTP probe
against a real ``ollama serve`` daemon. Skips the model-pull step — that
behavior is already covered by unit tests with mocked streams, and we'd
rather not download hundreds of megabytes for a quick check.

Run::

    python scripts/smoke_ollama_runtime.py

What it does, in order:

1. ``find_binary()`` — prints the absolute path to ``ollama``.
2. ``is_daemon_running()`` — reports whether the daemon was already up
   on the user's machine (we'll preserve that state).
3. Enter ``OllamaDaemon(required_models=[])`` — if the daemon wasn't
   running we spawn it and wait until ``/api/tags`` answers; if it was,
   we leave it alone.
4. Make a real ``client.list()`` call inside the ``with`` block to
   confirm the API is genuinely usable.
5. Exit the context — daemon stops if and only if we started it.
6. ``is_daemon_running()`` once more — should match step 2's reading.

Output is meant to be human-readable. Exit code 0 if everything looked
right; non-zero on any unexpected outcome.
"""
from __future__ import annotations

import logging
import sys

from src import ollama_runtime
from src.ollama_runtime import OllamaDaemon


def _say(msg: str) -> None:
    print(f"[smoke] {msg}", flush=True)


def main() -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s  %(levelname)-7s  %(message)s",
        datefmt="%H:%M:%S",
    )

    # 1. Binary discovery
    try:
        path = ollama_runtime.find_binary()
    except ollama_runtime.OllamaBinaryMissingError as exc:
        _say("FAIL: ollama not found on PATH.")
        print(exc, file=sys.stderr)
        return 2
    _say(f"found ollama binary at: {path}")

    # 2. Initial daemon state
    initially_running = ollama_runtime.is_daemon_running()
    _say(f"daemon initially running? {initially_running}")

    # 3. + 4. Enter the context manager and prove the API works
    try:
        with OllamaDaemon(required_models=[]) as runtime:
            _say(
                f"inside context: we_started_daemon={runtime._we_started_daemon}, "
                f"proc={runtime._proc.pid if runtime._proc else None}"
            )
            client = runtime.client()
            tags = ollama_runtime.list_pulled_models()
            _say(f"list_pulled_models() returned {len(tags)} model(s): {tags}")
            # Issue a second call just to be sure connection reuse works.
            client.list()
            _say("second client.list() succeeded — daemon is reachable")
    except Exception as exc:
        _say(f"FAIL: exception inside context manager: {exc!r}")
        return 3

    # 5. + 6. Verify final state matches the initial state
    finally_running = ollama_runtime.is_daemon_running()
    _say(f"daemon running after exit? {finally_running}")

    if initially_running and not finally_running:
        _say("FAIL: daemon was running before but we stopped it — that's a bug.")
        return 4
    if not initially_running and finally_running:
        _say("FAIL: daemon is still running after exit — we should have stopped it.")
        return 5

    _say("PASS: lifecycle behaved correctly.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
