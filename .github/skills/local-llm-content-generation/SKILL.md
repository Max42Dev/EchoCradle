---
name: local-llm-content-generation
description: 'Generate game content (lore, dialogue, quests, item descriptions, names, NPC personalities) with local LLMs via Ollama. Use when authoring narrative content, building prompt templates, requesting structured JSON output, or wiring an in-game LLM client. Triggers: Ollama, local LLM, generate lore, dialogue, quest, NPC personality, structured output, JSON schema, prompt template, content pipeline.'
---

# Local LLM Content Generation

## When to Use
- Authoring lore, dialogue, quests, item/ability descriptions, names
- Building reusable prompt templates for content pipelines
- Requesting structured (JSON) output that maps to game data
- Wiring an in-game or editor-time LLM client

## Prerequisites
- Ollama installed and running (`ollama serve`).
- A model pulled, e.g. `ollama pull llama3.1:8b` or `qwen2.5:7b`.
- MCP server `ollama` configured in `.vscode/mcp.json` (optional, for chat-driven generation).

## Model Selection (24GB VRAM budget)
| Task | Suggested model | Notes |
|------|-----------------|-------|
| General text/lore | `llama3.1:8b` | Fast, good quality |
| Reasoning/quests | `qwen2.5:14b` | Better logic, more VRAM |
| Code/C# | `qwen2.5-coder:7b` | For script generation |
| Embeddings | `nomic-embed-text` | For RAG over lore |

Keep one large model resident; unload others to avoid VRAM thrash.

## Procedure
1. **Define the schema first.** Decide the exact JSON shape the game expects.
2. **Write a system prompt** that fixes tone, setting, and constraints.
3. **Request structured output** — use Ollama's `format: "json"` or a JSON schema.
4. **Validate** the output against the schema; retry on failure with the error appended.
5. **Cache** results to disk keyed by prompt hash to avoid regenerating.
6. **Review** generated content for consistency with the world bible.

## Prompt Template Pattern
```
SYSTEM: You are the lore writer for <game>. Tone: <tone>. Setting: <setting>.
Rules: <constraints>. Output ONLY valid JSON matching this schema: <schema>.

USER: Generate <N> <thing> for <context>.
```

## Structured Output Example
```json
{
  "name": "Ashen Warden",
  "archetype": "undead guardian",
  "level": 12,
  "stats": { "hp": 240, "attack": 18, "defense": 22 },
  "abilities": ["Grave Chill", "Bone Ward"],
  "lore": "Once a knight of the fallen order..."
}
```

## In-Game Client (C#)
- Call Ollama's HTTP API at `http://localhost:11434/api/generate` or `/api/chat`.
- Use `async`/`await` (UniTask) and never block the main thread.
- Stream tokens for dialogue so the UI feels responsive.
- Always have a fallback (pre-authored line) if the model is unavailable.
- Keep prompts small; cache aggressively; consider a local RAG index for lore.

## Quality Control
- Maintain a **world bible** (names, factions, geography) and inject relevant excerpts.
- Use low `temperature` (0.2–0.5) for consistency, higher for brainstorming.
- Generate in batches, then deduplicate and rank.
- Human-review anything player-facing before shipping.

## Pitfalls
- Models hallucinate canon — always ground prompts with the world bible.
- Unbounded generation is slow; cap tokens and batch size.
- JSON mode can still emit invalid JSON — always validate.
- Don't ship raw model output without review.

## References
- [Ollama API](https://github.com/ollama/ollama/blob/main/docs/api.md)
- [Ollama model library](https://ollama.com/library)
