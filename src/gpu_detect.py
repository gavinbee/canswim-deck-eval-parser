"""Detect available GPU VRAM and pick the right Qwen2.5-VL tier.

We try detection in this order:

1. **NVIDIA** via ``nvidia-smi``. Covers the vast majority of consumer
   and workstation GPUs on Windows and Linux. We query
   ``name,memory.free,memory.total`` and parse each GPU on a line.
2. **Apple Silicon** via ``sysctl hw.memsize``. Unified memory means
   the M-series chips effectively expose all system RAM as VRAM. We
   take 70% of total as a conservative usable budget (the OS, app
   processes, and Ollama's own working set occupy the rest).
3. **Intel Mac** via ``system_profiler SPDisplaysDataType``. Best-effort;
   covers discrete AMD and older NVIDIA cards. Apple Silicon is
   handled by #2 above, not here.

If nothing answers we return an empty list, and ``pick_tier`` falls back
to the smallest tier (which Ollama runs on CPU automatically when no
GPU is available).

Tier boundaries are sourced from ``docs/models.md``:

* ≥ 48 GB free → ``qwen2.5vl:72b`` (Q4)
* ≥ 20 GB free → ``qwen2.5vl:32b`` (Q4_K_M)
* ≥ 6 GB free  → ``qwen2.5vl:7b``  (Q4) — the safe default
* otherwise   → ``qwen2.5vl:3b``  (Q4) — also the CPU fallback

The edit-loop model is always ``qwen2.5:7b`` for tiers that can run it,
or ``qwen2.5:3b`` for the smallest tier.
"""
from __future__ import annotations

import logging
import platform
import re
import shutil
import subprocess
from dataclasses import dataclass
from enum import Enum
from typing import Iterable, Optional

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Types
# ---------------------------------------------------------------------------


class Tier(Enum):
    """Model-tier identifiers — see docs/models.md for what each runs."""

    CPU_OR_TINY = "3b"   # No GPU or <6 GB free.
    SMALL       = "7b"   # 6-20 GB free. Safe default.
    LARGE       = "32b"  # 20-48 GB free.
    XLARGE      = "72b"  # 48+ GB free. Power-user, not recommended for v1.


# (vision_model_tag, edit_model_tag) per tier.
TIER_MODELS: dict[Tier, tuple[str, str]] = {
    Tier.CPU_OR_TINY: ("qwen2.5vl:3b",  "qwen2.5:3b"),
    Tier.SMALL:       ("qwen2.5vl:7b",  "qwen2.5:7b"),
    Tier.LARGE:       ("qwen2.5vl:32b", "qwen2.5:7b"),
    Tier.XLARGE:      ("qwen2.5vl:72b", "qwen2.5:7b"),
}


# Ordered high-to-low so we can return the first tier the free-VRAM
# value clears. Values are MB.
_TIER_THRESHOLDS_MB: tuple[tuple[int, Tier], ...] = (
    (48_000, Tier.XLARGE),
    (20_000, Tier.LARGE),
    (6_000,  Tier.SMALL),
)


@dataclass(frozen=True)
class GpuInfo:
    """Describes one detected GPU.

    ``free_mb`` is the value we actually choose tiers on; ``total_mb``
    is reported in logs so the user can see what their card is
    nominally capable of even when something else is using it.
    """

    name: str
    total_mb: int
    free_mb: int


# ---------------------------------------------------------------------------
# Public surface
# ---------------------------------------------------------------------------


def detect_gpus() -> list[GpuInfo]:
    """Return every GPU we can detect on this host.

    Empty list means "we couldn't find one" — either there really isn't
    a GPU, or none of our probes work on this platform. Either way the
    tier picker falls back to the smallest model.
    """
    nvidia = _detect_nvidia()
    if nvidia:
        return nvidia
    if _is_apple_silicon():
        apple = _detect_apple_silicon()
        if apple:
            return apple
    if platform.system() == "Darwin":
        mac = _detect_intel_mac()
        if mac:
            return mac
    return []


def pick_tier(gpus_or_vram: Iterable[GpuInfo] | int | None) -> Tier:
    """Choose the model tier for the given hardware.

    Accepts either a list of ``GpuInfo`` (in which case we use the
    GPU with the most free VRAM) or an explicit free-VRAM integer in
    MB. ``None`` returns :class:`Tier.CPU_OR_TINY`, which Ollama will
    happily run on CPU.
    """
    if gpus_or_vram is None:
        return Tier.CPU_OR_TINY
    if isinstance(gpus_or_vram, int):
        free_mb = gpus_or_vram
    else:
        gpus = list(gpus_or_vram)
        if not gpus:
            return Tier.CPU_OR_TINY
        # Ollama uses one GPU at a time; pick the one with the most
        # free VRAM rather than summing across cards.
        free_mb = max(g.free_mb for g in gpus)
    for threshold, tier in _TIER_THRESHOLDS_MB:
        if free_mb >= threshold:
            return tier
    return Tier.CPU_OR_TINY


def recommended_models(tier: Tier) -> tuple[str, str]:
    """Return ``(vision_tag, edit_tag)`` for the given tier."""
    return TIER_MODELS[tier]


