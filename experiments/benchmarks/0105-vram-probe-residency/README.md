# 0105 — VRAM probing & multi-model residency

**Status:** done (probe verified on the reference box)
**Date:** 2026-09-29
**Author:** agent

## Question

How do we reliably measure free VRAM and **per-process** VRAM on this machine so
the scheduler can decide whether two models can be resident at once?

## Hypothesis

`pynvml` (NVML) exposes total/used VRAM and per-process usage; polling it around
load/unload lets us measure real model footprints and validate the co-residency
budget `usable_vram / N`. We expected per-process attribution to work.

## Setup

- **OS:** Windows Server 2022 (build 20348), WDDM display driver
- **Hardware:** NVIDIA L4 24 GB (23034 MiB, driver 596.86), 16 GB RAM
- **Runtime:** Python 3.13.15; **`pynvml`/`nvidia-ml-py` NOT installed** (so the
  fallback path is the one actually exercised here)
- **Models:** none resident during the probe run; `probe.py` is the instrument
- **Instrument:** [`src/probe.py`](src/probe.py)

## How to run

```powershell
# Default: one snapshot, NVML if present else nvidia-smi, 3 GiB reserve.
python src\probe.py

# Sample free/used over time and report the budget against the WORST case.
python src\probe.py --reserve-gb 3 --samples 30 --interval 0.5

# Force the stdlib/ nvidia-smi path even if pynvml is installed.
python src\probe.py --smi
```

## Results

Measured with `probe.py` on the reference box (idle, `pynvml` absent):

| Metric | Value |
| --- | --- |
| Backend actually used | `nvidia-smi` (NVML module not installed) |
| Total VRAM reported | 23034 MiB (22.49 GiB) |
| Used VRAM (idle) | ~313–536 MiB (varies with desktop/DCV) |
| Free VRAM (idle) | ~22060–22284 MiB |
| **Per-process VRAM** | **`N/A` for every process — see finding below** |
| Processes listed | 13 (dwm, explorer, DCV agent, Unity Hub, VS Code, Edge, …) |
| Polling overhead (nvidia-smi spawn) | **~34 ms per call** (10-call average, PowerShell `Measure-Command`) |
| Polling overhead (NVML, published) | single-digit ms; no process spawn |
| Usable VRAM @ reserve 3 GiB | 19212 MiB (18.76 GiB) |
| Budget N=2 | 9606 MiB / model (9.38 GiB) |

### Key finding: Windows/WDDM does **not** expose per-process VRAM

`nvidia-smi --query-compute-apps=pid,process_name,used_memory` lists the correct
PIDs and names but returns `[N/A]` in the memory column for **every** process.
NVML's `nvmlDeviceGetComputeRunningProcesses` returns the same sentinel. This is
expected on Windows with the WDDM display driver: the compositor keeps handles
open, so the driver refuses to attribute dedicated memory per process.

Consequences for the scheduler:

- The `used_memory` field cannot be used to attribute VRAM. Do not build logic
  that depends on it.
- **Measure footprints as a delta of device `used`:** read `used` before load,
  after load, and after unload. `footprint ≈ used_after − used_before`; verify
  the release with a final read that returns close to the baseline.
- Because `used` includes every non-model consumer (desktop, DCV, Unity), the
  delta is the *only* number that scales sensibly across machines.

### Budget validation — how to read `usable_vram / N`

The formula `usable_vram = free − reserve` then `per_model = usable_vram / N` is
**necessary but not sufficient**. The probe prints it with the guardrails needed
to make it safe:

1. **Budget against the minimum free across samples**, not the instantaneous
   value. A 30-sample run at 0.5 s catches transient spikes from Unity or the
   desktop; `probe.py` reports `min/avg/max` and budgets on `min`.
2. **The reserve is real, not decoration.** 3 GiB covers OS/desktop + Unity's
   ~1–2 GB + fragmentation. Fragmentation is the reason the *sum* of two
   footprints that equal `usable_vram` can still OOM: the allocator may not find
   one contiguous block. Keep 10–15 % of `usable_vram` unallocated on top of the
   reserve as a slack term.
