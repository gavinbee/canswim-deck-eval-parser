"""Manage the local Ollama daemon and pull required models on demand.

The parser uses Ollama for vision extraction (and, when ``--interactive``,
text edits). Ollama itself is a runtime dependency the user installs
once. Beyond that, *we* manage the daemon lifecycle and model availability
so the user never has to think about it::

    with OllamaDaemon(required_models=["qwen2.5vl:7b"]) as runtime:
        client = runtime.client()
        ...  # extraction calls
    # daemon stopped on exit (if we started it; left alone otherwise)

Three checks happen on ``__enter__`` (in order):

1. **Binary present?** ``shutil.which("ollama")``. If missing we raise
   :class:`OllamaBinaryMissingError` with copy-paste install instructions.
2. **Daemon running?** Probe via ``ollama.Client().list()``. If unreachable
   we spawn ``ollama serve`` and poll for readiness; ``__exit__`` will
   terminate it. If the user already had it running, we leave it.
3. **Models pulled?** Call ``Client.list()``; for each required tag not
   present, either ``ollama pull <tag>`` (with progress streamed to
   stderr) or, if ``auto_pull=False``, raise
   :class:`OllamaModelMissingError` with the manual command.

Tests mock ``shutil.which``, the HTTP client, and ``subprocess.Popen`` —
this module never actually shells out during the test suite.
"""
from __future__ import annotations

import logging
import os
import shutil
import signal
import subprocess
import sys
import time
from typing import Iterable, Optional

import ollama

log = logging.getLogger(__name__)


# Default Ollama HTTP endpoint. Made overridable mostly for tests.
DEFAULT_HOST = "http://localhost:11434"

# How long ``__enter__`` waits for a daemon we just spawned to start
# answering API calls. Real-world: 1-3 seconds on a typical machine.
DAEMON_START_TIMEOUT_S = 30.0

# Poll interval while waiting for the daemon.
DAEMON_POLL_INTERVAL_S = 0.25

# Graceful-shutdown grace period before SIGKILL.
DAEMON_STOP_TIMEOUT_S = 5.0


# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------


class OllamaRuntimeError(RuntimeError):
    """Base class for everything this module raises."""


class OllamaBinaryMissingError(OllamaRuntimeError):
    """``ollama`` is not on PATH. Message includes copy-paste install commands."""


class OllamaDaemonStartTimeoutError(OllamaRuntimeError):
    """We spawned ``ollama serve`` but it never started answering API calls."""


class OllamaModelMissingError(OllamaRuntimeError):
    """A required model isn't pulled and ``auto_pull`` is off."""


class OllamaModelPullFailedError(OllamaRuntimeError):
    """``ollama pull <tag>`` ran but didn't succeed."""


# ---------------------------------------------------------------------------
# Standalone helpers (also exposed publicly for tests + CLI bootstrap)
# ---------------------------------------------------------------------------


def find_binary() -> str:
    """Locate the ``ollama`` executable on PATH.

    Returns:
        Absolute path to the binary.

    Raises:
        OllamaBinaryMissingError: with platform-aware install hints.
    """
    path = shutil.which("ollama")
    if path:
        return path
    raise OllamaBinaryMissingError(_install_hint())


def _install_hint() -> str:
    """Platform-aware ``ollama`` install message for error output."""
    return (
        "Ollama is not installed (or not on PATH).\n"
        "Install it with one of:\n"
        "    Windows:  winget install Ollama.Ollama\n"
        "    macOS:    brew install ollama\n"
        "    Linux:    curl -fsSL https://ollama.com/install.sh | sh\n"
        "Or run the bundled installer:  scripts/install.ps1   (Windows)\n"
        "                               scripts/install.sh    (other)\n"
        "Full guide: "
        "https://github.com/gavinbee/canswim-deck-eval-parser/blob/main/docs/installation.md"
    )


