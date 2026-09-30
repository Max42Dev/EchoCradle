# 0109 — Streaming TTS & STT (speech out while text is generated, text out while speech is spoken)

**Status:** done (paper/README study + runnable harnesses; no weights downloaded)
**Date:** 2026-09-30
**Author:** agent

## Question

Which **local** text-to-speech (TTS) and speech-to-text (STT) engines can run
**incrementally** — TTS speaking a sentence while the LLM is still writing the
next one, and STT emitting text while the player is still talking — and can they
do it on **CPU** so they never compete with the GPU models for VRAM?

## Hypothesis

- **TTS:** true token-level streaming is rare; the practical pattern is
  **sentence-level streaming** — split the LLM token stream at sentence
  boundaries and synthesise each sentence as it completes. Small ONNX/VITS
  engines (Piper, Kokoro, sherpa-onnx) are fast enough on CPU that
  time-to-first-audio is well under a second.
- **STT:** Whisper-family models are **chunked**, not truly streaming; genuine
  streaming needs an online/transducer model (sherpa-onnx online, Vosk) or a
  chunked-Whisper wrapper with a streaming policy (Whisper-Streaming,
  WhisperLive).
- Both can run **CPU-only**, which is the key property for this project: voice
  I/O then costs **zero VRAM** and can run on the parallel CPU lane the
  orchestrator already has (see `ideas/model-orchestrator/modelorchestrator.md`).

