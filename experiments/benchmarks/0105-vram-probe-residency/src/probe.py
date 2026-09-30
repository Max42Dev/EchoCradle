#!/usr/bin/env python3
"""VRAM probe for EchoCradle (experiment 0105).

Measures total / used / free VRAM and, where the driver exposes it, per-process
VRAM. It then reports a conservative ``usable_vram`` budget that the scheduler
can divide across co-resident models.

The script runs with the standard library alone. If ``pynvml`` (pip package
``nvidia-ml-py``; it still imports as ``pynvml``) is installed it is preferred,
because it is faster and returns structured data. When it is missing the script
falls back to parsing ``nvidia-smi`` output.

Usage
-----
    python probe.py
    python probe.py --reserve-gb 3 --samples 10 --interval 0.5
    python probe.py --models 2            # per-model budget for N models
"""

from __future__ import annotations

import argparse
import csv
import io
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass, field
from statistics import mean
from typing import Optional

MIB_PER_GIB = 1024.0
# Idle headroom the OS, the compositor (dwm/DCV) and Unity need on the L4.
DEFAULT_RESERVE_GIB = 3.0


@dataclass
class ProcessUsage:
    """A single GPU compute process."""

    pid: int
    name: str
    used_mib: Optional[float]  # None when the driver reports "N/A"


@dataclass
class GpuSnapshot:
    """A point-in-time view of one GPU."""

    backend: str
    name: str
    total_mib: float
    used_mib: float
    free_mib: float
    processes: list[ProcessUsage] = field(default_factory=list)


# --------------------------------------------------------------------------- #
# Backends
# --------------------------------------------------------------------------- #
class NvmlBackend:
    """Preferred backend: NVML via the ``nvidia-ml-py`` / ``pynvml`` module."""

    backend_id = "nvml"

    def __init__(self) -> None:
        import pynvml  # noqa: PLC0415 - optional dependency, imported on use

        self._nvml = pynvml
        pynvml.nvmlInit()
        self._handle = pynvml.nvmlDeviceGetHandleByIndex(0)

    def _processes(self) -> list[ProcessUsage]:
        nvml = self._nvml
        procs: list[ProcessUsage] = []
        # On Windows/WDDM this call commonly raises NotSupported or returns an
        # empty list: per-process attribution is simply not exposed. We treat
        # that as "unknown", never as an error.
        try:
            raw = nvml.nvmlDeviceGetComputeRunningProcesses(self._handle)
        except Exception:  # noqa: BLE001 - any NVML failure means "unsupported"
            return procs
        for entry in raw:
            used = getattr(entry, "usedGpuMemory", None)
            # 0xFFFFFFFFFFFFFFFF / very large sentinels mean "N/A" in NVML.
            if used is None or used >= 2**63:
                used_mib: Optional[float] = None
            else:
                used_mib = used / (1024 * 1024)
            procs.append(
                ProcessUsage(pid=int(entry.pid), name=f"pid {entry.pid}", used_mib=used_mib)
            )
        return procs

    def snapshot(self) -> GpuSnapshot:
        nvml = self._nvml
        name = nvml.nvmlDeviceGetName(self._handle)
        if isinstance(name, bytes):  # older bindings return bytes
            name = name.decode("utf-8", "replace")
        mem = nvml.nvmlDeviceGetMemoryInfo(self._handle)
        return GpuSnapshot(
            backend=self.backend_id,
            name=str(name),
            total_mib=mem.total / (1024 * 1024),
            used_mib=mem.used / (1024 * 1024),
            free_mib=mem.free / (1024 * 1024),
            processes=self._processes(),
        )


class SmiBackend:
    """Fallback backend: parse ``nvidia-smi`` CSV output (stdlib only)."""

    backend_id = "nvidia-smi"

    def _run(self, args: list[str]) -> str:
        exe = shutil.which("nvidia-smi") or "nvidia-smi"
        completed = subprocess.run(  # noqa: S603 - fixed, known binary
            [exe, *args],
            capture_output=True,
            text=True,
            check=True,
        )
        return completed.stdout

    @staticmethod
    def _to_float(value: str) -> Optional[float]:
        value = value.strip()
        if not value or value.upper() in {"N/A", "NA", "[N/A]"}:
            return None
        return float(value)

    def _processes(self) -> list[ProcessUsage]:
        try:
            out = self._run(
                ["--query-compute-apps=pid,process_name,used_memory", "--format=csv"]
            )
        except (subprocess.CalledProcessError, FileNotFoundError):
            return []
        procs: list[ProcessUsage] = []
        # nvidia-smi emits "pid, process_name, used_gpu_memory [MiB]" with a
        # space after each comma; skipinitialspace keeps the header keys clean.
        reader = csv.DictReader(io.StringIO(out), skipinitialspace=True)
        for row in reader:
            try:
                pid = int(row["pid"])
            except (KeyError, TypeError, ValueError):
                continue
            name = (row.get("process_name") or "").strip()
            used = self._to_float(row.get("used_gpu_memory [MiB]", ""))
            procs.append(ProcessUsage(pid=pid, name=name, used_mib=used))
        return procs

    def snapshot(self) -> GpuSnapshot:
        out = self._run(
            [
                "--query-gpu=name,memory.total,memory.used,memory.free",
                "--format=csv,nounits,noheader",
            ]
        )
        line = out.strip().splitlines()[0]
        name, total, used, free = (part.strip() for part in line.split(",", 3))
        return GpuSnapshot(
            backend=self.backend_id,
            name=name,
            total_mib=float(total),
            used_mib=float(used),
            free_mib=float(free),
            processes=self._processes(),
        )


