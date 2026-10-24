# Unattended fallback — executed 2026-10-11

## Current automatic texture + animation stages

**Executed final run:**
[`results/mira-textured-animated-v3-2026-10-11/`](results/mira-textured-animated-v3-2026-10-11/),
**319.965 seconds**, zero human interventions, all stages completed.
All five named clips passed independent reimport; maximum joint-offset error
was **2.01e-7 local units** (gate: 0.005). Regression suite: **33 passed**.
The earlier failed first run and intermediate v2 remain separate diagnostic
evidence; the final v3 includes the improved dense UV packing/coverage gate.

The same one-command runner now performs:

1. Local reference generation with bounded foreground/framing retries.
2. TripoSR reconstruction and learned SkinTokens rigging.
3. Background Blender orientation/scale preparation, smart UV unwrap and
  **1024² UV albedo bake** from reconstructed vertex colours. Images are saved
  as `prepared/T_*_BaseColor.png` and embedded into exported GLB materials.
  Roughness (0.8) and metallic (0) are explicit constants, not estimated maps.
  UVs now use a welded **temporary** layout copy to avoid isolated triangles from
  split imported vertices; original rig geometry/weights are preserved. Shared-position
  shading normals reduce faceting. See [quality comparison](QUALITY_REVIEW.md).
4. Sequential local MoMask generation for **Idle, Walk, Run, Jump, Attack**.
5. Conservative anatomical mapping and per-clip retargeting, preserving target
  bone lengths. Separate named NLA tracks are exported to one GLB/FBX/Blend.
6. Independent GLB reimport checks **every frame of every clip** for preserved
  joint offsets, actual mesh deformation, expected frame counts and in-place
  horizontal root motion; UVs and embedded base-colour images are required.
7. **Optional visual debug log:** create a timestamped folder directly in
  `results/<id>_debug_<UTC date-time-microseconds>Z/`, with sequentially numbered
  `step_0_...`, `step_1_...` files. This replaces the standalone final-video output.
  Set `debug_capture: false` to skip it; it is enabled for this experiment by default.

The folder captures **every available stage**: input prompts/seeds, each reference
attempt, transparent foreground and alpha mask, the actual gray-background
reconstruction input, raw mesh picture + GLB, learned rig picture + visible bones,
UV albedo map + reports, textured rest-pose pictures, deformation-smoke video,
**each raw MoMask BVH skeleton video**, **each retargeted character clip video**,
playback reports, final GLB/FBX/Blend and **one final all-clips video**. Numbering
expands with reference retries and clip count, rather than stopping at step 10.
Original source files remain available beside previews. `README.md` links the
steps; `debug_manifest.json` records statuses, sources, hashes and errors;
`logs/` contains original pipeline logs plus per-preview Blender logs.

Logging is **not a pipeline gate**: preview failures do not change generation
success, and successful debug steps are retained when another step fails. Failed
generation attempts also get a best-effort bundle of available artifacts. A step
that never ran is marked unavailable, not invented. Static models get PNG renders;
animated models get MP4 at 20 fps. Blender Eevee renders at 512² with a fixed camera
fitted to all frames. `imageio-ffmpeg` encodes locally, retaining only one temporary
frame, and the normal timeout applies **per preview**. No models are downloaded.

For **historical runs** that did not save the cutout, U2Netp recreates background
removal for inspection; this is explicitly labelled recreated, not original evidence.
New runs persist the actual RGBA/mask and reconstruction-stage foreground.

To capture an **existing** result without rerunning shape, rig or motion inference:

```powershell
& .\.venv\Scripts\python.exe experiments/three-d-generation/0110-text-to-animated-character/src/debug_capture.py --config experiments/three-d-generation/0110-text-to-animated-character/automatic.local.json --run experiments/three-d-generation/0110-text-to-animated-character/results/mira-textured-animated-v3-2026-10-11
```

Each invocation creates a fresh debug folder and never rewrites the source assets,
original pipeline manifest or prior debug folders. Visual evidence is not a quality
certification; use it to separate image, geometry, rig, motion and retargeting defects.

**Latest visual debug folder:**
[`results/courier_mira_debug_2026-10-11_15-31-12-368859Z/README.md`](results/courier_mira_debug_2026-10-11_15-31-12-368859Z/README.md).
It captures the completed v3 run, including the explicitly recreated historical
cutout: **22 numbered steps, 9 PNG images, 12 MP4 videos**, plus source assets,
reports and logs. All MP4s decoded successfully; regression suite: **38 passed**.
The original intermediates remain in
[`results/mira-textured-animated-v3-2026-10-11/`](results/mira-textured-animated-v3-2026-10-11/).

**Verified review (2026-10-11):**
[Mira — all five animations](results/SK_courier_mira_animations_2026-10-11_15-18-19-444678Z.mp4),
rendered from the completed v3 export. MP4 decode verified **440 frames / 22 seconds
at 20 fps, 512×512**, with clip labels and embedded character textures visible.
Updated regression suite: **37 passed**. This is visual evaluation evidence only.

