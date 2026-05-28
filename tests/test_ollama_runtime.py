"""Tests for src/ollama_runtime.py.

Every external dependency is mocked — these tests never spawn a real
``ollama serve`` and never make a real HTTP call. The aim is to pin
behaviour across:

* binary discovery and the install-hint error path
* daemon-already-running vs daemon-we-spawned lifecycles
* spawn timeout and clean shutdown
* model availability check, auto-pull, and ``--no-auto-pull``
* pull progress streaming
"""
from __future__ import annotations

import io
import subprocess
from types import SimpleNamespace
from typing import Iterable
from unittest.mock import MagicMock, patch

import pytest

from src import ollama_runtime
from src.ollama_runtime import (
    DAEMON_START_TIMEOUT_S,
    OllamaBinaryMissingError,
    OllamaDaemon,
    OllamaDaemonStartTimeoutError,
    OllamaModelMissingError,
    OllamaModelPullFailedError,
    ensure_models,
    find_binary,
    is_daemon_running,
    list_pulled_models,
    pull_model,
)


# ---------------------------------------------------------------------------
# find_binary
# ---------------------------------------------------------------------------


class TestFindBinary:
    def test_returns_path_when_present(self):
        with patch("src.ollama_runtime.shutil.which", return_value="/usr/bin/ollama"):
            assert find_binary() == "/usr/bin/ollama"

    def test_raises_with_install_hint_when_missing(self):
        with patch("src.ollama_runtime.shutil.which", return_value=None):
            with pytest.raises(OllamaBinaryMissingError) as exc:
                find_binary()
        msg = str(exc.value)
        # The error should be self-contained: how to install on every
        # supported platform.
        assert "winget install Ollama.Ollama" in msg
        assert "brew install ollama" in msg
        assert "curl" in msg and "ollama.com/install.sh" in msg
        # And it should point users to the bundled installer + docs.
        assert "scripts/install.ps1" in msg
        assert "docs/installation.md" in msg


# ---------------------------------------------------------------------------
# is_daemon_running
# ---------------------------------------------------------------------------


class TestIsDaemonRunning:
    def test_true_when_list_succeeds(self):
        fake_client = MagicMock()
        fake_client.list.return_value = SimpleNamespace(models=[])
        with patch("src.ollama_runtime.ollama.Client", return_value=fake_client):
            assert is_daemon_running() is True

    def test_false_when_client_raises(self):
        fake_client = MagicMock()
        fake_client.list.side_effect = ConnectionError("not running")
        with patch("src.ollama_runtime.ollama.Client", return_value=fake_client):
            assert is_daemon_running() is False

    def test_false_on_any_exception(self):
        # Even unexpected errors are treated as "not running" — a
        # broken daemon will surface clearly on the next real call.
        fake_client = MagicMock()
        fake_client.list.side_effect = RuntimeError("something else")
        with patch("src.ollama_runtime.ollama.Client", return_value=fake_client):
            assert is_daemon_running() is False


# ---------------------------------------------------------------------------
# list_pulled_models
# ---------------------------------------------------------------------------


class TestListPulledModels:
    def test_extracts_tags_from_response(self):
        fake_client = MagicMock()
        fake_client.list.return_value = SimpleNamespace(models=[
            SimpleNamespace(model="qwen2.5vl:7b"),
            SimpleNamespace(model="qwen2.5:7b"),
        ])
        with patch("src.ollama_runtime.ollama.Client", return_value=fake_client):
            assert list_pulled_models() == ["qwen2.5vl:7b", "qwen2.5:7b"]

    def test_empty_models_list_returns_empty(self):
        fake_client = MagicMock()
        fake_client.list.return_value = SimpleNamespace(models=[])
        with patch("src.ollama_runtime.ollama.Client", return_value=fake_client):
            assert list_pulled_models() == []

    def test_falls_back_to_name_attribute(self):
        # Older ollama-python versions used .name on each entry instead
        # of .model. We accept either so a version bump doesn't break us.
        fake_client = MagicMock()
        fake_client.list.return_value = SimpleNamespace(models=[
            SimpleNamespace(name="qwen2.5vl:3b", model=None),
        ])
        with patch("src.ollama_runtime.ollama.Client", return_value=fake_client):
            assert list_pulled_models() == ["qwen2.5vl:3b"]


# ---------------------------------------------------------------------------
# pull_model
# ---------------------------------------------------------------------------


def _progress_chunks(*statuses: str) -> Iterable:
    return iter(SimpleNamespace(status=s) for s in statuses)


