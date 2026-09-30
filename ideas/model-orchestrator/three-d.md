# Model Orchestrator — 3D & Animation

**Status:** exploring
**Last updated:** 2026-09-30
**Parent:** [`modelorchestrator.md`](modelorchestrator.md)
**Related:** [`catalog.md`](catalog.md), [`image.md`](image.md) (image → 3D input)

Covers the `mesh3d` and `animation` modalities: props, items, characters, and
rigged/animated meshes exported as GLB for Unity.

---

## Image → 3D

| Concern | Decision | Evidence |
|---------|----------|----------|
| Primary | **Stable-Fast-3D** | [0103](../../experiments/three-d-generation/0103-image-to-3d-selection/README.md) |
| MIT fallback | **TripoSR** | MIT licence |
| Shape-only fallback | **TripoSG** | MIT licence |
| Post-step | **Blender headless** + `trimesh` | Normalise, rescale, re-export GLB |

**Why the best models are out.** Hunyuan3D-2 full (17 GB w/ texture) and 2.1
(29 GB VRAM) fail on **licence**, not quality. Prefer **MIT** models (TripoSR,
TripoSG) and treat gated/non-commercial ones as development-only.

> **Selective download is mandatory** on the big repos. A 24 GB repo yielded a
> 4.4 GB usable subset (exp. 0103). Never clone a repo wholesale.

### Pipeline

```
image task ──► generate mesh ──► extract ──► bake texture ──► Blender normalise/export GLB
```

Each step may be its own scheduler-visible sub-task. The Blender normalise step
is **CPU-only** and runs on the CPU lane, so it never consumes the VRAM budget.

---

## Animation

| Concern | Decision | Evidence |
|---------|----------|----------|
| Approach | **Auto-rig + retarget** (UniRig MIT or Rigify) via Blender headless | [0106](../../experiments/three-d-generation/0106-animation-feasibility/README.md) |
| Generated motion | **Not feasible locally** | Blocked by **SMPL licensing**, not hardware |

**Animation is a template/retargeting problem, not a generative one.** The path
is auto-rig → auto-skin → retargeted stock clips.

> **Weight-sanity gate.** Generated meshes must pass a weight-sanity check
> (non-manifold / zero-weight validation) **before** they can be skinned. If the
> gate fails, fall back to an unrigged prop (exp. 0106).

---

## Scheduling notes

- `mesh3d` is **FOREGROUND** when the player is about to see the object, else
  **BACKGROUND**.
- 3D footprint varies with **input size**; the host reports a per-request
  estimate.
- The Blender normalise and rig steps are **CPU-only** and run in parallel with
  GPU work.

---

## Open questions

| # | Question |
|---|----------|
| Q11 | How to gate generated meshes before rigging (weight sanity)? |
| — | Which auto-rig path (UniRig vs Rigify) is the default? |