def is_daemon_running(host: str = DEFAULT_HOST) -> bool:
    """True if ``GET /api/tags`` succeeds at ``host``.

    Uses the ``ollama`` Python client (which uses ``httpx`` under the
    hood) for consistency with everywhere else. Any exception from the
    client counts as "not running" — we don't try to discriminate
    between "no daemon" and "daemon but broken" here; the latter will
    surface clearly when an actual generate / pull is attempted.
    """
    try:
        ollama.Client(host=host).list()
        return True
    except Exception as exc:  # noqa: BLE001 — see docstring
        log.debug("Ollama daemon probe at %s failed: %s", host, exc)
        return False


def list_pulled_models(host: str = DEFAULT_HOST) -> list[str]:
    """Return every locally-pulled model tag (``name:tag`` form)."""
    response = ollama.Client(host=host).list()
    # ollama-python returns ``ListResponse`` with a ``models`` attribute;
    # each entry has a ``model`` field that holds the tag.
    out: list[str] = []
    for entry in getattr(response, "models", []) or []:
        tag = getattr(entry, "model", None) or getattr(entry, "name", None)
        if tag:
            out.append(tag)
    return out


def pull_model(
    tag: str,
    host: str = DEFAULT_HOST,
    progress_stream=sys.stderr,
) -> None:
    """``ollama pull <tag>`` with progress streamed to ``progress_stream``.

    ``ollama.Client.pull(stream=True)`` yields ``ProgressResponse``
    objects with a ``status`` string (e.g. ``"pulling manifest"``,
    ``"downloading"``) and totals for download progress. We collapse
    that into one human-readable line per status transition so the
    output stays scannable even on a big multi-GB pull.

    Raises:
        OllamaModelPullFailedError: if the stream ends without a
            "success" status. The ollama HTTP API surfaces failures as
            either an exception (bubbles up directly) or a final
            response whose status is something other than ``success``.
    """
    client = ollama.Client(host=host)
    last_status: Optional[str] = None
    saw_success = False

    log.info("Pulling Ollama model %s …", tag)
    try:
        for chunk in client.pull(tag, stream=True):
            status = getattr(chunk, "status", "") or ""
            if status and status != last_status:
                # Only print when the status string changes — keeps the
                # output to a handful of lines for the whole pull.
                print(f"  ollama pull {tag}: {status}", file=progress_stream)
                last_status = status
            if status == "success":
                saw_success = True
    except ollama.ResponseError as exc:
        raise OllamaModelPullFailedError(
            f"ollama pull {tag} failed: {exc}"
        ) from exc

    if not saw_success:
        raise OllamaModelPullFailedError(
            f"ollama pull {tag} completed without a success status "
            f"(last seen: {last_status!r})"
        )


def ensure_models(
    required: Iterable[str],
    host: str = DEFAULT_HOST,
    auto_pull: bool = True,
    progress_stream=sys.stderr,
) -> list[str]:
    """Pull any of ``required`` that aren't already local.

    Args:
        required: Model tags to ensure (e.g. ``["qwen2.5vl:7b"]``).
        host: Ollama HTTP endpoint.
        auto_pull: If False, raise :class:`OllamaModelMissingError` for
            any missing model rather than pulling it.
        progress_stream: Where pull progress lines go.

    Returns:
        The list of tags that were actually pulled (empty if all were
        already present).
    """
    required = list(required)
    if not required:
        return []

    pulled = set(list_pulled_models(host=host))
    missing = [t for t in required if t not in pulled]

    if not missing:
        log.debug("All required Ollama models already pulled: %s", required)
        return []

    if not auto_pull:
        raise OllamaModelMissingError(
            "Required Ollama model(s) are not pulled and --no-auto-pull "
            "was set:\n"
            + "\n".join(f"    ollama pull {tag}" for tag in missing)
            + "\nRun the commands above, or omit --no-auto-pull."
        )

    for tag in missing:
        pull_model(tag, host=host, progress_stream=progress_stream)
    return missing


# ---------------------------------------------------------------------------
# Daemon lifecycle context manager
# ---------------------------------------------------------------------------