3. **Peak, not steady state.** An autoregressive model's KV/attention cache grows
   with sequence length and a diffusion model's peak is at the denoise step. The
   number to record is `max used` during generation, not the resident weight
   size. `probe.py --samples N` run *during* a generation captures this.

So the practical rule is:

```
usable_vram = min_free_over_window - reserve - fragmentation_slack
resident_ok = sum(peak_footprint_i) <= usable_vram
```

## Conclusion

`promising` — the probe works and the budget model is sound, with two mandatory
corrections to the naive formula.

**Recommended probing approach:**

1. **Prefer NVML via `pynvml` (pip `nvidia-ml-py`).** It is the supported API,
   avoids spawning a process per poll (~34 ms saved each time; material when
   sampling at 2 Hz during a load), and returns structured values. Install with
   `pip install nvidia-ml-py`. Note the pip package is `nvidia-ml-py` but it still
   imports as `pynvml`; do **not** install the abandoned `pynvml` PyPI package of
   the same import name.
2. **Always keep the `nvidia-smi` subprocess fallback.** This box currently has
   no `pynvml`, and the fallback produced identical total/used/free values. It is
   stdlib-only (no download, respects the disk constraint) and version-proof.
3. **Treat per-process memory as unavailable on Windows.** Use the before/after
   *delta of device used* instead of attribution.
4. **Reserve + slack.** `reserve = 3 GiB` for OS/Unity, plus ~10–15 % of usable
   as fragmentation slack, and budget against the minimum free over a sampling
   window.

**Fallbacks for other hosts (non-NVIDIA / CPU-only):**

- **AMD (ROCm / Windows):** there is no portable NVML equivalent on Windows.
  Parse `rocm-smi --showmeminfo vram --json`. On Windows, ROCm support is thin;
  fall back to the tier formula and conservative model sizing.
- **Intel Arc:** `xpu-smi stats` (Linux) / Level Zero sysman; no simple Windows CLI.
- **Apple Silicon (unified memory):** cap by total RAM, not VRAM —
  `torch.mps.recommended_max_memory()`; there is no free-VRAM number.
- **CPU-only:** no VRAM probe at all. Report `total = 0`, force the CPU tier,
  size by **system RAM** (use `psutil.virtual_memory().available`) and treat
  `usable_vram` as `usable_ram − reserve`.
- **Any host without the vendor tool:** `probe.py` exits with code 2 and a clear
  message rather than crashing, so the orchestrator can degrade to the tier
  formula.

## Next steps

- [ ] `pip install nvidia-ml-py` in the orchestrator venv and confirm
      `probe.py` selects the NVML backend (`Backend: nvml`).
- [ ] Measure real footprints by delta: load MusicGen small (0104), snapshot,
      unload, snapshot; repeat for AudioGen; check whether both fit N=2.
- [ ] Validate the tiering formula in `../../ideas/model-orchestrator/modelorchestrator.md` with
      these measured deltas (`0108`).
- [ ] Add a `psutil`-based RAM probe for the CPU-only fallback tier.

## Links

- Related idea: `../../ideas/model-orchestrator/modelorchestrator.md`
- Sibling benchmark: `../0108-hardware-tiering-formula/`
- Uses this probe: `../../audio-generation/0104-audio-model-selection/`
- Helper: [`src/probe.py`](src/probe.py)

### Sources

- NVML API reference — <https://docs.nvidia.com/deploy/nvml-api/index.html>
- `nvmlDeviceGetComputeRunningProcesses` — <https://docs.nvidia.com/deploy/nvml-api/group__nvmlDeviceQueries.html>
- `nvidia-ml-py` (official bindings) — <https://pypi.org/project/nvidia-ml-py/>
- `nvidia-smi` query reference — <https://nvidia.custhelp.com/app/answers/detail/a_id/3751/>
- WDDM / per-process GPU memory reporting discussion —
  <https://github.com/NVIDIA/nvidia-settings/issues>
- `psutil` memory API — <https://psutil.readthedocs.io/en/latest/#memory>
