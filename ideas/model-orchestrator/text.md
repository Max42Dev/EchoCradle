# Model Orchestrator — Text & Embeddings

**Status:** exploring
**Last updated:** 2026-09-30
**Parent:** [`modelorchestrator.md`](modelorchestrator.md)
**Related:** [`catalog.md`](catalog.md), [`audio.md`](audio.md) (LLM→TTS bridge)

Covers the `text` and `embedding` modalities: dialogue, lore, quests, item text,
structured JSON output, and the embeddings used for lore RAG / dedup.

---

## Runtime

| Concern | Decision | Evidence |
|---------|----------|----------|
| Inference engine | **llama.cpp `llama-server` in router mode** | [0101](../../experiments/llm-runtimes/0101-text-runtime-selection/README.md) |
| Convenience front-end | **Ollama** (optional) | Same engine; use if model-pull ergonomics matter |
| Embeddings | llama.cpp `/v1/embeddings` | One runtime for text + embeddings |
| Structured output | `response_format` JSON-schema | LLM output is untrusted — validate before use (Golden Rule 5) |

**Why router mode.** The scheduler needs **multi-model residency** — several text
models loaded in one process, loadable/unloadable on demand. llama.cpp's router
mode exposes `POST /models/load` and `/models/unload`, which is exactly the
residency control the scheduler needs. This is also what eliminated vLLM.

### Rejected

| Candidate | Why |
|-----------|-----|
| **vLLM** | Linux-only, one model per process — fails multi-model residency (0101) |
| **HF transformers + bitsandbytes** | No built-in server; heavy torch footprint (0101) |
| **LM Studio server** | Proprietary, GUI-first (0101) |

---

## Model families

| Tier | Class | Example family | Format | Notes |
|------|-------|----------------|--------|-------|
| 0 | tiny | 1–3 B instruct | GGUF Q4 | CPU-capable; short replies |
| 1 | small | ~3–4 B instruct | GGUF Q4_K_M | *(reference)* ~4.7 GB disk, ~5.5 GB VRAM |
| 2 | medium | ~7–8 B instruct | GGUF Q4_K_M | Default dialogue model |
| 3 | large | ~14 B instruct | GGUF Q4/Q5 | Best prose; needs co-residency headroom |
| 4 | xl | 30 B+ | GGUF Q4 | Only when VRAM allows alongside a generator |

- Prefer **GGUF** quantizations; Q4_K_M is the usual quality/size sweet spot.
- **Open-weight only** (Golden Rule / G8): Apache-2.0 or MIT families preferred.
- The catalog descriptor carries `quality`, `license`, `shippable` and the
  **measured** VRAM footprint (see [`catalog.md`](catalog.md)).

---

## Scheduling notes

- `text` is the **INTERACTIVE** hot path: target < 2 s to first token.
- KV cache **grows with context**, so the scheduler must budget the **peak**
  footprint, not the resident weight size (exp. 0105).
- Text and a generator are the two expected **hot** models (`N = 2` in the
  co-residency budget) — see [`modelorchestrator.md`](modelorchestrator.md#capability-probe--tiering-formula).
- `tts` consumes the text host's **token stream** rather than waiting for the
  task to finish; the dependency edge is a stream, not a barrier
  (see [`audio.md`](audio.md)).

---

## Tool calling

Models may call **tools** mid-turn (read/write a JSON document, query state).
The orchestrator exposes them in the OpenAI `tools` format, which llama.cpp
understands, and executes the calls through a `ToolRegistry`.

> **Finding (introduction-test-tts-sst).** Tool calling fails when the *same*
> prompt also asks the model to hold a persona and follow a multi-step agenda.
> Bisecting the system prompt showed each instruction is harmless alone but the
> combination suppresses the call entirely — **instruction competition**, not a
> capability wall.
>
> **Fix: split the work into two calls per turn.**
>
> 1. an **extraction** call with no persona and no agenda, which only decides
>    what to record, and
> 2. a **persona** call with no tools, which only decides what to say.
>
> Measured: Qwen2.5-7B combined prompt 0/3 calls; Granite 4.2-8B combined 1/3;
> Granite 4.2-8B extraction-only **4/4**. The split costs no extra VRAM (two
> calls to the same model) and lets the extraction step use a different or
> smaller model later.
>
> **Model choice still matters, but less than prompt structure.** Qwen2.5 is a
> 2024 model with weak tool-calling post-training; prefer 2025–2026 models
> (Granite 4.2, Qwen3/3.5, Hermes 4). On BFCL v4, Qwen3-8B (42.6) beats
> Llama-3.3-70B (31.9) — post-training dominates parameter count.

### Practical notes

- Start `llama-server` with `--jinja` so the model's own chat template (and
  therefore native tool calling) is active.
- **Reasoning models** (Granite 4.2, Qwen3) spend the whole token budget on
  hidden reasoning and return empty `content` unless thinking is disabled. Pass
  `chat_template_kwargs: {"enable_thinking": false}` for short turns.
- Keep the KV cache at f16; llama.cpp documents that quantized KV degrades tool
  calling.
- Put tool definitions last (closest to generation) and keep the persona short.

---

## Open questions

| # | Question |
|---|----------|
| Q9 | Do in-process diffusers and llama.cpp actually co-exist inside the VRAM budget? |
| — | Which family is the default per tier once benchmarked on more than the dev box? |
| — | Does a dedicated extraction model (xLAM-2-3b, ToolACE-2-8B) beat a general model for the second call? |
| — | Is the persona/extraction split still needed on a 14B+ model? |
