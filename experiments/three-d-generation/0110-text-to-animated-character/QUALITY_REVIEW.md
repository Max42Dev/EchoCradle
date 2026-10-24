# Character quality baseline and improvement log

## Baseline — 2026-10-11

Source: `results/mira-textured-animated-v3-2026-10-11/`.
Visual evidence: `results/courier_mira_debug_2026-10-11_15-31-12-368859Z/`.
All nine PNGs were inspected; **no videos reviewed**. Ratings are subjective
game-character suitability, not automated structural gate scores.

| Stage | Score | Visible observations |
| --- | --- | --- |
| Reference | 7/10 | Good illustration; turned torso, tilted hips, close legs, asymmetric hands. |
| Cutout/mask | 8/10 | Mostly clean silhouette; soft finger/hair edges. Historical cutout recreated. |
| Reconstruction input | 8/10 | Gray background, adequate margins, no cropping. |
| Raw mesh | 3/10 | Largest initial loss: lumpy/missing face, hands, hair/clothing detail; muddy colours. |
| Rig | Unrated | Picture too small/overlay unclear to establish bone or skinning correctness. Debug usefulness 2/10. |
| UV albedo | 3/10 | Very fragmented islands and black gaps; poor editable texture layout. |
| Textured rest, both views | 2/10 | Dark triangular artifacts; coarse face/hands and lost colour distinction. |

Priorities: (1) isolate/fix texture seams/coverage; (2) improve reconstruction
with existing settings before replacing models; (3) better symmetric reference
pose; (4) comparable close-up/debug rendering. Do not infer animation quality
from static pictures. Texture artifacts' exact cause was **not yet proven**.

## Comparison plan

Reuse the exact baseline learned rig to isolate baking/render changes. Save fresh
outputs, same cameras and lighting, original vertex-colour control, UV statistics
and before/after pictures. No new models/downloads. Only promote changes supported
by executed evidence. Raising texture resolution cannot recover missing geometry.

## Executed improvement — same models, 2026-10-11

See [comparison and updated ratings](results/mira-quality-texture-v2-2026-10-11/README.md).
Controlled baseline bake reproduced cracks; temporary-copy welded UV layout removed
most cracks without altering original geometry/weights. Shared-position custom
normals removed artificial faceting. New default implemented in `texture_bake.py`.
Same 1024² textures, same rig and five motions, no downloads or model inference.

**Updated scores:** UV/albedo 3 → 5/10; visible final appearance 2 → 4/10;
underlying geometry remains 3/10. Reference/cutout unchanged. Face/hands and washed-out
colour still need work, and originate before the texture bake (vertex-control evidence).
All five exported clips passed independent structural playback; 39 tests passed.
Animation quality remains unreviewed. Next cheap tests: colour handoff, symmetric
reference and bounded same-image TripoSR resolution comparison.