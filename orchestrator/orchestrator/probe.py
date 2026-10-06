"""Capability probe: what can this machine actually run?

The orchestrator ships to players, so there is **no fixed hardware target**.
Everything is discovered at runtime and turned into a coarse *tier* (0-4) plus
the concrete numbers the planner needs.

Two rules from the design doc (exp. 0105) are enforced here:

1. **Never attribute VRAM per process.** Windows/WDDM returns ``[N/A]`` for
   every PID, so we only ever read *device* totals.
2. **Budget against the minimum free VRAM over a sampling window**, not an
   instantaneous reading, so a transient spike cannot cause a later OOM.

The probe is deliberately dependency-free: it shells out to ``nvidia-smi`` and
falls back to "no GPU" rather than importing a vendor library.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path

from orchestrator.paths import model_store_dir

#: VRAM bounds used to normalise the tier score (GB).
_VRAM_MIN_GB = 4.0
_VRAM_MAX_GB = 48.0
_RAM_MIN_GB = 8.0
_RAM_MAX_GB = 64.0
_DISK_MIN_GB = 10.0
_DISK_MAX_GB = 500.0

#: VRAM held back for the OS, the game and driver overhead (GB).
DEFAULT_RESERVE_GB = 3.0
#: Extra allowance so two footprints that exactly sum to the budget still fit.
FRAGMENTATION_SLACK = 0.12


def _clamp01(value: float) -> float:
    return max(0.0, min(1.0, value))


def _norm(value: float, low: float, high: float) -> float:
    if high <= low:
        return 0.0
    return _clamp01((value - low) / (high - low))


@dataclass(frozen=True)
class GpuInfo:
    """One physical GPU as reported by ``nvidia-smi``."""

    name: str
    total_gb: float
    used_gb: float
    driver: str = ""

    @property
    def free_gb(self) -> float:
        return max(0.0, self.total_gb - self.used_gb)


@dataclass
class ProbeReport:
    """The result of probing the host. Cheap to build, safe to log."""

    gpus: list[GpuInfo] = field(default_factory=list)
    ram_gb: float = 0.0
    free_disk_gb: float = 0.0
    tier: int = 0
    tier_score: float = 0.0
    #: Minimum free VRAM seen over the sampling window (GB).
    min_free_vram_gb: float = 0.0
    reserve_gb: float = DEFAULT_RESERVE_GB
    fragmentation_slack: float = FRAGMENTATION_SLACK
    probed_at: float = field(default_factory=time.time)

    @property
    def has_gpu(self) -> bool:
        return bool(self.gpus)

    @property
    def total_vram_gb(self) -> float:
        return sum(g.total_gb for g in self.gpus)

    @property
    def usable_vram_gb(self) -> float:
        """VRAM the orchestrator may plan against, after reserve and slack."""
        if not self.gpus:
            return 0.0
        budget = self.min_free_vram_gb - self.reserve_gb
        if budget <= 0:
            return 0.0
        return budget * (1.0 - self.fragmentation_slack)

    def fits(self, vram_gb: float, ram_gb: float = 0.0) -> bool:
        """Can a model with this peak footprint run here?"""
        if vram_gb <= 0.0:
            # CPU-only model: only RAM matters.
            return ram_gb <= self.ram_gb
        if not self.gpus:
            # No GPU: fall back to RAM if the model is small enough to offload.
            return ram_gb > 0.0 and ram_gb <= self.ram_gb
        return vram_gb <= self.usable_vram_gb

    def to_dict(self) -> dict:
        return {
            "gpus": [
                {
                    "name": g.name,
                    "total_gb": round(g.total_gb, 2),
                    "used_gb": round(g.used_gb, 2),
                    "free_gb": round(g.free_gb, 2),
                    "driver": g.driver,
                }
                for g in self.gpus
            ],
            "ram_gb": round(self.ram_gb, 2),
            "free_disk_gb": round(self.free_disk_gb, 2),
            "tier": self.tier,
            "tier_score": round(self.tier_score, 4),
            "min_free_vram_gb": round(self.min_free_vram_gb, 2),
            "usable_vram_gb": round(self.usable_vram_gb, 2),
            "reserve_gb": self.reserve_gb,
            "fragmentation_slack": self.fragmentation_slack,
        }


def _query_nvidia_smi() -> list[GpuInfo]:
    """Read GPU totals via ``nvidia-smi``. Returns [] when unavailable."""
    exe = shutil.which("nvidia-smi")
    if not exe:
        return []
    try:
        out = subprocess.run(
            [
                exe,
                "--query-gpu=name,memory.total,memory.used,driver_version",
                "--format=csv,noheader,nounits",
            ],
            capture_output=True,
            text=True,
            timeout=10,
            check=True,
        ).stdout
    except (subprocess.SubprocessError, OSError):
        return []

    gpus: list[GpuInfo] = []
    for line in out.strip().splitlines():
        parts = [p.strip() for p in line.split(",")]
        if len(parts) < 3:
            continue
        try:
            total_mib = float(parts[1])
            used_mib = float(parts[2])
        except ValueError:
            continue
        gpus.append(
            GpuInfo(
                name=parts[0],
                total_gb=total_mib / 1024.0,
                used_gb=used_mib / 1024.0,
                driver=parts[3] if len(parts) > 3 else "",
            )
        )
    return gpus


def _total_ram_gb() -> float:
    """Total physical RAM in GB, without third-party packages."""
    try:
        import ctypes

        class MemoryStatusEx(ctypes.Structure):
            _fields_ = [
                ("dwLength", ctypes.c_ulong),
                ("dwMemoryLoad", ctypes.c_ulong),
                ("ullTotalPhys", ctypes.c_ulonglong),
                ("ullAvailPhys", ctypes.c_ulonglong),
                ("ullTotalPageFile", ctypes.c_ulonglong),
                ("ullAvailPageFile", ctypes.c_ulonglong),
                ("ullTotalVirtual", ctypes.c_ulonglong),
                ("ullAvailVirtual", ctypes.c_ulonglong),
                ("ullAvailExtendedVirtual", ctypes.c_ulonglong),
            ]

        status = MemoryStatusEx()
        status.dwLength = ctypes.sizeof(MemoryStatusEx)
        if ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(status)):
            return status.ullTotalPhys / (1024**3)
    except (AttributeError, OSError):
        pass
    # Portable fallback.
    try:
        pages = os.sysconf("SC_PHYS_PAGES")  # type: ignore[attr-defined]
        size = os.sysconf("SC_PAGE_SIZE")  # type: ignore[attr-defined]
        return (pages * size) / (1024**3)
    except (AttributeError, ValueError, OSError):
        return 0.0


def _free_disk_gb(path: Path) -> float:
    try:
        return shutil.disk_usage(path).free / (1024**3)
    except OSError:
        return 0.0


def _tier_from_score(score: float, vram_gb: float) -> int:
    """Map the host to a tier.

    The design doc describes the bands by **VRAM range**, so VRAM is the
    authoritative signal and ``score`` is reported alongside it for telemetry
    (it also lifts a CPU-only machine that has plenty of RAM).

    ============  ==================
    Tier          VRAM
    ============  ==================
    0             CPU only / < 6 GB
    1             6-10 GB
    2             10-16 GB
    3             16-32 GB
    4             32 GB+
    ============  ==================
    """
    if vram_gb >= 32.0:
        return 4
    if vram_gb >= 16.0:
        return 3
    if vram_gb >= 10.0:
        return 2
    if vram_gb >= 6.0:
        return 1
    # No (or tiny) GPU: a RAM-rich machine can still run small models on CPU.
    if score >= 0.30:
        return 1
    return 0


def probe(
    *,
    samples: int = 3,
    interval_s: float = 0.2,
    reserve_gb: float = DEFAULT_RESERVE_GB,
) -> ProbeReport:
    """Probe the host and return a :class:`ProbeReport`.

    ``samples``/``interval_s`` control the VRAM sampling window. The minimum
    free VRAM across the window is what the budget is built from.
    """
    gpus = _query_nvidia_smi()
    min_free = 0.0
    if gpus:
        readings: list[float] = []
        for i in range(max(1, samples)):
            current = _query_nvidia_smi()
            if current:
                readings.append(sum(g.free_gb for g in current))
            if i < samples - 1:
                time.sleep(interval_s)
        min_free = min(readings) if readings else sum(g.free_gb for g in gpus)

    ram_gb = _total_ram_gb()
    disk_gb = _free_disk_gb(model_store_dir())

    total_vram = sum(g.total_gb for g in gpus)
    score = (
        0.70 * _norm(total_vram, _VRAM_MIN_GB, _VRAM_MAX_GB)
        + 0.25 * _norm(ram_gb, _RAM_MIN_GB, _RAM_MAX_GB)
        + 0.05 * _norm(disk_gb, _DISK_MIN_GB, _DISK_MAX_GB)
    )

    return ProbeReport(
        gpus=gpus,
        ram_gb=ram_gb,
        free_disk_gb=disk_gb,
        tier=_tier_from_score(score, total_vram),
        tier_score=score,
        min_free_vram_gb=min_free,
        reserve_gb=reserve_gb,
    )