Edit `animations.json` for motion prompts, seeds, frame lengths (multiples of
four, at most 196 at 20 fps), `root_motion` (`in_place` or `travel`) and loop
intent. One to five clips are accepted. Loop intent is metadata, **not a claim
of seamless loops**. Locomotion defaults to in-place; vertical jump motion is
preserved. Animation generation is bounded and sequential, with no downloads.
`automatic.local.json` pins the MoMask checkout and supplies its provisioned
weights/BVH importer paths. Texture resolution accepts 512, 1024 or 2048.

Final assets are `SK_<id>.glb`, `SK_<id>.fbx`, `<id>.blend`; evidence includes
`texture_report.json` under `prepared/`, `motions/<clip>/motion_report.json`,
`retarget_report.json`, `playback_report.json` and `automatic_manifest.json`.
Prepared previews depict the textured rest pose and the separate deformation
smoke. The smoke action is removed from the final player's animation set.

**Limits:** no newly synthesized high-frequency texture detail or normal maps;
no anatomical, prompt-semantic, contact or loop-quality certification; no Unity
or independent FBX playback test. Learned rigs can change topology between runs
and mapping deliberately rejects unsupported skeletons. MoMask commercial rights
remain unestablished. `production_success=false` remains intentional.

## Earlier geometry-only result (historical)

## Result

One command, **205.026 seconds**, **zero human interventions** during execution.
No GUI launch, image selection, prompt edit or script change between stages.
Blender ran only as a background worker. The earlier interactive Blender window
was not used by this attempt.

Outputs: [`results/mira-automatic-2026-10-11/`](results/mira-automatic-2026-10-11/).
Open `SK_courier_mira.glb`, `SK_courier_mira.fbx`, `courier_mira.blend`, or
`preview_front.png`. Logs and `automatic_manifest.json` record all worker commands,
durations, reference attempts, output hashes and gate outcomes.

![Automatically generated and rendered geometry](results/mira-automatic-2026-10-11/preview_front.png)

| Stage | Wall seconds |
|---|---:|
| Local DreamShaper reference | 29.259 |
| Foreground/framing QA | 2.477 |
| Local TripoSR shape/colour | 29.562 |
| Local SkinTokens rigging | 114.895 |
| Background Blender QA, animation smoke, export/render | 28.740 |

**18,348 triangles, 28 learned joints, one skin, vertex colours, 84 animation
channels.** Skinning bindings and normalized ≤4 weights checked before export.
Rotating automatically selected weighted non-root `bone_24` moved **23,506**
evaluated vertices (max local displacement **0.17486**). This demonstrates actual
deformation, not correct anatomical motion. Height uses input spec **1.7 m**.

## Command

From repository root:

```powershell
& .\.venv\Scripts\python.exe experiments/three-d-generation/0110-text-to-animated-character/src/automatic_pipeline.py --config experiments/three-d-generation/0110-text-to-animated-character/automatic.local.json --output out/mira-new-automatic-attempt
```

`automatic.local.json` supplies local paths, source revision pins and bounds.
The runner uses provisioned models from the orchestrator's shared store;
no provisioning or cleanup occurs during inference. Use a fresh output folder.
Existing attempts are never overwritten. Failed attempts retain a manifest/logs.
Workers run sequentially with timeouts and worker-tree termination on timeout.
HF offline flags and shared pre-provisioned caches are inherited by every worker.
External network-disabled certification remains unperformed.

## Automatic gates and honest limits

- Reference gate: U2Net foreground occupancy and margins. First image passed;
  bounded deterministic seed retries (two configured, three maximum) are available.
  **This is not a pose/anatomy/identity classifier**. A cropped or bad image can
  still fool segmentation. There is no hidden human reference-approval step.
- Orientation: explicit **TripoSR Z-up → Blender** backend contract, not a
  per-character guessed transform. Other backends must declare their own contract.
- Rig gate: one armature, mesh bindings, normalized nonzero ≤4 bone influences,
  ≤30k triangles and measured nonzero deformation. No forced humanoid mapping.
- Smoke joint: automatically chosen by skin influence; no `bone_7` assumption.
  Largest influenced non-root bone is **not** necessarily an arm or useful limb.
- Export gate: valid GLB 2 header, skin/joints/weights and animation channels.
  FBX export succeeded; independent FBX reimport and Unity playback not run.

The seed-42 rerun produced **28 joints**, versus **34** in the earlier pilot.
Identical seed does **not** imply bitwise deterministic learned rigging here;
the FlashAttention/sampling stack's reproducibility is unverified. Do not advertise
deterministic character rigs until repeated-run comparisons pass.

**Still missing:** useful generated/retargeted motion, bone-role mapping,
anatomical deformation assessment, UV/PBR materials, Unity validation and
unattended success across multiple descriptions/seeds. Geometry remains coarse,
especially face/hands. SF3D Proposal B remains gated; this is the runnable TripoSR
fallback. `production_success=false` is intentional even on a structural pass.

Next priority is independent motion/retargeting on a known rig, then anatomical
mapping and Unity smoke playback—not interpreting this artificial bend as walk.