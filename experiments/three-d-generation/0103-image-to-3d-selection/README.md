# 0103 — Image-to-3D model selection

**Status:** done (paper/README study — no local inference run, see *How to run*)
**Date:** 2026-09-29
**Author:** agent

## Question

Which local image-to-3D generator fits the **24 GB VRAM / ~34 GB free-disk** budget
and is fast, scriptable and Unity-friendly enough to produce shop-item, prop and
monster meshes as an orchestrator background task?

## Hypothesis

TripoSR is the fastest and lightest baseline; Hunyuan3D-2 (mini/Turbo) and
TRELLIS give better quality at a higher cost. At least one is usable within the
disk budget.

**Outcome:** hypothesis mostly confirmed, but the winner is **Stable-Fast-3D
(SF3D)** rather than TripoSR, because it is the only candidate that is *both*
TripoSR-fast/light **and** produces game-ready UV-unwrapped textured meshes. The
large quality models (Hunyuan3D-2 full / 2.1, TRELLIS, InstantMesh) are rejected
on **disk** or **Windows/VRAM** grounds, not on quality.

## Setup

- **OS:** Windows Server 2022 (x64). No reliable conda; use a plain `venv` + `pip`.
- **Hardware:** NVIDIA **L4 24 GB** (Ada, ~30 TF FP16, 300 GB/s), 16 GB system RAM,
  **~34 GB free disk** (hard constraint). Unity 6 co-resident (reserve ~3 GB VRAM).
- **Runtime:** Python 3.10/3.11 venv, PyTorch CUDA build matched to the driver;
  Blender **5.2.1 LTS** (`C:\Program Files\Blender Foundation\Blender 5.2`) for the
  post-import/export step (`src/blender_import_export.py`).
- **Models:** candidates below. Disk figures are **measured** from the Hugging Face
  API (`/api/models/<repo>/tree/main?recursive=true`) on 2026-09-29; they are the
  *whole-repo* size, and the "usable subset" is what you actually need to download
  for a given variant. VRAM/time are **published figures unless marked est.**

> **Scope note.** To compare fairly, "shape only" is used as the common scope
> (every tool can produce an untextured mesh). Texture capability and its extra
> cost are called out separately in the table and in each option's conclusion.

## How to run

These were **not** executed locally (the models are far larger than the disk
budget allows to trial all at once). The commands below are the documented entry
points to reproduce each result; run them behind the orchestrator's disk budget so
only one model is downloaded at a time.

```powershell
# TripoSR (1.6 GB model) ------------------------------------------------------
git clone https://github.com/VAST-AI-Research/TripoSR; cd TripoSR
pip install -r requirements.txt
python run.py inputs/item.png --output-dir out/ --bake-texture --texture-resolution 1024

# Stable-Fast-3D (3.8 GB, gated: request access on HF + `huggingface-cli login`) -
git clone https://github.com/Stability-AI/stable-fast-3d
pip install -r requirements.txt
python run.py inputs/item.png --output-dir out/ --texture-resolution 1024

# Hunyuan3D-2 mini Turbo (shape ~4.4 GB subset of a 24 GB repo) ---------------
#   download ONLY: hunyuan3d-dit-v2-mini-turbo/ + hunyuan3d-vae-v2-mini/
python minimal_demo.py --model_path tencent/Hunyuan3D-2mini `
  --subfolder hunyuan3d-dit-v2-mini-turbo --enable_flashvdm --output out/item.glb

# TripoSG (7.6 GB, MIT, shape only) -------------------------------------------
python -m scripts.inference_triposg --image-input inputs/item.png `
  --faces 5000 --output-path out/item.glb

# Normalise + re-export for Unity (this experiment's helper) -------------------
& "C:\Program Files\Blender Foundation\Blender 5.2\blender.exe" --background `
  --python src\blender_import_export.py -- out\item.glb ..\unity_import --target-size 0.3