class OllamaDaemon:
    """Context manager that owns the Ollama daemon for the duration.

    Usage::

        with OllamaDaemon(required_models=["qwen2.5vl:7b"]) as runtime:
            client = runtime.client()
            ...

    If the daemon was already running on entry, we don't touch it on
    exit. If we spawned it, we ``terminate()`` → ``wait(5)`` → ``kill()``.
    """

    def __init__(
        self,
        required_models: Optional[Iterable[str]] = None,
        *,
        host: str = DEFAULT_HOST,
        auto_pull: bool = True,
        start_timeout_s: float = DAEMON_START_TIMEOUT_S,
        progress_stream=sys.stderr,
    ) -> None:
        self.required_models: list[str] = list(required_models or [])
        self.host = host
        self.auto_pull = auto_pull
        self.start_timeout_s = start_timeout_s
        self._progress_stream = progress_stream
        self._we_started_daemon = False
        self._proc: Optional[subprocess.Popen] = None

    # ---- lifecycle -----------------------------------------------------

    def __enter__(self) -> "OllamaDaemon":
        # 1. Binary on PATH?
        find_binary()  # raises OllamaBinaryMissingError if absent

        # 2. Daemon already up?
        if is_daemon_running(self.host):
            log.debug("Ollama daemon already running at %s", self.host)
        else:
            self._start_daemon_and_wait()

        # 3. Models pulled?
        ensure_models(
            self.required_models,
            host=self.host,
            auto_pull=self.auto_pull,
            progress_stream=self._progress_stream,
        )

        return self

    def __exit__(self, exc_type, exc_val, exc_tb) -> None:
        if self._we_started_daemon and self._proc is not None:
            self._stop_daemon()

    # ---- accessors -----------------------------------------------------

    def client(self) -> ollama.Client:
        """Return an ``ollama.Client`` bound to this daemon's host."""
        return ollama.Client(host=self.host)

    # ---- internals -----------------------------------------------------

    def _start_daemon_and_wait(self) -> None:
        """Spawn ``ollama serve`` and poll until the API is alive."""
        log.info("Starting ollama serve …")
        # ``ollama serve`` blocks while running. Detach it from our
        # stdin/stdout so it doesn't compete with our CLI output, but
        # keep stderr passthrough so genuine startup failures land
        # somewhere visible.
        self._proc = subprocess.Popen(
            ["ollama", "serve"],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=sys.stderr,
            # On Windows, give the subprocess its own process group so
            # Ctrl-C in our terminal doesn't tear it down before we get
            # to clean up.
            creationflags=(
                subprocess.CREATE_NEW_PROCESS_GROUP
                if os.name == "nt" else 0
            ),
        )
        self._we_started_daemon = True

        deadline = time.monotonic() + self.start_timeout_s
        while time.monotonic() < deadline:
            if self._proc.poll() is not None:
                raise OllamaDaemonStartTimeoutError(
                    f"ollama serve exited with code {self._proc.returncode} "
                    "before becoming reachable"
                )
            if is_daemon_running(self.host):
                log.debug("Ollama daemon is up (took ~%.1fs)",
                          self.start_timeout_s - (deadline - time.monotonic()))
                return
            time.sleep(DAEMON_POLL_INTERVAL_S)

        # Timeout — kill what we started.
        self._stop_daemon()
        raise OllamaDaemonStartTimeoutError(
            f"ollama serve did not become reachable within "
            f"{self.start_timeout_s}s"
        )

    def _stop_daemon(self) -> None:
        """Terminate gracefully, kill if it doesn't go."""
        proc = self._proc
        if proc is None:
            return
        log.info("Stopping ollama serve (pid=%d) …", proc.pid)
        try:
            # On Windows, terminate the whole process group so child
            # workers go down too.
            if os.name == "nt":
                proc.send_signal(signal.CTRL_BREAK_EVENT)
            else:
                proc.terminate()
            try:
                proc.wait(timeout=DAEMON_STOP_TIMEOUT_S)
                return
            except subprocess.TimeoutExpired:
                log.warning(
                    "ollama serve did not exit within %.1fs of terminate(); "
                    "killing.", DAEMON_STOP_TIMEOUT_S,
                )
                proc.kill()
                proc.wait(timeout=DAEMON_STOP_TIMEOUT_S)
        except ProcessLookupError:
            # Already gone — fine.
            pass
        finally:
            self._proc = None
            self._we_started_daemon = False