def describe(gpus: list[GpuInfo], tier: Tier) -> str:
    """One-line summary suitable for the CLI startup log.

    Used by the CLI when ``--vision-model`` isn't set, so the user
    sees which tier was auto-picked and why.
    """
    vision, _ = recommended_models(tier)
    if not gpus:
        return (
            f"No GPU detected. Using {vision} on CPU "
            "(slow but functional; override with --vision-model)."
        )
    best = max(gpus, key=lambda g: g.free_mb)
    return (
        f"Detected {best.name} with {best.free_mb} MB free "
        f"({best.total_mb} MB total). "
        f"Using {vision} ({tier.value} tier)."
    )


# ---------------------------------------------------------------------------
# NVIDIA probe
# ---------------------------------------------------------------------------


_NVIDIA_QUERY = (
    "nvidia-smi",
    "--query-gpu=name,memory.free,memory.total",
    "--format=csv,noheader,nounits",
)


def _detect_nvidia() -> list[GpuInfo]:
    """Probe via ``nvidia-smi``. Empty list if it's not on PATH or fails."""
    if shutil.which("nvidia-smi") is None:
        return []
    try:
        result = subprocess.run(
            _NVIDIA_QUERY,
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        log.debug("nvidia-smi failed: %s", exc)
        return []
    if result.returncode != 0:
        log.debug("nvidia-smi exited with %d: %s",
                  result.returncode, result.stderr.strip())
        return []
    return list(_parse_nvidia_smi(result.stdout))


def _parse_nvidia_smi(stdout: str) -> Iterable[GpuInfo]:
    """Parse the CSV output of nvidia-smi's --query-gpu form.

    Each line looks like::

        NVIDIA GeForce RTX 3070, 6240, 8192

    Tolerates blank lines and stray whitespace; skips rows that don't
    parse to integers (which would indicate driver-output drift).
    """
    for raw in stdout.splitlines():
        line = raw.strip()
        if not line:
            continue
        parts = [p.strip() for p in line.split(",")]
        if len(parts) < 3:
            continue
        name, free_str, total_str = parts[0], parts[1], parts[2]
        try:
            free_mb = int(free_str)
            total_mb = int(total_str)
        except ValueError:
            log.debug("Skipping unparseable nvidia-smi row: %r", line)
            continue
        yield GpuInfo(name=name, total_mb=total_mb, free_mb=free_mb)


# ---------------------------------------------------------------------------
# Apple Silicon probe
# ---------------------------------------------------------------------------


def _is_apple_silicon() -> bool:
    """True on M-series Macs."""
    return platform.system() == "Darwin" and platform.machine().lower() in {
        "arm64", "aarch64",
    }


def _detect_apple_silicon() -> list[GpuInfo]:
    """Use total system RAM (× a usable fraction) as the available budget.

    The unified-memory architecture means there's no distinct VRAM
    figure to read. 70% of installed RAM is a conservative ceiling for
    what the GPU can actually use without starving the OS, app
    processes, and Ollama's working set.
    """
    if shutil.which("sysctl") is None:
        return []
    try:
        result = subprocess.run(
            ("sysctl", "-n", "hw.memsize"),
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        log.debug("sysctl hw.memsize failed: %s", exc)
        return []
    if result.returncode != 0:
        return []
    try:
        total_bytes = int(result.stdout.strip())
    except ValueError:
        return []
    total_mb = total_bytes // (1024 * 1024)
    usable_mb = int(total_mb * 0.70)
    name = f"Apple Silicon ({platform.processor() or 'unknown'})"
    return [GpuInfo(name=name, total_mb=total_mb, free_mb=usable_mb)]


# ---------------------------------------------------------------------------
# Intel-Mac discrete-GPU probe (best-effort)
# ---------------------------------------------------------------------------


_SPDISPLAYS_VRAM_RE = re.compile(
    r"VRAM\s*\(Total\)\s*:\s*(\d+)\s*(MB|GB)", re.IGNORECASE,
)
_SPDISPLAYS_NAME_RE = re.compile(r"^\s{6}Chipset Model:\s*(.+)$", re.MULTILINE)


def _detect_intel_mac() -> list[GpuInfo]:
    """Parse ``system_profiler SPDisplaysDataType`` for discrete VRAM.

    Best-effort: not all Macs report a VRAM (Total) line, and Apple's
    output format has changed across releases. Failure here is silent —
    callers fall back to CPU tier.
    """
    if shutil.which("system_profiler") is None:
        return []
    try:
        result = subprocess.run(
            ("system_profiler", "SPDisplaysDataType"),
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        log.debug("system_profiler failed: %s", exc)
        return []
    if result.returncode != 0:
        return []
    return list(_parse_spdisplays(result.stdout))


def _parse_spdisplays(stdout: str) -> Iterable[GpuInfo]:
    names = _SPDISPLAYS_NAME_RE.findall(stdout)
    matches = _SPDISPLAYS_VRAM_RE.findall(stdout)
    for i, (size_str, unit) in enumerate(matches):
        size = int(size_str)
        if unit.upper() == "GB":
            size *= 1024
        name = names[i] if i < len(names) else "Unknown GPU"
        # We don't have free-vs-total info from system_profiler — assume
        # the card is mostly free at startup.
        yield GpuInfo(name=name, total_mb=size, free_mb=size)
