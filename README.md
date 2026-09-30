# EchoCradle

An RPG where **local AI models** generate the content: narrative, images, 3D assets,
levels, and NPC/monster AI. Built with **Unity** on **Windows**, driven by local
MCP servers and agent skills.

## Vision

| Content type | Local AI stack |
|--------------|----------------|
| Lore, dialogue, quests, item text | Ollama (local LLM) |
| Character art, textures, icons | ComfyUI (Stable Diffusion) |
| 3D models, props, environments | Blender + Blender MCP |
| Levels & dungeons | Procedural generators (C#) + LLM design |
| NPC / monster AI | Behavior trees + local LLM for dialogue/intent |

Everything runs **locally** — no cloud APIs required.

## Repository Layout

```
.github/
  skills/                 # Agent skills (Unity, Blender, AI, review, testing, git)
  copilot-instructions.md # Always-on project guidance
.vscode/
  mcp.json                # Local MCP server configuration
docs/
  SETUP.md                # Full environment setup guide
Assets/                   # Unity project (created by Unity Hub)
```

## Getting Started

See **[docs/SETUP.md](docs/SETUP.md)** for the complete setup guide, including
installing Unity, Blender, Ollama, ComfyUI, and wiring up the MCP servers.

## Skills

Agent skills live in `.github/skills/` and load on demand:

- `unity-csharp-development` — gameplay code conventions
- `unity-mcp-editor-control` — drive the Unity Editor via MCP
- `blender-mcp-asset-pipeline` — 3D asset creation & export
- `local-llm-content-generation` — narrative/content via Ollama
- `local-image-generation-comfyui` — game art via ComfyUI
- `procedural-level-generation` — dungeon/terrain generators
- `npc-monster-ai` — behavior trees, perception, LLM agents
- `csharp-code-review` — review checklist
- `unity-testing` — EditMode/PlayMode tests
- `git-workflow` — commits, LFS, branching

## MCP Servers

Configured in `.vscode/mcp.json` (all local):

| Server | Purpose |
|--------|---------|
| `blender` | 3D modeling & export |
| `unity` | Unity Editor control |
| `comfyui` | Local image generation |
| `ollama` | Local LLM inference |
| `filesystem` | Scoped file access |
| `git` | Repository operations |
| `memory` | Persistent knowledge graph |
| `sequential-thinking` | Structured reasoning |
| `fetch` | Web content retrieval |

## License

See [LICENSE](LICENSE).
