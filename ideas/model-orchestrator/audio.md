# Model Orchestrator — Audio (Music, SFX, Speech)

**Status:** exploring
**Last updated:** 2026-09-30
**Parent:** [`modelorchestrator.md`](modelorchestrator.md)
**Related:** [`catalog.md`](catalog.md), [`text.md`](text.md) (LLM→TTS bridge)

Covers the `music`, `sfx`, `tts` and `stt` modalities.

---

## Music & SFX

| Concern | Decision | Evidence |
|---------|----------|----------|
| Music | **MusicGen small** (medium as fallback) | [0104](../../experiments/audio-generation/0104-audio-model-selection/README.md) |
| SFX | **AudioGen medium** | [0104](../../experiments/audio-generation/0104-audio-model-selection/README.md) |

- MusicGen small ≈ **2.6 GB**; AudioGen medium ≈ **3.9 GB**.
- Music is long-running and lowest priority (**BACKGROUND** / **IDLE**).
- SFX is short and event-driven; several clips per call.
- **MusicGen large rejected**: ~21 GB weights, 60–90 s per 10 s clip (0104).

---

## Speech (TTS / STT)

| Concern | Decision | Evidence |
|---------|----------|----------|
| TTS | **Kokoro-82M** (CPU), **Piper** (MIT, CPU) fallback | [0104](../../experiments/audio-generation/0104-audio-model-selection/README.md) |
| Streaming TTS + online ASR | **sherpa-onnx** | [0109](../../experiments/audio-generation/0109-streaming-tts-stt/README.md) |
| Final STT | **faster-whisper** | [0109](../../experiments/audio-generation/0109-streaming-tts-stt/README.md) |
| Endpointing | **Silero VAD** | [0109](../../experiments/audio-generation/0109-streaming-tts-stt/README.md) |

**Speech is a streaming, CPU-only concern.**

- TTS streams at **sentence** granularity: split the LLM token stream and
  synthesise each sentence as it completes.
- STT needs a **true streaming** recogniser for live captions — Whisper is
  chunked and lags by a full chunk.
- Both run on **CPU**, so voice I/O costs **zero VRAM** and works on **Tier 0**.
- The LLM→TTS bridge is a **sentence splitter**, not a model.

> **Streaming tasks.** `tts` and `stt` are the only kinds that emit **partial
> results while running**. `tts` consumes the *token stream* of its `text`
> dependency rather than waiting for it to finish — the dependency edge is a
> **stream**, not a barrier (exp. 0109).

### Rejected

| Candidate | Why |
|-----------|-----|
| **XTTS-v2** | Only justified by voice cloning; licence-encumbered (0104) |

---

## Scheduling notes

- Music/SFX are **BACKGROUND** / **IDLE**; they may finish after the player
  enters the level.
- Speech runs on the **CPU lane** and is always runnable, even under full VRAM
  pressure.
- Voice dialogue chain: `stt` (CPU) → `text` (GPU) → `tts` (CPU), all
  **INTERACTIVE**.

---

## Open questions

| # | Question |
|---|----------|
| Q13 | Run speech in-process in Unity (sherpa-onnx C# API) instead of the Python host? |
