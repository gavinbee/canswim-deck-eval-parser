"""Tests for src/gpu_detect.py.

All platform probes are mocked — these tests run identically on any OS
and never shell out. The aim is to pin:

* tier boundaries (the most important thing, since the model selection
  for an entire run rides on getting these right)
* nvidia-smi output parsing (the most common real-world case)
* Apple Silicon and Intel-Mac fallbacks
* the dispatch order in detect_gpus()
* the human-readable describe() summary
"""
from __future__ import annotations

import subprocess
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from src import gpu_detect
from src.gpu_detect import (
    GpuInfo,
    Tier,
    describe,
    detect_gpus,
    pick_tier,
    recommended_models,
)


# ---------------------------------------------------------------------------
# pick_tier — boundaries
# ---------------------------------------------------------------------------


class TestTierBoundariesFromInt:
    """The whole point of this module — make sure the right model is
    chosen at each tier boundary."""

    @pytest.mark.parametrize("free_mb, expected", [
        # Above 48 GB → 72b power-user tier.
        (49_000, Tier.XLARGE),
        (96_000, Tier.XLARGE),
        # 48 GB exactly: just clears the threshold.
        (48_000, Tier.XLARGE),
        # 32 GB sits between the LARGE and XLARGE thresholds → LARGE.
        (32_000, Tier.LARGE),
        # 24 GB (RTX 3090/4090 ballpark) → LARGE.
        (24_000, Tier.LARGE),
        # Exactly 20 GB: just clears LARGE.
        (20_000, Tier.LARGE),
        # 19_999 falls back to SMALL.
        (19_999, Tier.SMALL),
        # 12 GB (typical mid-range card) → SMALL (the safe default).
        (12_000, Tier.SMALL),
        # 8 GB (RTX 3070 / 4060 Ti 8 GB ballpark) → SMALL.
        (8_000,  Tier.SMALL),
        # 6 GB exactly: just clears SMALL.
        (6_000,  Tier.SMALL),
        # Just below 6 GB → CPU_OR_TINY.
        (5_999,  Tier.CPU_OR_TINY),
        (4_000,  Tier.CPU_OR_TINY),
        (0,      Tier.CPU_OR_TINY),
    ])
    def test_thresholds(self, free_mb: int, expected: Tier):
        assert pick_tier(free_mb) == expected


class TestPickTierFromGpuInfos:
    def test_uses_max_free_across_gpus(self):
        # When two cards report different free amounts, the one with
        # more free VRAM wins — Ollama uses a single GPU at a time.
        gpus = [
            GpuInfo(name="A", total_mb=12_288, free_mb=2_000),
            GpuInfo(name="B", total_mb=24_576, free_mb=22_000),
        ]
        assert pick_tier(gpus) == Tier.LARGE

    def test_empty_list_falls_back_to_cpu(self):
        assert pick_tier([]) == Tier.CPU_OR_TINY

    def test_none_falls_back_to_cpu(self):
        assert pick_tier(None) == Tier.CPU_OR_TINY


class TestRecommendedModels:
    def test_each_tier_has_a_model_pair(self):
        for tier in Tier:
            vision, edit = recommended_models(tier)
            assert vision.startswith("qwen2.5vl:")
            assert edit.startswith("qwen2.5:")

    def test_small_tier_is_7b(self):
        # The default tier maps to 7B vision + 7B edit.
        assert recommended_models(Tier.SMALL) == ("qwen2.5vl:7b", "qwen2.5:7b")

    def test_large_tier_keeps_edit_at_7b(self):
        # Bigger vision model, but the edit-loop model stays at 7b —
        # there's no useful benefit to a larger edit model for this
        # use case per docs/models.md.
        vision, edit = recommended_models(Tier.LARGE)
        assert vision == "qwen2.5vl:32b"
        assert edit == "qwen2.5:7b"

    def test_cpu_tier_uses_3b_for_both(self):
        assert recommended_models(Tier.CPU_OR_TINY) == ("qwen2.5vl:3b", "qwen2.5:3b")


# ---------------------------------------------------------------------------
# nvidia-smi parsing
# ---------------------------------------------------------------------------