class TestPullModel:
    def test_streams_status_lines_to_progress_stream(self):
        fake_client = MagicMock()
        fake_client.pull.return_value = _progress_chunks(
            "pulling manifest", "downloading", "downloading", "success"
        )
        out = io.StringIO()
        with patch("src.ollama_runtime.ollama.Client", return_value=fake_client):
            pull_model("qwen2.5vl:7b", progress_stream=out)
        printed = out.getvalue()
        # One line per status TRANSITION (so the repeated "downloading"
        # collapses into a single line).
        assert printed.count("\n") == 3
        assert "pulling manifest" in printed
        assert "downloading" in printed
        assert "success" in printed

    def test_passes_stream_kwarg_true(self):
        fake_client = MagicMock()
        fake_client.pull.return_value = _progress_chunks("success")
        with patch("src.ollama_runtime.ollama.Client", return_value=fake_client):
            pull_model("qwen2.5vl:7b", progress_stream=io.StringIO())
        fake_client.pull.assert_called_once_with("qwen2.5vl:7b", stream=True)

    def test_no_success_status_raises(self):
        fake_client = MagicMock()
        fake_client.pull.return_value = _progress_chunks(
            "pulling manifest", "downloading"  # no "success"
        )
        with patch("src.ollama_runtime.ollama.Client", return_value=fake_client):
            with pytest.raises(OllamaModelPullFailedError) as exc:
                pull_model("qwen2.5vl:7b", progress_stream=io.StringIO())
        assert "downloading" in str(exc.value)

    def test_response_error_wraps(self):
        import ollama
        fake_client = MagicMock()
        fake_client.pull.side_effect = ollama.ResponseError("404 not found")
        with patch("src.ollama_runtime.ollama.Client", return_value=fake_client):
            with pytest.raises(OllamaModelPullFailedError) as exc:
                pull_model("qwen2.5vl:7b", progress_stream=io.StringIO())
        assert "404 not found" in str(exc.value)


# ---------------------------------------------------------------------------
# ensure_models
# ---------------------------------------------------------------------------


class TestEnsureModels:
    def test_no_required_is_a_noop(self):
        # Calls neither list() nor pull(). Verified by patching both
        # and asserting they aren't touched.
        with patch("src.ollama_runtime.list_pulled_models") as ll, \
             patch("src.ollama_runtime.pull_model") as pm:
            pulled = ensure_models([])
        assert pulled == []
        ll.assert_not_called()
        pm.assert_not_called()

    def test_all_present_is_a_noop(self):
        with patch(
            "src.ollama_runtime.list_pulled_models",
            return_value=["qwen2.5vl:7b", "qwen2.5:7b"],
        ), patch("src.ollama_runtime.pull_model") as pm:
            pulled = ensure_models(["qwen2.5vl:7b"])
        assert pulled == []
        pm.assert_not_called()

    def test_pulls_missing(self):
        with patch(
            "src.ollama_runtime.list_pulled_models",
            return_value=["qwen2.5vl:7b"],
        ), patch("src.ollama_runtime.pull_model") as pm:
            pulled = ensure_models(["qwen2.5vl:7b", "qwen2.5:7b"])
        assert pulled == ["qwen2.5:7b"]
        pm.assert_called_once()
        # First positional arg is the missing tag.
        assert pm.call_args.args[0] == "qwen2.5:7b"

    def test_no_auto_pull_raises_with_manual_command(self):
        with patch(
            "src.ollama_runtime.list_pulled_models",
            return_value=[],
        ):
            with pytest.raises(OllamaModelMissingError) as exc:
                ensure_models(["qwen2.5vl:7b", "qwen2.5:7b"], auto_pull=False)
        msg = str(exc.value)
        # Both missing tags should appear in the manual command list.
        assert "ollama pull qwen2.5vl:7b" in msg
        assert "ollama pull qwen2.5:7b" in msg
        assert "--no-auto-pull" in msg


# ---------------------------------------------------------------------------
# OllamaDaemon — lifecycle
# ---------------------------------------------------------------------------


class TestOllamaDaemonAlreadyRunning:
    def test_enters_without_spawning(self):
        with patch("src.ollama_runtime.find_binary", return_value="/u/b/ollama"), \
             patch("src.ollama_runtime.is_daemon_running", return_value=True), \
             patch("src.ollama_runtime.ensure_models") as em, \
             patch("src.ollama_runtime.subprocess.Popen") as popen:
            with OllamaDaemon(required_models=["qwen2.5vl:7b"]) as runtime:
                assert runtime._we_started_daemon is False
                assert runtime._proc is None
            popen.assert_not_called()
            em.assert_called_once()

    def test_exit_does_not_stop_externally_managed_daemon(self):
        with patch("src.ollama_runtime.find_binary"), \
             patch("src.ollama_runtime.is_daemon_running", return_value=True), \
             patch("src.ollama_runtime.ensure_models"):
            d = OllamaDaemon()
            with d:
                pass
            # No proc was held, so nothing to stop.
            assert d._proc is None


