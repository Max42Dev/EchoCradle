# EchoCradle — Project Instructions

An RPG where **local AI models** generate content: narrative, images, 3D assets,
levels, and NPC/monster AI. Unity + Windows. Everything runs locally.

## Golden Rules

1. **Local only.** Never introduce cloud AI APIs. Use Ollama (text), ComfyUI
   (images), Blender (3D). If a cloud service seems necessary, ask first.
2. **Disk is scarce.** This machine has limited free space. Avoid large
   downloads; clean up intermediates; prefer small models.
3. **Data-driven.** Game data lives in `ScriptableObject`s under `Assets/Data`.
   Runtime state is plain C#. Never put Unity objects in save data.
4. **Deterministic generation.** Level/content generators take a seed and are
   reproducible. Store the seed with the save.
5. **LLM output is untrusted.** Always validate structured output against a
   schema before using it. Never execute model output as code.
6. **Never block the main thread.** LLM/image/network calls are async with
   scripted fallbacks.
7. **No backwards compatibility.** This is development; things can change.
   Don't keep old code, shims, deprecated paths, or migration layers around —
   replace them outright.

## Code Conventions

- C#: `[SerializeField] private` over public fields; cache components in
  `Awake`; no allocations in `Update`; unsubscribe events in `OnDisable`.
- Naming: `PascalCase` types/methods, `camelCase` locals/params, `_camelCase`
  private fields.
- Assets: `SM_` static mesh, `SK_` skeletal, `M_` material, `T_` texture.
- Folders: `Assets/Scripts`, `Assets/Prefabs`, `Assets/Data`, `Assets/Art`,
  `Assets/Scenes`, `Assets/Tests`.

## Workflow

- Use the skills in `.github/skills/` for domain tasks (Unity, Blender, AI,
  review, testing, git).
- Use the MCP servers in `.vscode/mcp.json` to drive Unity, Blender, ComfyUI,
  and Ollama directly.
- Commit with Conventional Commits. Large binaries go through Git LFS.
- Run tests before committing gameplay changes.

## Architecture Overview

```
Content generation (offline / editor-time)
  Ollama ──► lore, dialogue, quests, item text (JSON)
  ComfyUI ─► portraits, textures, icons
  Blender ─► 3D models, props, environments

Runtime (in-game)
  Procedural generators ─► levels, dungeons, loot
  Behavior trees / utility AI ─► NPC & monster decisions
  Ollama (optional, async) ─► dynamic dialogue & intent
```

## Key References

- Setup: [`docs/SETUP.md`](../docs/SETUP.md)
- Skills: [`.github/skills/`](skills/)
- MCP config: [`.vscode/mcp.json`](../.vscode/mcp.json)
