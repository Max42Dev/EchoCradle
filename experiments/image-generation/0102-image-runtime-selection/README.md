# 0102 — Image runtime selection (diffusers in-proc vs ComfyUI HTTP)

**Status:** running
**Date:** 2026-09-29
**Author:** agent

## Question

Should image generation run **inside the orchestrator** via `diffusers`, or via
the already-adopted **ComfyUI** HTTP server?

## Hypothesis

`diffusers` in-process gives finer VRAM control and cheap model load/unload for
the orchestrator's scheduler; ComfyUI costs a second process and extra VRAM but
gives ready-made workflows and models.

## Setup

- **OS:** Windows Server 2022 Datacenter (build 20348), AWS EC2
- **Hardware:** NVIDIA L4 24 GB GDDR6 (72 W, ~30 TFLOPS fp16 tensor), 16 GB RAM,
  **31.8 GiB free disk** (80 GB volume) — disk is the hard constraint
- **Runtime:** Python 3.13.15 on PATH. **Neither runtime is installed yet:**
  `torch`, `diffusers` and ComfyUI are all absent (verified 2026-09-29);
  nothing is listening on `127.0.0.1:8188`.
- **Models:** none downloaded — no SD 1.5 or SDXL checkpoint is on disk. Disk
  budget forbids pulling multi-GB checkpoints for a comparison experiment.
- **Comparison protocol (this run):** desk research against upstream source and
  vendor docs, plus two runnable clients in [`src/`](src/) that are the harness
  for the measured run once a runtime is installed.

