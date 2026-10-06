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

## TTS landscape (surveyed 2026-09)

Two families of option, and the choice between them is a **design decision, not a
tuning detail**: staying on the CPU-only path keeps voice I/O free of VRAM and
working on Tier 0, while the GPU models buy expressiveness at the cost of
competing with the text and image models for the budget.

### CPU-only (sherpa-onnx) — the current path

`sherpa-onnx` supports exactly these TTS families: **VITS** (incl. Piper,
MeloTTS), **Matcha**, **Kokoro**, **KittenTTS**, **MMS**, **SupertonicTTS**,
**ZipVoice** and **PocketTTS** (the last two are zero-shot cloning).

| Model | Params | Size | Licence | RTF (CPU) | Voices / languages |
|-------|--------|------|---------|-----------|--------------------|
| **Kokoro-82M v1.0** | 82 M | ~330 MB | Apache-2.0 | ~0.12 desktop | 54 voices, 8 languages |
| **Supertonic 3** | 66 M | tens of MB (int8) | ⚠️ OpenRAIL-M | **~0.012** | multi-speaker, **31 languages** |
| **Piper** | 20–63 M | 20–75 MB | MIT | ~0.05–0.1 | 1 voice/model, ~40 languages |
| **KittenTTS** | 15 M | <25 MB | Apache-2.0 | very fast | 8 voices, English |
| **MeloTTS** | ~52 M | ~50 MB | MIT | real-time | 6 languages + EN accents |
| **Matcha-TTS** | ~18 M | ~71 MB | MIT | fast | 1 speaker/model |
| **PocketTTS** | — | int8 | — | — | zero-shot clone (reference WAV) |
| **ZipVoice** | — | int8 | — | — | zero-shot clone (WAV + transcript) |

> **Piper quality tiers matter.** `vits-piper-en_US-amy-low` is the *lowest*
> tier and sounds robotic. `-medium` voices are a large, free improvement, and
> `vits-piper-en_US-libritts_r-medium` carries **904 speakers** — useful for a
> cast of NPCs from one model.

> **Kokoro checkpoint matters.** `kokoro-en-v0_19` is the Dec-2024 checkpoint
> (11 voices). sherpa-onnx now ships `kokoro-multi-lang-v1_1` (**103 speakers**,
> v1.0-class training) — a quality upgrade with no code change.

**Measured on the dev box** (L4 host, CPU synthesis, one 8 s sentence):

| Voice | Synth time | RTF |
|-------|-----------|-----|
| Piper amy-low | 0.54 s | 0.06 |
| Piper lessac-medium | 0.83 s | 0.11 |
| Kokoro | 6.35 s | 0.79 |

Kokoro is ~13× slower than Piper but still faster than realtime, so
sentence-level streaming stays ahead of the LLM.

### GPU-capable (would need a different runtime)

None of these are supported by sherpa-onnx. All fit the dev box's 24 GB L4.

| Model | Params | VRAM | Licence | Voice from description | Non-verbal tags | Cloning |
|-------|--------|------|---------|------------------------|-----------------|---------|
| **Maya1** | 3 B | **16 GB+** | Apache-2.0 | ✅ `<description="...">` | ✅ 17 tags | ✗ (unverified) |
| **Dia-1.6B** | 1.6 B | **~10 GB** | Apache-2.0 | ✗ | ✅ 21 tags | ✅ |
| **Chatterbox** | 0.5 B | ~2–4 GB *(est.)* | **MIT** | ✗ | ⚠️ Turbo/Nano only | ✅ |
| **Orpheus** | 3 B | ~8–12 GB *(est.)* | Apache-2.0 ⚠️ | ✗ | ✅ 8 tags | ✅ |
| **Parler-TTS mini** | 0.9 B | ~2–4 GB *(est.)* | Apache-2.0 | ✅ free text | ✗ | ✗ |
| **Zonos-v0.1** | 2 B | **6 GB+** | Apache-2.0 | ✗ | ✗ | ✅ |
| **Higgs TTS 2** | ~6 B | ~16–24 GB *(est.)* | ⚠️ 100k MAU cap | ⚠️ scene prompt | ✗ | ✅ |
| **Fish/OpenAudio S1-mini** | 0.5 B | ~1–2 GB *(est.)* | ❌ **CC-BY-NC-SA** | ✗ | ✅ ~60 tags | ✅ |