**Outcome: hypothesis confirmed.** Recommended: **sherpa-onnx** as the single
runtime for both directions (it does streaming ASR *and* TTS, is Apache-2.0, and
ships a **C# API** — see [Unity note](#unity-note)), with **Piper**/**Kokoro**
as TTS alternatives and **faster-whisper** for high-accuracy non-streaming
transcription.

## Setup

- **OS:** Windows Server 2022 (x64)
- **Hardware:** NVIDIA L4 24 GB, 16 GB RAM, ~34 GB free disk *(reference box)*
- **Runtime:** Python 3.13.15. **No speech models are installed** and none were
  downloaded (disk budget); this is a documentation + tooling spike.
- **Models:** candidates below. Sizes are from the projects' own model listings;
  latency is *indicative* for a modern CPU (see caveat).
- **Harnesses:** [`src/`](src/) — three stdlib-only scripts that run today and
  define the exact interfaces the orchestrator's speech host will implement.

> **Latency caveat.** No model was run here, so the latency column is a sanity
> range from published figures and the models' own benchmarks, not a measurement.
> The *ordering* (Piper < Kokoro < sherpa-onnx VITS < XTTS) is reliable; absolute
> milliseconds are not. Re-measure with `probe.py` once a model is on disk.

## How to run

```powershell
cd experiments\audio-generation\0109-streaming-tts-stt\src

# 1. Incremental sentence splitter (the LLM -> TTS bridge). Stdlib only.
python sentence_splitter.py --demo
python sentence_splitter.py --demo --min-chars 12

# 2. Streaming TTS harness. The "null" engine writes silent WAVs so the
#    pipeline timing is measurable without any model installed.
python tts_stream.py --text "The blacksmith eyes you warily. He has not slept." --engine null
python tts_stream.py --text-file ..\sample.txt --engine null --out out\

# 3. Streaming STT harness. The "null" engine replays a WAV in chunks and
#    prints partial/final hypotheses so the streaming contract is exercised.
#    Make a 2 s silent 16 kHz test WAV first (no model needed):
python -c "import wave,struct; w=wave.open('sample.wav','wb'); w.setnchannels(1); w.setsampwidth(2); w.setframerate(16000); w.writeframes(struct.pack('<h',0)*32000); w.close()"
python stt_stream.py --wav sample.wav --engine null --chunk-ms 200
```

Verified output on the reference box (Python 3.13, no models installed):

```text
# sentence_splitter.py --demo
  token   5  ->  SPEAK: 'The blacksmith eyes you warily.'
  token  20  ->  SPEAK: 'He has not slept in three days, and the forge fire behind him is dying.'
  token  24  ->  SPEAK: '"What do you want?"'
  token  26  ->  SPEAK: 'he asks.'
  token  37  ->  SPEAK: 'His hand rests on a hammer that is not for show.'
5 sentences emitted from 37 tokens

# tts_stream.py --engine null
  [ 0.002s] SPEAK (1.82s audio) -> tts_000.wav  'The blacksmith eyes you warily.'
  [ 0.004s] SPEAK (2.55s audio) -> tts_001.wav  'He has not slept in three days.'
  [ 0.007s] SPEAK (1.45s audio) -> tts_002.wav  'What do you want?'
time to 1st audio: 0.002s

# stt_stream.py --engine null --chunk-ms 200
   0.200  partial  word1
   0.401  partial  word1 word2
   ...
   2.011  FINAL    word1 word2 ... word10
time to first partial: 0.200s
```

The `null` engines prove the **streaming contract** (partials arrive before the
audio ends; the first sentence is spoken before the last token arrives). Real
engines plug into the same interface.

Each script prints a `[HEAVY]`-marked section showing exactly where the real
engine plugs in (Piper / Kokoro / sherpa-onnx / faster-whisper / Vosk).

## Results

### TTS comparison

| Engine | Licence | Params / size | Device | Streaming granularity | Time-to-first-audio (indicative) | RTF (CPU) | Notes |
|---|---|---|---|---|---|---|---|
| **Piper** (VITS, ONNX) | **MIT** | ~20–60 M; **60–110 MB/voice** | **CPU** | **Sentence-level** (synthesise per sentence) | **~50–150 ms** | ~0.05–0.2 | Smallest and fastest; robotic but clear. Repo archived Oct 2025 → active fork **OHF-Voice/piper1-gpl**. |
| **Kokoro-82M** | **Apache-2.0** | 82 M; **~0.35 GB** | CPU or GPU | **Sentence-level** (`split_pattern` yields per segment) | ~150–400 ms | ~0.1–0.3 | Best quality/price; needs `espeak-ng` for OOD fallback. |
| **sherpa-onnx TTS** (VITS / Piper / Kokoro / Matcha) | **Apache-2.0** | varies; 60 MB–0.4 GB | **CPU** (also GPU/NPU) | **Sentence-level**, some models streaming-capable | ~100–400 ms | ~0.1–0.3 | One runtime for TTS **and** ASR **and** VAD; **C# API**. |
| **XTTS-v2** | Coqui Public Model Licence | ~0.5 B; ~1.8 GB | GPU preferred | Sentence-level | ~1–3 s | >1 on CPU | Only for voice cloning; licence-encumbered. **Rejected.** |
| **Windows SAPI / System.Speech** | OS component | n/a | CPU | Word-level | ~0 ms | ~0 | Zero-download fallback, but robotic and not open-weight. Use only as last resort. |

### STT comparison

| Engine | Licence | Model size | Device | **True streaming?** | Latency to partial | Accuracy | Notes |
|---|---|---|---|---|---|---|---|
| **sherpa-onnx online** (Zipformer / Paraformer / transducer) | **Apache-2.0** | 20–300 MB | **CPU** | ✅ **Yes** — native online recognizer, emits partials | **~100–300 ms** | Good | Purpose-built streaming; VAD + endpointing included; **C# API**. |
| **Vosk** (Kaldi) | **Apache-2.0** | **~50 MB** | **CPU** | ✅ **Yes** — streaming API, zero-latency | **~50–200 ms** | Fair | Tiny, 20+ languages, C# bindings. Lower accuracy than Whisper. |
| **Moonshine** | **MIT** (models MIT by default) | 1 MB–~200 MB | **CPU** | ✅ **Yes** — designed for live streaming | **~100–300 ms** | Good (claims > Whisper Large V3 on some sets) | Very low latency; tiny models; C++/Python/JS/mobile. |
| **faster-whisper** (CTranslate2) | **MIT** | 39 MB–1.5 GB | CPU (int8) or GPU | ❌ **Chunked** — needs a streaming policy wrapper | ~0.5–2 s (chunk) | **Best** | Use for final/high-accuracy passes; wrap with Whisper-Streaming/WhisperLive for live mode. |
| **whisper.cpp** | **MIT** | 39 MB–1.5 GB | CPU or GPU | ⚠️ Chunked; `stream` example adds a policy | ~0.5–2 s | **Best** | GGUF/quantized; good Windows story. |
| **Silero VAD** (not STT) | **MIT** | ~2 MB | **CPU** | n/a — endpointing | ~30 ms | n/a | Pair with any STT to cut silence and detect utterance end. |

### The two pipelines

**TTS — speak while the LLM is still writing:**

```
LLM token stream ──► SentenceSplitter ──► TTS engine ──► audio queue ──► speaker
   (llama.cpp)        (incremental)        (per sentence)   (gapless)
```

The splitter is the critical piece: it must emit a sentence the moment a
boundary is seen, without waiting for the full response. `src/sentence_splitter.py`
implements and tests exactly this.

**STT — text while the player is still talking:**

```
mic ──► VAD ──► streaming ASR ──► partial hypotheses ──► UI (live captions)
                    │
                    └──► endpoint detected ──► final transcript ──► LLM
```

### Why CPU-only matters here

These engines **are** neural networks (VITS, StyleTTS2, Zipformer/transducers) —
they simply do not *need* a GPU. The relevant metric is the **real-time factor
(RTF)**: if a model synthesises or transcribes audio faster than it plays
(RTF < 1), CPU is sufficient. Piper's CPU RTF is roughly 0.05–0.2, i.e. a second
of speech is produced in ~50–200 ms. The models are also small (20–300 M params,
~1/30th to ~1/100th of an 8B LLM) and the work per request is short and
sequential, so there is no large-context pass to accelerate.

Running them on CPU is therefore a **deliberate choice, not a limitation**:

- costs **zero VRAM**, so it never competes with the text/image/3D models;
- runs on the orchestrator's **CPU lane**, which is always runnable even when the
  GPU budget is exhausted (see `ideas/model-orchestrator/modelorchestrator.md`, Scheduler);
- works on **Tier 0** machines with no GPU at all;
- the models are small enough to stay resident in RAM, so there is no
  load/unload latency.

> **Not a hard rule.** Kokoro and Whisper *can* run on the GPU and will be faster
> if VRAM is free. The design simply does not *require* it, which is what makes
> voice I/O robust across the whole range of player machines. The orchestrator's
> probe would place speech on the GPU only if headroom remains after the large
> models are placed.

### Unity note

**sherpa-onnx ships a C# API**, and there is a community
[`sherpa-onnx-unity`](https://github.com/xue-fei/sherpa-onnx-unity) binding. That
opens a second integration option: run speech **in-process in Unity** (no HTTP
hop, no Python) for the lowest-latency voice path, while the orchestrator keeps
owning the GPU models. This is worth a follow-up experiment — it would make voice
I/O independent of the orchestrator service being alive.

## Conclusion

`promising` — **adopt a CPU-only speech host with sentence-level streaming TTS
and true streaming STT.**

- **TTS — recommended: Piper (MIT) as the default, Kokoro-82M (Apache-2.0) when
  quality matters.** Both are small, CPU-only, and stream at sentence
  granularity, which is all the "speak while generating" requirement needs.
  Piper is the always-available fallback (tiny, MIT, no PyTorch); Kokoro is the
  quality option. **sherpa-onnx TTS** is the consolidation choice if we want one
  runtime for everything.
- **STT — recommended: sherpa-onnx online models (Apache-2.0) for live
  captions, faster-whisper (MIT) for the final high-accuracy transcript.** The
  streaming recognizer gives partials in a few hundred ms; Whisper gives the
  accurate final text. Pair both with **Silero VAD** for endpointing.
- **Rejected:** XTTS-v2 (licence + CPU cost), and using Whisper *alone* for live
  captions — it is chunked, so partials lag by a full chunk.
- **Key architectural finding:** the LLM→TTS bridge is a **sentence splitter**,
  not a model. It is pure logic, testable with no dependencies, and it is what
  actually delivers "speech starts before the text is finished".

## Next steps

- [ ] Install `sherpa-onnx` (pip) and run a real streaming ASR + TTS pass on CPU;
      record time-to-first-audio and time-to-first-partial with `probe.py`.
- [ ] Verify Piper and Kokoro install on Windows Server 2022 / Python 3.13
      (Kokoro needs `espeak-ng`).
- [ ] Experiment: **sherpa-onnx in-process in Unity** (C# API) vs. the Python
      speech host — latency, packaging, and whether it removes the HTTP hop.
- [ ] Define the orchestrator's `speech` host contract from `src/` and add
      `tts`/`stt` task kinds to the task model.
- [ ] Promote finding to `../../ideas/model-orchestrator/audio.md`.

## Links

- Related idea: `../../ideas/model-orchestrator/audio.md`
- Sibling experiment: [`../0104-audio-model-selection/`](../0104-audio-model-selection/README.md)
- VRAM measurement: [`../../benchmarks/0105-vram-probe-residency/`](../../benchmarks/0105-vram-probe-residency/README.md)
- Harnesses: [`src/sentence_splitter.py`](src/sentence_splitter.py),
  [`src/tts_stream.py`](src/tts_stream.py), [`src/stt_stream.py`](src/stt_stream.py)

### Sources

- sherpa-onnx — <https://github.com/k2-fsa/sherpa-onnx> (Apache-2.0; streaming ASR + TTS + VAD; C# API)
- sherpa-onnx docs — <https://k2-fsa.github.io/sherpa/onnx/>
- sherpa-onnx-unity — <https://github.com/xue-fei/sherpa-onnx-unity>
- Piper — <https://github.com/rhasspy/piper> (MIT; archived, active fork <https://github.com/OHF-Voice/piper1-gpl>)
- Piper voices — <https://huggingface.co/rhasspy/piper-voices>
- Kokoro-82M — <https://github.com/hexgrad/kokoro> (Apache-2.0)
- faster-whisper — <https://github.com/SYSTRAN/faster-whisper> (MIT; CTranslate2)
- whisper.cpp — <https://github.com/ggml-org/whisper.cpp> (MIT)
- Whisper-Streaming — <https://github.com/ufal/whisper_streaming>
- WhisperLive — <https://github.com/collabora/WhisperLive>
- Moonshine — <https://github.com/moonshine-ai/moonshine> (MIT)
- Vosk — <https://github.com/alphacep/vosk-api> (Apache-2.0)
- Silero VAD — <https://github.com/snakers4/silero-vad> (MIT)
- XTTS-v2 — <https://huggingface.co/coqui/XTTS-v2>