def make_backend(force_smi: bool) -> object:
    """Return an NVML backend when possible, else the nvidia-smi fallback."""
    if not force_smi:
        try:
            return NvmlBackend()
        except Exception as exc:  # noqa: BLE001 - missing module or no driver
            # Informational, not an error: keep it on stdout so callers that
            # treat stderr as failure (e.g. PowerShell) don't see a bad exit.
            print(f"[probe] NVML unavailable ({exc.__class__.__name__}: {exc}); "
                  f"falling back to nvidia-smi")
    return SmiBackend()


# --------------------------------------------------------------------------- #
# Reporting
# --------------------------------------------------------------------------- #
def _mib_to_gib(value: float) -> float:
    return value / MIB_PER_GIB


def print_header(snap: GpuSnapshot) -> None:
    print(f"GPU              : {snap.name}")
    print(f"Backend          : {snap.backend}")
    print(f"Total VRAM       : {snap.total_mib:8.0f} MiB "
          f"({_mib_to_gib(snap.total_mib):5.2f} GiB)")
    print(f"Used  VRAM       : {snap.used_mib:8.0f} MiB "
          f"({_mib_to_gib(snap.used_mib):5.2f} GiB)")
    print(f"Free  VRAM       : {snap.free_mib:8.0f} MiB "
          f"({_mib_to_gib(snap.free_mib):5.2f} GiB)")


def print_processes(procs: list[ProcessUsage]) -> None:
    print("\nCompute processes:")
    if not procs:
        print("  (none reported)")
        return
    attributed = [p for p in procs if p.used_mib is not None]
    for proc in sorted(procs, key=lambda p: (p.used_mib is None, -(p.used_mib or 0))):
        mem = f"{proc.used_mib:8.0f} MiB" if proc.used_mib is not None else "     N/A  "
        print(f"  {proc.pid:>7}  {mem}  {proc.name}")
    if not attributed:
        print("\n  NOTE: every process reports N/A. On Windows/WDDM the driver")
        print("        does not expose per-process VRAM, so footprints must be")
        print("        measured as a *delta* of total used, not by attribution.")


def print_budget(free_mib: float, reserve_gib: float, model_counts: list[int]) -> None:
    usable_mib = max(0.0, free_mib - reserve_gib * MIB_PER_GIB)
    print(f"\nReserve          : {reserve_gib:.2f} GiB "
          f"({reserve_gib * MIB_PER_GIB:.0f} MiB)")
    print(f"Usable VRAM      : {usable_mib:.0f} MiB "
          f"({_mib_to_gib(usable_mib):.2f} GiB)  "
          f"= free - reserve")
    print("\nCo-residency budget (usable_vram / N):")
    for n in model_counts:
        per = usable_mib / n
        verdict = "ok" if per > 0 else "no headroom"
        print(f"  N={n}: {per:8.0f} MiB / model ({_mib_to_gib(per):.2f} GiB)  [{verdict}]")
    if usable_mib <= 0:
        print("\n  WARNING: free VRAM is below the reserve. Serialise model loads,")
        print("           raise the reserve only after freeing memory, or unload a model.")


# --------------------------------------------------------------------------- #
# Main
# --------------------------------------------------------------------------- #
def parse_args(argv: Optional[list[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Probe GPU VRAM and estimate a safe usable-VRAM budget.",
    )
    parser.add_argument(
        "--reserve-gb",
        type=float,
        default=DEFAULT_RESERVE_GIB,
        help="GiB kept for OS/desktop/Unity safety margin (default: %(default)s)",
    )
    parser.add_argument(
        "--interval",
        type=float,
        default=1.0,
        help="Seconds between samples (default: %(default)s)",
    )
    parser.add_argument(
        "--samples",
        type=int,
        default=1,
        help="Number of samples to take (default: %(default)s)",
    )
    parser.add_argument(
        "--models",
        type=int,
        default=2,
        help="Largest co-residency count to report the per-model budget for",
    )
    parser.add_argument(
        "--smi",
        action="store_true",
        help="Force the nvidia-smi fallback even when pynvml is installed",
    )
    return parser.parse_args(argv)


def main(argv: Optional[list[str]] = None) -> int:
    args = parse_args(argv)
    samples = max(1, args.samples)
    backend = make_backend(force_smi=args.smi)

    snapshots: list[GpuSnapshot] = []
    try:
        for i in range(samples):
            if i:
                time.sleep(max(0.0, args.interval))
            snapshots.append(backend.snapshot())  # type: ignore[attr-defined]
    except FileNotFoundError:
        print("[probe] nvidia-smi not found and NVML unavailable: no GPU visible.",
              file=sys.stderr)
        return 2
    except Exception as exc:  # noqa: BLE001 - report and stop cleanly
        print(f"[probe] failed to read GPU state: {exc}", file=sys.stderr)
        return 1

    last = snapshots[-1]
    print_header(last)
    print_processes(last.processes)

    if samples > 1:
        used_vals = [s.used_mib for s in snapshots]
        free_vals = [s.free_mib for s in snapshots]
        print(f"\nSampling         : {samples} samples @ {args.interval:g}s")
        print(f"Used  (min/avg/max): {min(used_vals):.0f} / "
              f"{mean(used_vals):.0f} / {max(used_vals):.0f} MiB")
        print(f"Free  (min/avg/max): {min(free_vals):.0f} / "
              f"{mean(free_vals):.0f} / {max(free_vals):.0f} MiB")
        print("  -> the scheduler should budget against the MINIMUM free value.")

    # Budget against the worst (lowest-free) sample, which is the safe choice.
    worst_free = min(s.free_mib for s in snapshots)
    counts = sorted({1, 2, 3, args.models} if args.models > 3 else {1, 2, args.models})
    print_budget(worst_free, args.reserve_gb, counts)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())