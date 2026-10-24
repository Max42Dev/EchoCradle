# Marigold: role in character generation

**Research rechecked:** 2026-10-11. No Marigold inference or downloads performed.

## Conclusion

The official ETH Zurich **Marigold** models predict per-image depth, camera-space
surface normals and intrinsic appearance/lighting. Their published output contracts
do **not** include complete human meshes, unseen backside reconstruction, skinning
or animation. Therefore they are not a direct replacement for TripoSR here.

This is narrower than “Marigold cannot be used for 3D”: predicted depth **can** be
back-projected into visible-surface points/triangles or used for Blender displacement.
That is 2.5D reconstruction. Extruding/capping that surface can make a closed object,
but does not establish a correctly reconstructed person's hidden anatomy. A larger
pipeline could combine Marigold with multiview synthesis, body priors or surface
optimization to produce a complete person; those additional systems provide the
missing full-body completion. No official standalone Marigold full-human mesh
generator was established by the checked sources.

## Pipeline implications

- Keep DreamShaper reference, U2Netp foreground removal, TripoSR reconstruction,
  SkinTokens rigging and MoMask animation.
- Marigold Normals could **add** visible-detail guidance to mesh/material refinement.
  Camera-space normals must be aligned/projected and converted to tangent space,
  then UV baked before use as a Unity normal texture. Hidden surfaces need another
  source. It cannot repair silhouette or anatomy simply through a normal map.
- Marigold Depth could add visible geometry constraints; the output is
  **affine-invariant**, not calibrated metric depth. Current TripoSR adapter
  accepts RGB, not depth/normal conditioning, so this is not plug-and-play.
- Intrinsic models could supplement albedo/roughness/metallicity extraction;
  projection/occlusion/UV consistency remain necessary.

## Official evidence

1. [Repository README](https://github.com/prs-eth/Marigold): describes dense image
   analysis (monocular depth, surface normals, intrinsic decomposition).
2. [Depth v1.1 model card](https://huggingface.co/prs-eth/marigold-depth-v1-1):
   output is affine-invariant depth in [0, 1], optional ensemble uncertainty.
3. [Normals v1.1 model card](https://huggingface.co/prs-eth/marigold-normals-v1-1):
   output is 3D unit vectors in screen-space camera coordinates, optional uncertainty.
4. [Diffusers integration](https://huggingface.co/docs/diffusers/main/en/using-diffusers/marigold_usage).

Code: Apache-2.0. Weights: CreativeML Open RAIL++-M restrictions. Existing Diffusers
supports local inference, but native Windows integration, actual VRAM/disk cost
and character-specific quality are not measured here.