> **Honesty note.** Latency and cold-load figures below are *estimates* derived
> from the L4's published throughput, published model sizes, and the behaviour
> documented in the upstream sources. They are labelled `est.`. Everything
> labelled `verified` was read out of the upstream code/docs cited in
> [Links](#links). The `--dry-run` path of `diffusers_min.py` and the
> connection-failure path of `comfy_client.py` were executed on this box and
> behave as documented.

## How to run

All commands assume the working directory is this experiment's `src/` folder.

### Option A — diffusers (in-process)

Requires `torch` (CUDA build) + `diffusers` + `transformers` + `accelerate`.
**This is a ~2.5–3 GB install** (`torch` CUDA wheel alone is ~2.3 GB), which is
why it has not been installed yet.

```powershell
cd C:\projects\EchoCradle\experiments\image-generation\0102-image-runtime-selection\src

# install (heavy — check free disk first)
python -m pip install torch --index-url https://download.pytorch.org/whl/cu126
python -m pip install diffusers transformers accelerate safetensors pillow

# dependency-free: prints the plan + VRAM estimate, imports nothing
python .\diffusers_min.py --dry-run

# SD1.5-class 512x512, unloads VRAM when done
python .\diffusers_min.py --seed 1234 --steps 20 --out out\axe_sd15_512.png

# SDXL-class 1024x1024 from a single-file checkpoint shared with ComfyUI
python .\diffusers_min.py --model C:\models\sd_xl_base_1.0.safetensors `
    --from-single-file --offload model --width 1024 --height 1024 `
    --steps 30 --out out\axe_sdxl_1024.png
```

`--offload none|model|sequential` selects whole-pipeline-on-GPU,
`enable_model_cpu_offload()`, or `enable_sequential_cpu_offload()`.

### Option B — ComfyUI over HTTP

**ComfyUI is not currently installed on this machine** — there is no ComfyUI
directory, no `comfy` command, and nothing on port 8188 (the client below
confirms this by failing cleanly). Install it only if you accept the disk cost:

```powershell
# either (recommended): comfy-cli
pip install comfy-cli
comfy install

# or manual clone
git clone https://github.com/comfyanonymous/ComfyUI C:\tools\ComfyUI
cd C:\tools\ComfyUI
python -m pip install torch torchvision torchaudio --extra-index-url https://download.pytorch.org/whl/cu130
python -m pip install -r requirements.txt

# run the server, reserving 3 GB for OS/Unity and dropping node result caching
python main.py --port 8188 --reserve-vram 3 --cache-none --disable-api-nodes
```

Then, from this experiment's `src/`:

```powershell
cd C:\projects\EchoCradle\experiments\image-generation\0102-image-runtime-selection\src

python .\comfy_client.py --stats                 # VRAM/RAM probe via /system_stats
python .\comfy_client.py --list-checkpoints       # what is installed
python .\comfy_client.py --prompt "gilded iron axe icon, game item" `
    --seed 1234 --steps 20 --out out\comfy_axe.png
python .\comfy_client.py --release-only           # POST /free: unload models, free VRAM
```

The client is stdlib-only (`urllib`) — no `pip install`. It uses
`POST /prompt`, polls `GET /history/{id}`, downloads via `GET /view`, and can
probe `/system_stats` and call `POST /free` to release VRAM.

### VRAM-release probe (either option, no checkpoint needed)

Both runtimes load the same `pytorch` CUDA context. To measure how much a bare
context costs on the L4, a ~1 million-parameter dummy model is enough:

```powershell
python -c "import torch; m=torch.nn.Linear(512,2048).cuda(); print(torch.cuda.memory_allocated()/1024**3); del m; import gc; gc.collect(); torch.cuda.empty_cache(); print(torch.cuda.memory_allocated()/1024**3, torch.cuda.memory_reserved()/1024**3)"
```

If the second number is not ~0, `empty_cache()` alone is insufficient and the
orchestrator must gate on `torch.cuda.memory_reserved()` (practice C below).

## Results

Estimates are for batch 1, fp16, on the L4 (24 GB). "est." = derived from
published model sizes/L4 throughput and upstream docs, not measured here.

| Metric | A) diffusers (in-process) | B) ComfyUI (HTTP, port 8188) |
| --- | --- | --- |
| **512×512 latency (SD1.5-class, 20 steps)** | ~2–4 s est. (fp16, whole pipeline on GPU) | ~2–4 s est. same engine + ~50–150 ms HTTP/queue hop; first-ever run pays a one-off graph/engine warm-up |
| **1024 SDXL-class latency (30 steps)** | ~12–25 s est. fp16; `enable_model_cpu_offload()` adds ~10–40 % | ~12–25 s est.; benefits from ComfyUI's per-node caching and its own offload heuristics, so slightly better than naive diffusers under memory pressure |
| **Peak VRAM** | *verified sizing*: SD1.5 fp16 weights ≈ 2.1 GiB; SDXL fp16 ≈ 6.9 GiB. +0.5–2 GiB activations → **~3–5 GiB (SD1.5)**, **~8–11 GiB (SDXL)** using `.to("cuda")`; `enable_model_cpu_offload()` cuts SDXL to roughly the largest component (UNet ≈ 5.2 GiB); `enable_sequential_cpu_offload()` to <2 GiB but much slower | Adds a second resident `torch` CUDA context and a weight-offload cache on top of the diffusers footprint: **~1–3 GiB more than option A** at equal settings. ComfyUI's dynamic-VRAM/async-offload path is better at running *under* a tight budget than at running *within* one. `--reserve-vram N` is the documented knob. |
| **Cold model load time** | ~2–5 s est. (SD1.5) / ~8–15 s est. (SDXL) from local disk to first sample; goes to tens of seconds if the HF cache is cold (that is a *download*, not a load) | Same weight-load cost **plus** interpreter + node-registration + frontend startup (~5–20 s) — but that cost is paid **once at server start**, not per request. Per-request cold load is the same as diffusers when the model is not resident. |
| **Unload / release VRAM sufficiency** | **Fully sufficient and explicit.** `del pipe; gc.collect(); torch.cuda.empty_cache()` reliably drops weights; ~0.3–0.6 GiB of CUDA context + allocator reserved blocks remain and are *not* reclaimable. The orchestrator controls exactly what is resident. | **Adequate but coarse.** `POST /free {"unload_models":true,"free_memory":true}` is the documented release path (verified in `server.py`). Defaults otherwise keep models cached in VRAM for reuse; `--cache-none` / `--disable-smart-memory` trade speed for released VRAM. The orchestrator cannot free *one* model: it is all-or-nothing per server, and it cannot get the second CUDA context back without stopping the process. |
| **Determinism via seed** | Deterministic given fixed seed **and** identical device/offload/dtype. `torch.Generator(device=...)` must match the run device; changing offload mode or GPU vs CPU generator changes the result. Torch ≥2.7 also defaults `torch.use_deterministic_algorithms` off — not needed for sampling determinism. | Same seed reproduces the same image **when the graph and dtype are identical**. `--deterministic` switches torch to slower deterministic kernels and (per the flag's own help text) "might not make images deterministic in all cases". Seed lives in the KSampler node; the full graph is recoverable from the saved PNG. Reproducibility is *good* but bounded by node-level caching, which can skip re-executing an unchanged subgraph. |
| **Disk footprint (runtime + model)** | Runtime: `torch` cu126 wheel ≈ **2.3 GiB** + diffusers/transformers/accelerate ≈ **0.3–0.5 GiB** → **~2.6–2.8 GiB installed**. Models: SD1.5 fp16 ≈ 2.0 GiB; SDXL fp16 ≈ 6.9 GiB. Sharing a single-file checkpoint with ComfyUI avoids a second copy. | Runtime: Python + torch + ComfyUI + frontend ≈ **2.5–3.0 GiB** (torch dominates and is *the same* torch). Models identical: SD1.5 ≈ 2.0 GiB, SDXL ≈ 6.9 GiB. **`--extra-model-paths-config` can point ComfyUI at the same checkpoint directory diffusers uses**, so the two options need not duplicate model weights. |
| **Programmatic control granularity** | **Maximum.** The orchestrator owns the pipeline object: dtype, attention backend, VAE slicing/tiling, group/layerwise offload, `device_map`, unload timing, per-request generator. Can call the very same process as the LLM runtime. | **HTTP-level.** Control is a JSON graph + server flags set at launch. Per-request VRAM control does not exist (only global `POST /free` and launch-time flags). Cannot instrument allocation from inside; needs `/system_stats` polling instead. |
| **Extra process overhead** | None — same process as the orchestrator, no serialization, no port. But a crash takes the orchestrator down with it, and heavyweight imports (torch) are paid by the service itself. | One extra long-lived process; localhost JSON is cheap (<1 ms) but every task is a full graph round-trip + poll. Crash isolation is the real win. |
| **Model / workflow ecosystem** | Everything on Hugging Face via `from_pretrained`; you assemble the pipeline (schedulers, ControlNet, IP-Adapter, LoRA) yourself in Python. | Large library of ready-made, maintained workflows (SD1.5/SDXL/SD3.5/Flux, ControlNet, inpainting, upscaling, background removal, 3D, audio); version-controlled workflow JSON; new models supported within days. Great for *variety*, not for embedding into a scheduler. |
| **Ease of scheduling alongside other models (LLM in VRAM)** | **Best fit.** The scheduler can bin-pack `usable_vram = 24 − reserved(3) = 21 GiB` and load/unload the diffusion pipeline on demand, co-resident with llama3.1:8b (~5–6 GiB) and Unity (~1–2 GiB). One process, one allocator, one place to enforce the budget. | **Viable but it fights you.** The second process holds VRAM you cannot precisely reclaim, and without being asked to unload it will keep models resident. Workable if ComfyUI is pinned to `--reserve-vram` / `--cache-none` and only driven serially, but the orchestrator's VRAM accounting must treat ComfyUI as an opaque, non-preemptible block. |

### Disk reality check

At **31.8 GiB free**, budget for *one* runtime path only:
SD1.5 (2.0 GiB) + diffusers runtime (2.8 GiB) ≈ **4.8 GiB**.
SDXL (6.9 GiB) instead of SD1.5 pushes this to **~9.7 GiB**.
Installing **both** runtimes ≈ 5.5–6 GiB of duplicated torch before any model.
See [Caveats](#caveats) for the mitigation.

## Conclusion

`promising` — **choose A (diffusers in-process) as the orchestrator's primary
image runtime; keep B (ComfyUI) as an out-of-band authoring/iteration tool.**

**Per option.**

- **A) diffusers — recommended for the runtime path.** It is the only option
  that satisfies the orchestrator's actual requirement: *fine-grained VRAM
  control so models load/unload on demand and co-exist with a text model.*
  Concretely: the scheduler can call `.to("cuda")` / `del pipe;
  torch.cuda.empty_cache()` itself, read `torch.cuda.memory_reserved()` to know
  what it really has, and bin-pack diffusion alongside llama3.1:8b against a
  single 21 GiB budget. Programmatic control is total, cold-load is cheap and
  explicit, seeding is straightforward, and export is one `image.save()`. The
  costs are real but bounded: ~2.8 GiB of runtime on disk, one process that
  must not crash, and you build the pipeline wiring yourself.
- **B) ComfyUI — recommended for authoring, not for the hot path.** It wins
  decisively on *ecosystem and iteration speed*: hundreds of maintained
  workflows, ControlNet/inpaint/upscale/removal nodes, and new model support
  without writing Python. But its VRAM control is HTTP-level and coarse —
  `POST /free` is all-or-nothing, models stay cached by default, and the second
  CUDA context is not reclaimable without killing the process. That directly
  conflicts with "load/unload diffusion models and co-exist with a text model
  in VRAM". Its second torch install would also duplicate ~2.3 GiB already
  counted for option A.

**Net:** run diffusers inside the orchestrator for the scheduled, player-facing
content path; run ComfyUI (if disk ever allows) as a human-driven lab where its
workflow library pays for the extra footprint. Because both consume the *same*
`.safetensors` checkpoints via `--extra-model-paths-config` / `from_single_file`,
a ComfyUI-authored workflow can be ported to the diffusers host later without
re-downloading models.

### When each wins

| Situation | Winner |
| --- | --- |
| Automatic, background, VRAM-budgeted generation driven by the scheduler | **diffusers** |
| Co-residency with a warm LLM and Unity inside one VRAM budget | **diffusers** |
| Simple, dependency-light integration into an existing Python service | **diffusers** |
| Exploring a new model / ControlNet / inpainting chain quickly | **ComfyUI** |
| Non-programmer authoring, or needing a niche model node | **ComfyUI** |
| Crash isolation from the orchestrator process | **ComfyUI** |
| Disk-constrained machine that must pick exactly one | **diffusers** (less runtime duplication) |

### Caveats

- **Neither runtime is installed**, so every latency/VRAM figure is an
  estimate. The numbers that *are* verified are model weight sizes, the
  ComfyUI HTTP routes and CLI flags, and the diffusers memory-optimisation
  APIs — all from the sources in [Links](#links).
- **The L4 is a 72 W inference card.** It has 24 GB but modest compute; SDXL
  at 1024 will be noticeably slower here than on desktop 3090/4090-class
  hardware. Size latency expectations accordingly.
- **Do not install both runtimes on this box** at current free space. If
  ComfyUI is wanted, first delete the diffusers `torch` wheel and share a
  single venv, or point ComfyUI at the orchestrator's Python environment.
- `torch.cuda.empty_cache()` returns *cached* blocks, not the CUDA context
  (~0.3–0.6 GiB). Schedule against `memory_reserved()`, not `memory_allocated()`.

## Next steps

- [ ] Install diffusers + torch (cu126) only, ~2.6 GiB — confirm free disk first.
- [ ] Download SD1.5 fp16 only (~2.0 GiB); **skip SDXL** until disk frees up.
- [ ] Run `diffusers_min.py` at 512/20 steps and record real latency + peak VRAM.
- [ ] Run the dummy-model probe above to quantify the non-reclaimable context.
- [ ] Measure co-residency: llama3.1:8b resident + SD1.5 pipeline loaded
      simultaneously; confirm total < 21 GiB.
- [ ] Only then, optionally, evaluate ComfyUI — and only if a shared venv avoids
      a second torch install.
- [ ] Promote finding to `../../ideas/model-orchestrator/image.md`.

## Links

Local:

- Related idea: [`../../ideas/model-orchestrator/image.md`](../../ideas/model-orchestrator/image.md)
- ComfyUI skill: `.github/skills/local-image-generation-comfyui/SKILL.md`
- MCP config: `.vscode/mcp.json` (`servers.comfyui` → `http://127.0.0.1:8188`)
- Harness: [`src/comfy_client.py`](src/comfy_client.py), [`src/diffusers_min.py`](src/diffusers_min.py)

Sources (upstream, read for this experiment):

- diffusers — reduce memory usage (offload, VAE slicing/tiling, layerwise
  casting, device maps): https://huggingface.co/docs/diffusers/optimization/memory
- diffusers — SDXL pipeline & `from_single_file`: https://huggingface.co/docs/diffusers/api/pipelines/stable_diffusion/stable_diffusion_xl
- ComfyUI — README (features, install, offline/`--disable-api-nodes`, portable): https://github.com/comfyanonymous/ComfyUI
- ComfyUI — CLI flags verified from source (`--reserve-vram`, `--cache-none`,
  `--disable-smart-memory`, `--deterministic`, `--lowvram/--highvram/--novram`,
  `--async-offload`, `--extra-model-paths-config`, `--cuda-device`): https://github.com/comfyanonymous/ComfyUI/blob/master/comfy/cli_args.py
- ComfyUI — HTTP routes verified from source (`/prompt`, `/history/{id}`,
  `/view`, `/queue`, `/interrupt`, `/free`, `/system_stats`, `/models/{folder}`):
  https://github.com/comfyanonymous/ComfyUI/blob/master/server.py
- ComfyUI — minimal API client example: https://github.com/comfyanonymous/ComfyUI/blob/master/script_examples/basic_api_example.py
- ComfyUI — system requirements: https://docs.comfy.org/installation/system_requirements
- NVIDIA L4 datasheet (24 GB GDDR6, 72 W): https://www.nvidia.com/en-us/data-center/l4/
- SD 1.5 model card: https://huggingface.co/runwayml/stable-diffusion-v1-5
- SDXL base model card: https://huggingface.co/stabilityai/stable-diffusion-xl-base-1.0