class TestParseNvidiaSmi:
    def test_single_card(self):
        # Real-world line for the user's RTX 3070.
        out = "NVIDIA GeForce RTX 3070, 6240, 8192\n"
        gpus = list(gpu_detect._parse_nvidia_smi(out))
        assert gpus == [GpuInfo(name="NVIDIA GeForce RTX 3070",
                                free_mb=6240, total_mb=8192)]

    def test_multiple_cards(self):
        out = (
            "NVIDIA RTX 4090, 23800, 24564\n"
            "NVIDIA RTX 3090, 22000, 24576\n"
        )
        gpus = list(gpu_detect._parse_nvidia_smi(out))
        assert len(gpus) == 2
        assert gpus[0].free_mb == 23_800
        assert gpus[1].free_mb == 22_000

    def test_blank_lines_skipped(self):
        out = "\n\nNVIDIA RTX 3070, 6240, 8192\n\n"
        gpus = list(gpu_detect._parse_nvidia_smi(out))
        assert len(gpus) == 1

    def test_garbage_rows_skipped(self):
        # A line with non-numeric values — driver-output drift. Skip
        # but don't crash.
        out = "Some Header Row\nNVIDIA RTX 3070, abc, def\nNVIDIA RTX 3070, 6240, 8192\n"
        gpus = list(gpu_detect._parse_nvidia_smi(out))
        assert len(gpus) == 1
        assert gpus[0].free_mb == 6240

    def test_too_few_columns_skipped(self):
        out = "NVIDIA RTX 3070, 6240\n"
        gpus = list(gpu_detect._parse_nvidia_smi(out))
        assert gpus == []


# ---------------------------------------------------------------------------
# _detect_nvidia — orchestration
# ---------------------------------------------------------------------------


class TestDetectNvidia:
    def test_returns_empty_when_binary_missing(self):
        with patch("src.gpu_detect.shutil.which", return_value=None):
            assert gpu_detect._detect_nvidia() == []

    def test_parses_subprocess_output(self):
        fake_result = SimpleNamespace(
            returncode=0,
            stdout="NVIDIA RTX 3070, 6240, 8192\n",
            stderr="",
        )
        with patch("src.gpu_detect.shutil.which", return_value="/u/b/nvidia-smi"), \
             patch("src.gpu_detect.subprocess.run", return_value=fake_result):
            gpus = gpu_detect._detect_nvidia()
        assert len(gpus) == 1
        assert gpus[0].free_mb == 6240

    def test_nonzero_returncode_yields_empty(self):
        fake_result = SimpleNamespace(returncode=2, stdout="", stderr="oops")
        with patch("src.gpu_detect.shutil.which", return_value="/u/b/nvidia-smi"), \
             patch("src.gpu_detect.subprocess.run", return_value=fake_result):
            assert gpu_detect._detect_nvidia() == []

    def test_timeout_yields_empty(self):
        with patch("src.gpu_detect.shutil.which", return_value="/u/b/nvidia-smi"), \
             patch(
                "src.gpu_detect.subprocess.run",
                side_effect=subprocess.TimeoutExpired(cmd="nvidia-smi", timeout=5),
             ):
            assert gpu_detect._detect_nvidia() == []

    def test_oserror_yields_empty(self):
        # e.g. the binary disappears between which() and run().
        with patch("src.gpu_detect.shutil.which", return_value="/u/b/nvidia-smi"), \
             patch("src.gpu_detect.subprocess.run", side_effect=OSError("gone")):
            assert gpu_detect._detect_nvidia() == []


# ---------------------------------------------------------------------------
# Apple Silicon probe
# ---------------------------------------------------------------------------


class TestDetectAppleSilicon:
    def test_parses_sysctl_memsize(self):
        # 16 GB system → 16 * 1024 MB total → 70% = 11_468 MB usable.
        fake = SimpleNamespace(
            returncode=0, stdout=f"{16 * 1024 * 1024 * 1024}\n", stderr="",
        )
        with patch("src.gpu_detect.shutil.which", return_value="/usr/sbin/sysctl"), \
             patch("src.gpu_detect.subprocess.run", return_value=fake):
            gpus = gpu_detect._detect_apple_silicon()
        assert len(gpus) == 1
        assert gpus[0].total_mb == 16 * 1024
        assert gpus[0].free_mb == int(16 * 1024 * 0.70)

    def test_sysctl_missing_returns_empty(self):
        with patch("src.gpu_detect.shutil.which", return_value=None):
            assert gpu_detect._detect_apple_silicon() == []

    def test_nonzero_returncode_returns_empty(self):
        fake = SimpleNamespace(returncode=1, stdout="", stderr="x")
        with patch("src.gpu_detect.shutil.which", return_value="/usr/sbin/sysctl"), \
             patch("src.gpu_detect.subprocess.run", return_value=fake):
            assert gpu_detect._detect_apple_silicon() == []


