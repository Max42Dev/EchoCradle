# Proposal B — Mira courier pilot

## Follow-up: actual model generated

After authorized removal of re-downloadable text/speech checkpoints, the fallback
**DreamShaper → TripoSR → SkinTokens → Blender** ran successfully. Open
[`SK_courier_mira.glb`](results/mira-2026-10-11/SK_courier_mira.glb),
[`SK_courier_mira.fbx`](results/mira-2026-10-11/SK_courier_mira.fbx) or
[`courier_mira.blend`](results/mira-2026-10-11/courier_mira.blend).
See [measured results and preview](results/mira-2026-10-11/README.md).

It has 18,348 triangles, 34 learned joints, vertex colour appearance and an
authored deformation smoke clip. No production movements or Unity validation.
SF3D remains gated; the sections below document its original blocked preflight.

## Implemented scope

`character.json` describes **Mira**, an adult human courier with short chestnut
hair, fitted teal tunic, brown trousers and ankle boots. Seed: **1102026**.
No cloak, equipment or complex hair: limb separation is the initial priority.

The executable path is:

**local Diffusers image → reviewed reference → SF3D textured mesh → Blender
preparation → SkinTokens full skeleton + skin prediction → Blender structural
QA → FBX candidate.**

This is a **riggable/animatable-candidate experiment**, not completed animation
generation. Retargeting, five licensed movement clips, deformation checks and
Unity playback remain unimplemented follow-up gates. The pilot never reports
`character_success=true`. No template skeleton is secretly fitted; SkinTokens
uses full prediction and texture transfer, not `--use_skeleton`.

## Actual attempt, 2026-10-11

- OS: Windows Server 2022 Datacenter.
- GPU: NVIDIA L4, **23034 MiB total / 439 MiB idle used**, driver **596.86**.
- Host: **15.384 GiB RAM**, about **10.094 GiB available** at inventory.
- Disk C: **5.35 GiB free** at inventory; manifest captures live free space.
- Existing project interpreter: Python **3.13.15**; pytest/pydantic available.
  **torch, diffusers, transformers and Pillow absent**.
- Shared model store: only existing text/speech runtimes/checkpoints found.
  No provisioned image, SF3D, SPAR3D or SkinTokens weights; no ComfyUI on port 8188.
- SF3D checkpoint **4,024,289,892 bytes** plus SkinTokens checkpoints
  **1,618,915,724 bytes** = **5.256 GiB**. That alone would almost fill the disk;
  image weights, CUDA wheels, preprocessors, caches and scratch are additional.
- Conservative initial budget: **12 GiB provisioning + 2 GiB scratch + 3 GiB
  reserve = 17 GiB free required**. This is an estimate, not a measured install
  size; replace only with a justified inventory/budget.
- Outcome: **blocked before inference**, recorded in
  `results/preflight-2026-10-11/manifest.json`. Nothing downloaded or deleted.
- Orchestration tests exercise schema checks, blocked manifests, overwrite
  protection, process failure, artifact hashing, timeout cleanup and syntax.
  **11 tests passed**; editor diagnostics and `git diff --check` were clean.
  These do not validate model quality or compiled upstream dependencies.

## Provisioning requirements

Copy `pipeline.local.example.json` to an ignored local config and fill absolute
paths. Do not place weights under this experiment; reuse the shared model store
or approved existing directories. Config contains no secrets.

Use separate model environments. The project Python 3.13 environment is for the
stdlib runner/tests, **not a claim of compatibility with these CUDA stacks**.
SkinTokens upstream recommends Python 3.11 / torch 2.7.0 / CUDA 12.8 wheels;
requires toolkit ≥12.1 and FlashAttention. It starts a local `bpy_server.py`,
requiring its own compatible bpy environment, independent of installed Blender
5.2. Native Windows FlashAttention/bpy compatibility must be tested first.
Do not install CUDA wheels before sufficient disk is available.

| Stage | Source pin | Weight pin |
|---|---|---|
| SF3D | `ff21fc491b4dc5314bf6734c7c0dabd86b5f5bb2` | `stabilityai/stable-fast-3d@f0c9a8ffd62cb1bbc8a7a53c9f87a0be1b6be778` |
| SkinTokens | `273b691d35989d71cd17ff2895fdc735097b92d1` | `VAST-AI/SkinTokens@79736cad0fd84de384d5eede659b4ebd24effe33` |
| Image | Existing approved Diffusers-format model preferred | **Unselected**: inventory found none; choose and record revision/terms before provisioning |

SF3D needs local `config.yaml` and `model.safetensors`, DINOv2 and rembg caches.
SkinTokens needs both TokenRig and skin-VAE files plus Qwen3-0.6B **configuration**
files; its provisioning script excludes Qwen weights. Preserve upstream relative
dependency layout under its checkout; the TokenRig path is configurable.
Record dependency locks and checkpoint hashes during provisioning. The runner
checks source commits/tracked cleanliness; config weight pins are declarations,
not proof of downloaded bytes. Audit weight hashes before approval.

SF3D uses Stability Community terms; SkinTokens uses MIT metadata. Explicitly
review all image-model, encoder and preprocessor terms and set
`licenses_approved` / `dependencies_provisioned` only after review/smoke tests.
No cloud inference, hosted fallback, API key or automatic provisioning exists.

Offline flags disable Hugging Face fetches and telemetry; they do **not** stop
arbitrary upstream downloader code. Pre-provision all caches, then enforce OS
external-network isolation while allowing SkinTokens' loopback server. A result
cannot pass the local-only/offline gate until that isolated execution is tested.

## Exact runner commands (from repository root)

Preflight only; works without model dependencies:

```powershell
& .\.venv\Scripts\python.exe experiments/three-d-generation/0110-text-to-animated-character/src/pipeline.py --output out/0110-preflight-new
```

After provisioning/configuration, generate the reference:

```powershell
& .\.venv\Scripts\python.exe experiments/three-d-generation/0110-text-to-animated-character/src/pipeline.py --config pipeline.local.json --output out/0110-mira-reference --run
```

Review full body, neutral A-pose, separated limbs, complete feet/hands and
consistent clothing. Set `reference_review_sha256` to the generated image's
manifest hash only after inspection. Then resume into **a fresh attempt folder**:

```powershell
& .\.venv\Scripts\python.exe experiments/three-d-generation/0110-text-to-animated-character/src/pipeline.py --config pipeline.local.json --reference out/0110-mira-reference/reference.png --output out/0110-mira-rig --run
```

Tests:

```powershell
& .\.venv\Scripts\python.exe -m pytest experiments/three-d-generation/0110-text-to-animated-character/tests -q
```

All stages are sequential, bounded by timeout, logged and hashed. A timeout
terminates the worker tree on Windows. A normal SkinTokens exit uses upstream
atexit teardown; verify actual GPU release during the inference experiment.
Fresh output folders prevent cached successes and overwriting prior assets.
No retries run automatically: record each retry as another attempt.

## QA limitations and next gate

Preparation assumes upstream glTF import orientation, standardizes height, and
decimates only above the triangle budget. It is **not anatomical retopology**.
Structural export checks mesh/UV/material budget, one armature, armature bindings,
nonzero normalized skin weights and ≤4 influences. It does not infer humanoid
bone roles, measure contact, check bind matrices comprehensively, validate bends,
or create a Unity Avatar. Bad UV/material/rig cases reject rather than fabricate
repairs. FBX contains a skinned candidate, **not movement clips**.

To continue: make the disk budget fit without unapproved deletions, choose an
approved image model, provision/smoke-test pinned native workers, run this pilot,
then measure deformation and implement owned-clip retargeting/Unity playback.