> **Licence caveats.** Fish/OpenAudio is **non-commercial** — disqualified for a
> shipped game. Higgs TTS 2 carries a **100 000 annual-active-user cap** and is
> not OSI-approved. Orpheus is Apache-2.0 on its card but is a finetune of
> `meta-llama/Llama-3.2-3B-Instruct` and the repo is gated, so the Llama terms
> may still apply — needs legal review.

> **VRAM figures marked *(est.)* are estimates** from parameter count at
> bf16 (weights + KV cache + codec), not vendor-stated. Only Maya1 (16 GB+),
> Dia (~10 GB) and Zonos (6 GB+) publish explicit figures.

### Feature syntax (verified)

**Voice design from a text description** — only two models support it:

```text
# Maya1 (Apache-2.0, 16 GB+)
<description="40-year-old, warm, low pitch, conversational"> Our new update <laugh> finally ships.

# Parler-TTS mini (Apache-2.0, ~3 GB) - free text, no tags, no cloning
"A female speaker delivers a slightly expressive and animated speech with a
 moderate speed and pitch."
```

**Non-verbal / paralinguistic annotations** — syntax differs per model:

| Model | Syntax | Examples |
|-------|--------|----------|
| **Dia** | parentheses | `(laughs)` `(sighs)` `(coughs)` `(whispers)` `(gasps)` `(singing)` `(applause)` — 21 tags |
| **Maya1** | angle brackets | `<laugh>` `<sigh>` `<whisper>` `<gasp>` `<scream>` `<cry>` `<sing>` `<sarcastic>` — 17 tags |
| **Orpheus** | angle brackets | `<laugh>` `<chuckle>` `<sigh>` `<cough>` `<sniffle>` `<groan>` `<yawn>` `<gasp>` |
| **Fish/OpenAudio** | parentheses | `(whispering)` `(shouting)` `(sighing)` + ~60 emotion markers |
| **Chatterbox Turbo/Nano** | square brackets | `[cough]` `[laugh]` `[chuckle]` (full list undocumented) |

> Dia's card warns that tags "will be recognized, but might result in unexpected
> output" — treat non-verbal tags as best-effort, not guaranteed.

### Quality reference

Artificial Analysis Provider Voice Arena, snapshot **2026-09**. Elo, higher is
better; open-weight entries marked **OW**.

| Rank | Model | Elo |
|------|-------|-----|
| 1 | ElevenLabs v4 | 1316 |
| 10 | **Breeze TTS 2 (OW)** | 1207 |
| 30 | **Fish Audio S2 Pro (OW)** | 1118 |
| 37 | **Step Audio EditX (OW)** | 1093 |
| 42 | **Mistral Voxtral TTS (OW)** | 1080 |
| **49** | **Kokoro 82M v1.0 (OW)** | **1064** |
| 52 | **NVIDIA Magpie 357M (OW)** | 1063 |
| 61 | **Maya1 (OW)** | 1045 |
| 71 | **Chatterbox (OW)** | 1023 |
| 75 | Zonos-v0.1 (OW) | 1000 |
| 84 | **XTTS v2 (OW)** | 916 |
| 87 | **StyleTTS 2 (OW)** | 893 |

Piper is **not on this board**, so there is no direct Piper-vs-Kokoro delta.
Kokoro sits **+148 Elo above XTTS-v2** and **+171 above StyleTTS2**, both of
which are generally regarded as clearly more natural than Piper — so
Kokoro-class is a large, audible step up.

### Decision

**Stay on the CPU-only sherpa-onnx path for v1.** It is Apache-2.0, has a C# API
(Unity-friendly), costs zero VRAM, and works on Tier 0. The GPU models buy
features (voice design, non-verbal tags) that are **nice-to-have, not
load-bearing** for the game, at the cost of the property that makes voice work
on every machine.

Concrete improvements to take first, all zero new runtime:

1. **Use Kokoro, not Piper**, as the default voice. Already the planner's choice.
2. **Upgrade to `kokoro-multi-lang-v1_1`** (103 speakers) when convenient.
3. **Consider Supertonic 3** for its 31 languages and RTF 0.012 — but review the
   OpenRAIL-M licence before shipping.

Revisit the GPU models only if a concrete need appears (e.g. per-NPC voice
design, or expressive non-verbal delivery). **Maya1** is the strongest single
feature fit if that day comes; **Chatterbox** (MIT) is the most permissive
fallback.

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
| — | Is per-NPC voice design (Maya1) worth the VRAM, or is a fixed voice set enough? |
| — | Does Supertonic 3's OpenRAIL-M licence permit shipping? |
| — | Upgrade `kokoro-en-v0_19` → `kokoro-multi-lang-v1_1` (103 speakers)? |