# ---------------------------------------------------------------------------
# Intel Mac probe
# ---------------------------------------------------------------------------


class TestParseSpdisplays:
    def test_parses_mb_size(self):
        out = """
Graphics/Displays:
    Radeon Pro 5500M:
      Chipset Model: AMD Radeon Pro 5500M
      VRAM (Total): 4096 MB
"""
        gpus = list(gpu_detect._parse_spdisplays(out))
        assert len(gpus) == 1
        assert gpus[0].name == "AMD Radeon Pro 5500M"
        assert gpus[0].total_mb == 4096

    def test_parses_gb_size(self):
        out = """
      Chipset Model: NVIDIA GeForce GTX 1080
      VRAM (Total): 8 GB
"""
        gpus = list(gpu_detect._parse_spdisplays(out))
        assert len(gpus) == 1
        assert gpus[0].total_mb == 8 * 1024


# ---------------------------------------------------------------------------
# detect_gpus dispatch order
# ---------------------------------------------------------------------------


class TestDetectGpusDispatch:
    def test_nvidia_wins_when_present(self):
        nvidia_gpus = [GpuInfo(name="NVIDIA", total_mb=8192, free_mb=6240)]
        with patch("src.gpu_detect._detect_nvidia", return_value=nvidia_gpus), \
             patch("src.gpu_detect._is_apple_silicon", return_value=False), \
             patch("src.gpu_detect._detect_intel_mac") as intel_probe:
            assert detect_gpus() == nvidia_gpus
            intel_probe.assert_not_called()

    def test_falls_through_to_apple_silicon(self):
        apple_gpus = [GpuInfo(name="Apple Silicon (arm)", total_mb=16384, free_mb=11468)]
        with patch("src.gpu_detect._detect_nvidia", return_value=[]), \
             patch("src.gpu_detect._is_apple_silicon", return_value=True), \
             patch("src.gpu_detect._detect_apple_silicon", return_value=apple_gpus), \
             patch("src.gpu_detect._detect_intel_mac") as intel_probe:
            assert detect_gpus() == apple_gpus
            intel_probe.assert_not_called()

    def test_falls_through_to_intel_mac(self):
        mac_gpus = [GpuInfo(name="AMD", total_mb=4096, free_mb=4096)]
        with patch("src.gpu_detect._detect_nvidia", return_value=[]), \
             patch("src.gpu_detect._is_apple_silicon", return_value=False), \
             patch("src.gpu_detect.platform.system", return_value="Darwin"), \
             patch("src.gpu_detect._detect_intel_mac", return_value=mac_gpus):
            assert detect_gpus() == mac_gpus

    def test_nothing_detected_returns_empty(self):
        with patch("src.gpu_detect._detect_nvidia", return_value=[]), \
             patch("src.gpu_detect._is_apple_silicon", return_value=False), \
             patch("src.gpu_detect.platform.system", return_value="Linux"):
            assert detect_gpus() == []


# ---------------------------------------------------------------------------
# describe()
# ---------------------------------------------------------------------------


class TestDescribe:
    def test_no_gpu_mentions_cpu_fallback(self):
        msg = describe([], Tier.CPU_OR_TINY)
        assert "CPU" in msg
        assert "qwen2.5vl:3b" in msg
        assert "--vision-model" in msg

    def test_with_gpu_mentions_name_and_free(self):
        gpus = [GpuInfo(name="NVIDIA RTX 3070", total_mb=8192, free_mb=6240)]
        msg = describe(gpus, Tier.SMALL)
        assert "RTX 3070" in msg
        assert "6240" in msg
        assert "8192" in msg
        assert "qwen2.5vl:7b" in msg
        assert "7b" in msg

    def test_picks_the_best_gpu_for_summary(self):
        gpus = [
            GpuInfo(name="Card A", total_mb=12_288, free_mb=2_000),
            GpuInfo(name="Card B", total_mb=24_576, free_mb=22_000),
        ]
        msg = describe(gpus, Tier.LARGE)
        # Card B (more free) is what we summarise.
        assert "Card B" in msg
        assert "22000" in msg
