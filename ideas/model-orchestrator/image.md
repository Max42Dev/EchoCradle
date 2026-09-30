# Model Orchestrator — Image Generation

**Status:** exploring
**Last updated:** 2026-09-30
**Parent:** [`modelorchestrator.md`](modelorchestrator.md)
**Related:** [`catalog.md`](catalog.md), [`three-d.md`](three-d.md) (image → 3D)

Covers the `image` modality: item icons, portraits, textures, concept art, UI
icons. Images are also the **input** to the image-to-3D pipeline.

---

## Runtime

| Concern | Decision | Evidence |
|---------|----------|----------|
| Hot path | **diffusers + torch, in-process** | [0102](../../experiments/image-generation/0102-image-runtime-selection/README.md) |
| Authoring only | **ComfyUI** (HTTP) | Workflow ecosystem; not on the scheduler's hot path |

**Why diffusers, not ComfyUI.** ComfyUI's HTTP-level VRAM release is
all-or-nothing and its second CUDA context is not reclaimable, so the scheduler
cannot bin-pack it. `diffusers` allows manual `.to("cuda")` / unload with
`torch.cuda.empty_cache()`, giving per-model VRAM control.

> **Crash containment.** `diffusers` runs in the *same* process as the
> orchestrator, so a diffusion crash takes the whole service down. Run image
> hosts in a supervised child process, or guard with try/except + restart.
> ComfyUI remains the crash-isolated escape hatch for authoring-time work.

---

## Model classes

| Tier | Class | Notes |
|------|-------|-------|
| 0 | SD1.5-class / tiny distilled | Small images, CPU or low VRAM |
| 1 | SD1.5-class | 512×512 icons/textures |
| 2 | SDXL-class | 1024×1024 portraits, better prompt adherence |
| 3 | SDXL-class + refiner / larger | Best quality; runs alongside text |
| 4 | largest available | Larger batches, fewer serialisations |

- Prefer **open-weight** checkpoints (Apache-2.0 / MIT / OpenRAIL where
  redistribution is permitted); record `shippable` per descriptor.
- Footprint varies with **resolution** and step count, so the host must report an
  **estimated VRAM footprint per request**, not a static model size.

---

## Determinism

> **Caveat from exp. 0102.** Determinism holds only when the seed **and** the
> device/dtype/offload mode match. A CPU-vs-GPU generator or a different offload
> setting changes the image. The cache key must therefore include the resolved
> device/offload configuration, not just the visible request parameters.

---

## Scheduling notes

- `image` is typically **FOREGROUND** (an item the player is about to see) or
  **IDLE** (pre-generating shop assets).
- Diffusion **peaks at the denoise step** — budget the peak, not the resident
  weight size (exp. 0105).
- Image output feeds `mesh3d` as a dependency; the mesh task stays `blocked`
  until the image task is `done`.

---

## Open questions

| # | Question |
|---|----------|
| Q9 | Do in-process diffusers and llama.cpp actually co-exist inside the VRAM budget? |
| — | Which checkpoint is the default per tier once benchmarked on more than the dev box? |
