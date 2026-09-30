---
name: blender-mcp-asset-pipeline
description: 'Create and export 3D assets for the game using Blender via the Blender MCP server. Use when modeling props, characters, or environment pieces; applying materials; generating or importing models; and exporting GLB/FBX for Unity. Triggers: Blender, 3D model, mesh, material, UV, rig, GLB, FBX, export to Unity, Poly Haven, Poly Pizza, procedural modeling.'
---

# Blender MCP Asset Pipeline

## When to Use
- Modeling props, characters, environment kits, or modular level pieces
- Applying/creating materials and textures
- Importing free assets (Poly Haven, Poly Pizza, Sketchfab)
- Exporting assets to Unity (GLB/FBX)
- Batch/scripted mesh operations via Python

## Prerequisites
- Blender 3.0+ installed (this machine: Blender 5.2).
- `uv` installed; Blender MCP addon enabled and its socket server started.
- MCP server `blender` configured in `.vscode/mcp.json`.

## Procedure
1. **Start the bridge.** In Blender: `N` sidebar → *MCP for Blender* → **Start MCP Server**.
2. **Inspect the scene** before editing so you know what exists.
3. **Model or import.** Prefer procedural Python for repeatable geometry; use asset libraries for filler props.
4. **Materialize.** Apply PBR materials; keep texture resolutions modest (1k–2k) for game use.
5. **Optimize for the target.** Apply modifiers, triangulate where needed, keep polycounts within budget.
6. **Export** to `Assets/Art/Models/<category>/` as `.glb` (preferred) or `.fbx`.
7. **Verify in Unity** — import, check scale/orientation, assign materials.

## Game-Ready Conventions
- **Scale:** 1 Blender unit = 1 meter. Apply scale (`Ctrl+A → Scale`) before export.
- **Orientation:** Unity is Y-up, left-handed; Blender is Z-up, right-handed. Export with `+Y up` so Unity imports correctly.
- **Pivot:** Place the origin at the object's base/center of mass for sane placement.
- **Naming:** `SM_` static mesh, `SK_` skeletal mesh, `M_` material, `T_` texture.
- **Polycount budget:** props < 5k tris, characters < 30k tris, hero assets < 60k tris.
- **LODs:** author or generate LODs for anything placed many times.
- **Collision:** keep a simple collision proxy; don't use render meshes for physics.

## Export Settings (GLB)
- Format: glTF 2.0 Binary (`.glb`)
- Include: selected objects only, apply modifiers, `+Y up`
- Materials: export PBR; embed textures for small assets, external for large
- Animation: export only when the asset is animated

## Asset Libraries
- **Poly Haven** — CC0 HDRIs, textures, models. No API key.
- **Poly Pizza** — low-poly game assets; free API key; check licence (CC0 vs CC-BY).
- **Sketchfab** — broad catalogue; requires API key; check licences.
- Always record licence/attribution in the asset's metadata.

## Pitfalls
- Unapplied scale causes wrong sizes in Unity.
- Non-manifold geometry breaks some importers — clean up meshes.
- The addon socket has no auth; keep it on `localhost`.
- Blender freezes during large downloads (main-thread); prefer 1k/2k textures.

## References
- [MCP for Blender](https://github.com/ahujasid/mcp-for-blender)
- [glTF 2.0 spec](https://registry.khronos.org/glTF/specs/2.0/glTF-2.0.html)