class TestOllamaDaemonStartsAndStops:
    def setup_method(self):
        # Sequence of is_daemon_running returns: False once (so we
        # spawn) then True (so the poll loop succeeds immediately).
        self.is_running_side_effects = iter([False, True])

    def test_spawns_serve_when_not_running(self):
        fake_proc = MagicMock()
        fake_proc.poll.return_value = None
        fake_proc.pid = 1234

        with patch("src.ollama_runtime.find_binary"), \
             patch(
                "src.ollama_runtime.is_daemon_running",
                side_effect=lambda *_a, **_k: next(self.is_running_side_effects),
             ), \
             patch("src.ollama_runtime.ensure_models"), \
             patch("src.ollama_runtime.subprocess.Popen", return_value=fake_proc) as popen, \
             patch("src.ollama_runtime.time.sleep"):
            with OllamaDaemon() as runtime:
                assert runtime._we_started_daemon is True
                assert runtime._proc is fake_proc
            popen.assert_called_once()
            # Daemon should have been terminated cleanly on exit.
            assert fake_proc.terminate.called or fake_proc.send_signal.called
            fake_proc.wait.assert_called()

    def test_timeout_raises_and_kills_process(self):
        fake_proc = MagicMock()
        fake_proc.poll.return_value = None  # still alive while we wait

        with patch("src.ollama_runtime.find_binary"), \
             patch("src.ollama_runtime.is_daemon_running", return_value=False), \
             patch("src.ollama_runtime.ensure_models"), \
             patch("src.ollama_runtime.subprocess.Popen", return_value=fake_proc), \
             patch("src.ollama_runtime.time.sleep"), \
             patch("src.ollama_runtime.time.monotonic", side_effect=[0.0, DAEMON_START_TIMEOUT_S + 1]):
            with pytest.raises(OllamaDaemonStartTimeoutError):
                with OllamaDaemon(start_timeout_s=DAEMON_START_TIMEOUT_S):
                    pass
            # We tried to clean up the doomed process.
            assert fake_proc.terminate.called or fake_proc.send_signal.called

    def test_serve_exits_early_raises(self):
        fake_proc = MagicMock()
        fake_proc.poll.return_value = 1  # exited
        fake_proc.returncode = 1

        with patch("src.ollama_runtime.find_binary"), \
             patch("src.ollama_runtime.is_daemon_running", return_value=False), \
             patch("src.ollama_runtime.ensure_models"), \
             patch("src.ollama_runtime.subprocess.Popen", return_value=fake_proc), \
             patch("src.ollama_runtime.time.sleep"):
            with pytest.raises(OllamaDaemonStartTimeoutError) as exc:
                with OllamaDaemon():
                    pass
        assert "exit" in str(exc.value).lower() or "code 1" in str(exc.value)


class TestOllamaDaemonStopFallback:
    def test_kills_if_terminate_doesnt_take(self):
        fake_proc = MagicMock()
        # is_daemon_running starts False (so we spawn), then True (so
        # the spawn-wait succeeds).
        is_running = iter([False, True])
        # First wait() after terminate times out; second wait() (after
        # kill()) returns cleanly.
        fake_proc.poll.return_value = None
        fake_proc.wait.side_effect = [
            subprocess.TimeoutExpired(cmd="ollama serve", timeout=5),
            0,
        ]

        with patch("src.ollama_runtime.find_binary"), \
             patch(
                "src.ollama_runtime.is_daemon_running",
                side_effect=lambda *_a, **_k: next(is_running),
             ), \
             patch("src.ollama_runtime.ensure_models"), \
             patch("src.ollama_runtime.subprocess.Popen", return_value=fake_proc), \
             patch("src.ollama_runtime.time.sleep"):
            with OllamaDaemon():
                pass
        fake_proc.kill.assert_called_once()


class TestOllamaDaemonBinaryMissing:
    def test_enter_propagates_binary_missing(self):
        with patch(
            "src.ollama_runtime.shutil.which",
            return_value=None,
        ):
            with pytest.raises(OllamaBinaryMissingError):
                with OllamaDaemon():
                    pass
