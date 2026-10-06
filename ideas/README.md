# Ideas

Structured design documents describing the different parts of **EchoCradle** — an RPG where
local AI models generate content, images, 3D assets, levels, and NPC/monster AI.

## Purpose

This folder is the **design space**. It holds written, structured Markdown documents that
describe *what* we want to build and *why*. It is intentionally separate from `experiments/`,
which is where we validate *how* to build it.

## Conventions

- One topic per file, named in `kebab-case.md` (e.g. `world-generation.md`).
- A large topic may become a **folder** named in `kebab-case/`, with a `modelorchestrator.md`
  for the shared overview and one subfile per sub-topic (e.g.
  `model-orchestrator/modelorchestrator.md` + `text.md`, `image.md`, `three-d.md`, `audio.md`).
- Every document starts with a short **Summary** and a **Status** line
  (`draft` / `exploring` / `decided` / `deprecated`).
- Prefer bullet points, tables, and diagrams over long prose.
- Link related documents with relative links.
- When an idea is validated by an experiment, link to it under `experiments/`.
- When an idea is rejected, keep the document and mark it `deprecated` with the reason.

## Suggested documents

| File | Covers |
| --- | --- |
| `vision.md` | High-level pitch, pillars, target experience, non-goals |
| `architecture.md` | Unity + local AI runtime, process boundaries, data flow |
| `configuration.md` | Configs (typed JSON) as the source of truth; Unity objects derived from them |
| `git-state.md` | Git-backed save state: linear timeline, undo/redo, binary content, growth policy |
| `world-generation.md` | Procedural + LLM-driven world, regions, biomes, lore |
| `level-generation.md` | Layouts, dungeons, encounters, tilemap/terrain pipelines |
| `narrative.md` | Quests, dialogue, factions, memory and continuity |
| `npc-ai.md` | NPC/monster behavior, LLM-driven agents, utility AI, combat |
| `image-generation.md` | Portraits, textures, concept art, style consistency |
| `3d-asset-generation.md` | Meshes, rigging, animation, import pipeline into Unity |
| `audio-generation.md` | Music, ambience, SFX, voice |
| `model-orchestrator/` | Model Orchestrator: `modelorchestrator.md` (architecture, queue, VRAM budget) + per-modality files (`text.md`, `image.md`, `three-d.md`, `audio.md`, `catalog.md`) |
| `local-models.md` | Which models, quantization, VRAM budgets, fallbacks |
| `performance.md` | Latency budgets, caching, async generation, streaming |
| `modding-and-tools.md` | Editor tooling, content authoring, debug views |
| `roadmap.md` | Milestones and vertical slices |

## Status legend

- `draft` — rough notes, not yet reviewed.
- `exploring` — actively being researched; may have open questions.
- `decided` — agreed direction; changes require a note in the doc.
- `deprecated` — no longer pursued; kept for history.