```

## Results

Disk sizes **measured**; VRAM/time **published or estimated** for L4-class.

| Attribute | **TripoSR** | **SF3D** | **Hunyuan3D-2mini / Turbo** | **Hunyuan3D-2 (full)** | **Hunyuan3D-2.1** | **TRELLIS-image-large** | **InstantMesh** | **TripoSG** | **Photogrammetry**\* |
|---|---|---|---|---|---|---|---|---|---|
| **Input** | 1 image (RGB, alpha ok) | 1 image | 1 image (mini); 1–4 images (mv) | 1 image (mv variant too) | 1 image | 1 image (multi-image tuning-free) | 1 image → 6 views (Zero123++) | 1 image | ~20–200 photos |
| **Output mesh format(s)** | `.obj` (vertex colour) + baked texture; convert to GLB | **`.glb`** (UV + texture + PBR params) | trimesh → **`.glb`**/`.obj` | trimesh → GLB/OBJ | trimesh + PBR → GLB/OBJ | **`.glb`** (textured mesh) + `.ply` (Gaussians) | `.obj` (vertex colour) / `--export_texmap` | **`.glb`** (shape only) | `.obj`/`.ply` + texture maps → GLB |
| **Generation time (L4-class)** | **~1–3 s** (0.5 s on A100) | **~1–3 s** + bake ~1–2 s | shape ~2–6 s; +Paint ~20–60 s | shape ~10–30 s; +Paint ~60–180 s | ~20–60 s shape; paint heavy | ~10–40 s + postprocess (est.) | ~15–40 s (multi-view diff. dominant, est.) | ~5–15 s (est.) | minutes–hours |
| **Peak VRAM** | **~6 GB** | **~6 GB** | **~5–6 GB** (`--low_vram_mode`) | 6 GB shape / **16 GB** with texture | **10 GB** shape / 21 GB tex / **29 GB** both | **≥16 GB** | **~16 GB** (2-GPU option to fit) | **≥8 GB** | GPU optional, CPU-bound |
| **Model disk size** (measured, usable subset) | **1.6 GB** | **3.8 GB** | ~4.4 GB (of a 24.1 GB repo) | ~4.7 GB shape; ~17 GB +Paint (of 69.8 GB repo) | ~7.0 GB shape (14.2 GB repo) | **3.1 GB** | 6.9 GB repo | 7.6 GB | 0 GB models (AliceVision/Meshroom) |
| **Texture support** | vertex colour default; `--bake-texture` → RGB map, no PBR | **UV-unwrapped RGB + material params + delight** (best) | none (shape) / +Paint RGB | RGB texture (+Delight) | **PBR** texture (metallic/rough) | RGB texture baked into GLB | vertex colour / optional texmap | **none** (shape only) | **real** photographic texture |
| **Topology quality** | dense marching-cubes, blobby | **explicitly optimised + remesh (tri/quad)** | SDF→MC, decent, watertight-ish | like mini, higher detail | high detail | FlexiCubes, `simplify` ratio | dense iso-surface (res 256) | sharp-feature SDF/RF, clean | dense, needs retopo |
| **Licence** | **MIT** ✅ | Stability AI Community (free < $1 M rev.; gated) ⚠️ | Tencent Hunyuan 3D 2.0 Community ⚠️ | same ⚠️ | same ⚠️ | **MIT** ✅ (some submodules vary) | Apache-2.0 code (check weights) | **MIT** ✅ | Meshroom MPL-2.0 ✅ |
| **Ease of headless/scripted use (Windows)** | **easy** — `run.py` | **easy** — `run.py`; needs VS 2022 to build | easy — diffusers-like API, `api_server.py` | easy but heavy | easy but heavy | **hard** — Linux-tested, needs compiled CUDA submodules | easy (`run.py`) but config/variants | easy — `scripts/inference_triposg` | CLI (Meshroom), slow |
| **Unity import friendliness** | OBJ → needs GLB/FBX conversion (see `src`) | **GLB, game-ready** ⭐ | **GLB** ⭐ | GLB | GLB | **GLB** ⭐ | OBJ → convert | **GLB** ⭐ (untextured) | OBJ/FBX → convert |

\* Photogrammetry is the "fallback" row: it is *not* an image-to-3D generative
model, it is multi-view reconstruction (Meshroom/AliceVision, COLMAP+OpenMVS).

### Notes on the measurements

- **Disk** was measured per repository via the HF tree API. Repos ship multiple
  variants (e.g. Hunyuan3D-2mini ships `mini`, `mini-fast`, `mini-turbo` *and*
  matching VAEs ≈ 24.1 GB total, but Turbo needs only `hunyuan3d-dit-v2-mini-turbo`
  (3.65 GB) + `hunyuan3d-vae-v2-mini-turbo` (0.39 GB) ≈ **4.4 GB**). Selective
  download is mandatory here — never clone these repos wholesale.
- **L4 timing** is derived from published A100/4090 figures scaled to the L4
  (~½–⅓ of an A100 for FP16 matmul-heavy work, but comparable bandwidth per GB).
  Treat as **estimates to be validated by a follow-up run**.
- **VRAM** figures are the vendors' own statements (e.g. Hunyuan "6 GB shape /
  16 GB shape+texture"; TRELLIS "at least 16 GB"; SF3D/TripoSR "about 6 GB").

## Per-option conclusion

### TripoSR — `promising` (lightweight baseline)
MIT-licensed, 1.6 GB model, ~6 GB VRAM, sub-second on A100. Saves `.obj` with
vertex colours; `--bake-texture` adds a real texture map. **Why not the primary:**
output topology is dense/blobby (marching cubes on an implicit field), the default
output is OBJ-with-vertex-colour which Unity does not import cleanly (no vertex
colour → material), so it needs the Blender re-export step. Keep it as the
**fastest, licence-clean fallback**.

### Stable-Fast-3D (SF3D) — `promising` → **recommended primary**
Built on TripoSR and explicitly targets the game use case: it produces good
topology *without artifacts*, bakes to a **UV-unwrapped texture map**, and
**disentangles illumination** while predicting material parameters — which is
exactly why its output drops into a Unity PBR material. Native GLB, ~6 GB VRAM,
3.8 GB on disk, TripoSR-class speed. **Caveats:** the model is **gated** on HF
(request access + token) and is under the Stability AI Community License
(free for research and for commercial use **under $1 M revenue**; above that it
needs an enterprise licence). Windows support is labelled "experimental"
(needs VS 2022 to build the C++ UV-unwrapper/remesher).

### Hunyuan3D-2mini / Turbo — `promising` (best quality-per-GB)
`mini-Turbo` shape model is 0.6 B, 3.65 GB, ~6 GB VRAM (`--low_vram_mode`), GLB out
via trimesh. Good watertight-ish shapes, sharp for items. Texture via the separate
Paint model. **Caveats:** the **Tencent Hunyuan 3D 2.0 Community License** excludes
the **EU, UK and South Korea** as a Territory and requires a licence above
1 M MAU — not an OSI licence. Prefer this as the quality option only if the
licence/territory terms are acceptable.

### Hunyuan3D-2 (full, 1.1 B) — `rejected` (disk + licence)
Shape 4.7 GB but the **whole repo is 69.8 GB**, and shape+texture needs 16 GB VRAM
plus the Paint/Delight models (~+12 GB). Fails the 34 GB disk budget the moment you
also want texture, and carries the Tencent licence.

### Hunyuan3D-2.1 — `rejected` (VRAM + disk + licence)
2.9 B shape (7.0 GB), **10 GB VRAM shape / 21 GB texture / 29 GB both** against a
usable-VRAM budget of ~21 GB with Unity resident. Texture generation alone would
OOM a background task. Exceeds budget on two axes.

### TRELLIS-image-large — `rejected` (Windows + VRAM)
MIT-licensed and only 3.1 GB on disk, high quality, native textured GLB — very
attractive. **But** it requires **≥16 GB VRAM** (too tight alongside Unity) and is
**tested only on Linux**; Windows setup is an untested community path and needs
compiling several CUDA submodules (`spconv`, `diffoctreerast`, `nvdiffrast`, …).
The Windows integration risk is the blocker, not quality.

### InstantMesh — `rejected` (VRAM)
~16 GB VRAM (the demo uses **two GPUs** to fit), 6.9 GB repo, and it adds a
Zero123++ multi-view diffusion stage. Nice meshes, but the VRAM cost per job is too
high for co-residency with text/image models.

### TripoSG — `promising` (MIT shape-only alternative)
MIT-licensed, runner-up shape quality (SDF rectified-flow, sharp features),
≥8 GB VRAM, 7.6 GB on disk, GLB out with a `--faces` cap for game-ready polycount.
**Why not primary:** it is **shape only** — no texture — so it still needs a
texturing step. Use it when the Tencent/Stability licence terms are unacceptable
and only geometry is required.

### Classic photogrammetry-free approaches — `fallback`
- **Multi-view photogrammetry** (Meshroom/AliceVision, MPL-2.0; or COLMAP+OpenMVS)
  is fully local, license-clean and produces *real* photographic textures, but it
  needs many photos of the same object and is minutes-to-hours per asset — not a
  single-image, real-time generator. Useful for "scan this real prop" workflows.
- **Single-image depth→mesh** (Depth-Anything/MiDaS → displacement) yields reliefs,
  not closed prop meshes. Not suitable.
- **Asset-library fallback** (the pipeline we already have): Blender MCP +
  Poly Haven (CC0) / Poly Pizza / Sketchfab for props. **This is the guaranteed
  fallback when every generative model is unavailable or out of budget.**

## Conclusion

`promising` — the question is answered, with a clear budget.

**Recommended 3D pipeline (shape + texture, one image in):**

1. **Primary: Stable-Fast-3D** — game-ready UV+PBR textured GLB, ~6 GB VRAM,
   ~3.8 GB disk, TripoSR-class speed. Best fit for shop items and props.
2. **Fast licence-clean fallback: TripoSR** (MIT, 1.6 GB) for quick/untextured or
   when SF3D's licence/gating is a problem.
3. **Higher-quality shape when needed: Hunyuan3D-2mini-Turbo** (+ optional Paint),
   subject to accepting the Tencent Community Licence.
4. **MIT shape-only option: TripoSG.**
5. **Always-available fallback: Blender + asset libraries**; photogrammetry for
   scanned props.
6. **Post-step for every path: `src/blender_import_export.py`** — import, verify
   bbox/tri-count, recentre (base at origin), scale to target metres, re-export GLB.

**Disk plan (fits 34 GB with headroom):** SF3D 3.8 + TripoSR 1.6 + Hunyuan
mini-shape 4.4 + TripoSG 7.6 ≈ **17.4 GB** if all are staged; realistically stage
**SF3D + TripoSR (~5.4 GB)** and add others on demand. Selective download is
mandatory — never clone the full Hunyuan repos.

**Rejected for this box:** Hunyuan3D-2 full and 2.1 (disk/VRAM), TRELLIS (Windows +
16 GB), InstantMesh (16 GB).

**Blockers / risks:**
- SF3D and Hunyuan are **gated / non-OSI licensed** and Hunyuan excludes the
  EU/UK/KR territory — decide the licence posture before shipping.
- SF3D on Windows is "experimental" (needs a VS 2022 build toolchain).
- L4 timings are **estimates**; a follow-up must measure them.

## Next steps

- [ ] Run SF3D + TripoSR on 3 real generated item images; record wall-time, peak
      VRAM (`pynvml`) and GLB triangle count on the L4.
- [ ] Decide the licence posture (SF3D Community vs MIT-only TripoSR/TripoSG).
- [ ] Wire `src/blender_import_export.py` into the orchestrator's `mesh3d` host as
      the mandatory post-process.
- [ ] Promote finding to `../../../ideas/model-orchestrator/three-d.md` and start
      `ideas/3d-asset-generation.md`.
- [ ] Follow-up: texture-only step (Hunyuan3D-Paint / TripoSR `--bake-texture`)
      for meshes that only need geometry.

## Links

- Related idea: `../../../ideas/model-orchestrator/three-d.md` (Q3)
- Sibling: `../0106-animation-feasibility/` (what to do after the mesh exists)
- Helper: [`src/blender_import_export.py`](src/blender_import_export.py)

## References / sources

- TripoSR — https://github.com/VAST-AI-Research/TripoSR (MIT; ~6 GB VRAM; `--bake-texture`)
- Stable-Fast-3D — https://github.com/Stability-AI/stable-fast-3d (≈6 GB VRAM; UV unwrap + delight; experimental Windows)
- Hunyuan3D-2 — https://github.com/Tencent-Hunyuan/Hunyuan3D-2 ("6 GB shape / 16 GB shape+texture"); model zoo & variants
- Tencent Hunyuan 3D 2.0 Community Licence — https://github.com/Tencent-Hunyuan/Hunyuan3D-2/blob/main/LICENSE (Territory excl. EU/UK/KR; >1 M MAU clause)
- Hunyuan3D-2.1 — https://github.com/Tencent-Hunyuan/Hunyuan3D-2.1 ("10 GB shape / 21 GB texture / 29 GB both")
- TRELLIS — https://github.com/microsoft/TRELLIS (MIT; "≥16 GB"; Linux-only tested; GLB via `postprocessing_utils.to_glb`)
- InstantMesh — https://github.com/TencentARC/InstantMesh (Apache-2.0; Zero123++ multi-view; 2-GPU demo)
- TripoSG — https://github.com/VAST-AI-Research/TripoSG (MIT; ≥8 GB VRAM; `--faces`)
- Meshroom / AliceVision — https://github.com/alicevision/meshroom (MPL-2.0 photogrammetry)
- Model disk sizes: Hugging Face API `https://huggingface.co/api/models/<repo>/tree/main?recursive=true`, measured 2026-09-29.
- Blender glTF 2.0 exporter reference (5.2) — https://docs.blender.org/manual/en/5.2/addons/import_export/scene_gltf2.html