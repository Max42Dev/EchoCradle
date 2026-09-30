---
name: procedural-level-generation
description: 'Design and implement procedural and hand-authored level generation for the RPG. Use when building dungeon/terrain generators, room-and-corridor layouts, tilemap or mesh-based levels, biome placement, loot/enemy spawning, or seeding and reproducibility. Triggers: procedural generation, dungeon generator, level layout, room and corridor, BSP, wave function collapse, terrain, biome, spawner, seed, tilemap.'
---

# Procedural Level Generation

## When to Use
- Generating dungeons, caves, overworld regions, or interiors
- Placing rooms, corridors, props, enemies, and loot
- Building terrain or tilemap-based levels
- Making generation deterministic and reproducible via seeds

## Core Principles
- **Separate generation from presentation.** Produce a data model (grid/graph) first, then instantiate GameObjects/tiles from it.
- **Deterministic by seed.** Same seed → same level. Store the seed with the save.
- **Validate before building.** Ensure connectivity, reachability, and required rooms exist.
- **Constrain with rules.** Define min/max room sizes, corridor widths, spacing, and biome rules.
- **Generate in passes.** Layout → connectivity → decoration → population → validation.

## Common Algorithms
| Algorithm | Best for |
|-----------|----------|
| BSP | Structured dungeons with rectangular rooms |
| Room-and-corridor (graph) | Classic roguelike dungeons |
| Cellular automata | Organic caves |
| Wave Function Collapse | Tile-based, constraint-driven layouts |
| Perlin/Simplex noise | Terrain heightmaps, biomes |
| Poisson disk sampling | Evenly spaced props/enemies |
| Voronoi | Regions, territories, biome cells |

## Procedure
1. Define the level's **contract**: size, required rooms, difficulty, biome.
2. Implement the **layout pass** producing a grid/graph.
3. Implement the **connectivity pass** (MST + extra loops for interest).
4. Implement **decoration** (props, lighting, cover).
5. Implement **population** (enemies, loot, NPCs) using difficulty curves.
6. **Validate**: flood-fill reachability, required-room presence, no overlaps.
7. **Instantiate** into the scene (pooled, batched) and bake NavMesh if needed.
8. **Persist** the seed and any player-driven deltas.

## Data-Driven Tuning
- Put generation parameters in `ScriptableObject`s (room counts, sizes, weights).
- Use weighted tables for enemy/loot selection; keep them editable by designers.
- Version the generator so old saves can be migrated.

## Performance
- Generate on a background thread where possible; instantiate on the main thread.
- Batch tile/mesh creation; use `Mesh.CombineMeshes` or tilemap APIs.
- Pool props and enemies; avoid per-tile `Instantiate` churn.
- Bake NavMesh at generation time, not per-frame.

## Pitfalls
- Unreachable areas → always flood-fill validate.
- Non-deterministic `Random` → use a seeded `System.Random` or Unity's seeded state.
- Overlapping rooms → enforce spacing in the layout pass.
- Generation stalls → cap iterations and time-box the generator.

## References
- [Procedural Content Generation wiki](https://pcg.wikidot.com/)
- [Red Blob Games — dungeon generation](https://www.redblobgames.com/)
