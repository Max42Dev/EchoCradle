# Local generated movement experiment — 2026-10-11

## Integrated follow-up

Motion is now part of `automatic_pipeline.py`, not a manual post-process.
`animations.json` supplies five named player clip requests. The retargeter handles
multiple BVHs on one rig, strips the deformation smoke, emits named NLA tracks,
and applies explicit in-place/travel root policy. A fresh Blender worker reimports
the final textured GLB and checks every clip. See [AUTOMATION.md](AUTOMATION.md).
The historical single-walk attempts below retain their original measurements.

## Follow-up: corrected retarget structurally passes

The initial rejected retarget described below is preserved for diagnosis.
The rotation-only/non-root-zero-translation correction now passes strict GLB
reimport: **120 frames / 20 fps / 6 seconds**, root travel **3.767 m**, maximum
joint-offset length error **4.41e-7 local units**, against a 0.005 gate. Open
`results/mira-walk-corrected-2026-10-11/SK_courier_mira_walk.glb` or FBX;
sampled renders and measured checks are in its `playback/` directory.

**Visual limit:** arms still extend too far outward; rest-delta rotation does
not fully resolve source/target arm rest-pose differences. This is a generated
motion retarget candidate, not a polished useful walk or proof of foot contact.
No Unity test. This isolated result preceded integration into the one-command runner.

## Initial result: motion succeeded; first retarget rejected

**Real text-to-motion now ran locally**, separate from the earlier authored bend.
Prompt: “A person walks forward naturally at a steady pace.” Seed **10107**.
MoMask produced **120 frames / 22 joints / 20 fps / 6 seconds**, in **10.094 s**
including model load and BVH conversion. Peak torch CUDA allocation:
**953,722,880 bytes**. Source root travel **3.930** model units, BVH-fitting RMSE
**0.0130**; foot contact and visual quality not certified.

Files: `results/motion-walk-2026-10-11/motion.bvh`, `motion_joints.npy`,
`motion_bvh_joints.npy`, `motion_report.json`.

Automatic topology/rest-position mapping identified **22 body roles** on the
28-joint generated character without fixed generic bone-name assignments.
The retargeter exported `results/mira-walk-2026-10-11/SK_courier_mira_walk.glb`
and FBX. **Independent GLB reimport rejected playback**: maximum joint-offset
length error exceeded the **0.005 local-unit** gate. These are diagnostic assets,
**not working/approved character movements**. Inspect `playback/playback_report.json`.

## What is implemented

- Selective online provisioning: `src/provision_motion.py`, shared store
  `%LOCALAPPDATA%/EchoCradle/models/momask-character/`, about **531 MiB** weights.
  No training dataset, KIT, SMPL, evaluator or GloVe downloaded.
- Local generation-only worker: `src/motion_worker.py`, full mask + residual +
  RVQ decoding, checkpoint-provided 263-feature normalization, XYZ recovery/BVH.
- Conservative anatomical mapping and rest-delta retarget: `src/retarget_motion.py`.
  Unexpected pelvis/spine/limb topology rejects rather than inventing roles.
- Independent export playback checks and sampled diagnostic render:
  `src/render_motion.py`. Diagnostic mode never changes a rejected QA outcome.

MoMask source: `EricGuo5513/momask-codes@94a6636c9c463b7a9414c3401a6f1b67e6c51824`.
Official HumanML checkpoint ZIP from upstream's Google Drive link, CLIP ViT-B/32
SHA-256 `40d365715913c9da98579312b702a82c18be219cc2a73407c4526f58eba950af`.
Actual checkpoint hashes inventoried in the shared store's `provisioning.json`.
Dependencies reused the existing Python3.11/torch2.7 CUDA worker, adding CLIP,
ftfy, matplotlib3.8.4 and gdown. No replacement CUDA environment.

Compatibility adaptations are worker-local: removed NumPy aliases, old
`numpy.core.umath_tests.matrix_multiply` mapped to `np.matmul`, fixed local CLIP
path, trusted official checkpoint pickle loading (not permitted for arbitrary
model files). Native bpy4.2 process teardown returned failure after exporting;
retargeting now uses installed Blender5.2 in background, no GUI/manual editing.
Source BVH actions are removed before export to prevent irrelevant action tracks.

Licensing remains **research-only / commercial clearance not established**:
MIT application code does not clear pretrained/dataset or dependency rights.

## Next fix

Resolve retarget pose/basis/joint-offset preservation and exported FPS independently
on the existing generated motion. Do not regenerate/download more models to solve
a retargeting error. Only after strict reimport gates pass should this motion
stage join the one-command character runner. Then assess foot contact, anatomical
deformation, root semantics and Unity playback. Five useful clips remain undone.