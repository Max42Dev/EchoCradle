# EchoCradle Model Orchestrator

One local service that turns **content requests** into **finished artifacts**
without the caller ever blocking. It picks the right model for the machine it is
running on, downloads it on demand, and drives it behind a uniform API.

This is the implementation of the design in
[`ideas/model-orchestrator/`](../ideas/model-orchestrator/modelorchestrator.md).
It is **not** an experiment — it is meant to stay in the project and be reused by
every experiment and, eventually, by the game.

## Status

| Area | State |
|---|---|
| Catalog, probe, planner, store | ✅ implemented and tested |
| Text host (llama.cpp) | ✅ verified end to end |
| Speech host (sherpa-onnx TTS + streaming STT) | ✅ verified end to end |
| Tool calling (generic JSON config tool) | ✅ implemented and tested |
| Image / 3D / music / SFX hosts | ⬜ not yet |
| HTTP surface for Unity | ⬜ not yet |
| VRAM bin-packing scheduler | ⬜ not yet (one model per host) |

## Model catalog

The catalog ships with open-weight models across tiers. Text models are chosen by
quality among those that fit the VRAM budget:

| Tier | Text model | Size | Licence |
|---|---|---|---|
| 0 | `qwen2.5-1.5b-instruct-q4km` | 1.0 GB | Apache-2.0 |
| 1 | `granite-4.2-3b-q4km` | 2.1 GB | Apache-2.0 |
| 2 | `granite-4.2-8b-q4km` | 5.1 GB | Apache-2.0 |
| 2 | `qwen3-8b-q4km` | 4.7 GB | Apache-2.0 |
| 3 | `qwen2.5-14b-instruct-q4km` | 8.4 GB | Apache-2.0 |

Speech models are CPU-only and always fit: `kokoro-en-v0_19` (TTS, 11 voices),
`piper-en-amy-low` / `piper-en-lessac-medium` (TTS), `whisper-base-en` and
`zipformer-en-streaming` (STT), `silero-vad` (endpointing).

## Install

```powershell
cd c:\projects\EchoCradle
python -m pip install -e .\orchestrator          # core, no heavy deps
python -m pip install -e ".\orchestrator[speech]" # + sherpa-onnx, numpy
```

## The model store

**The store location is a constant and must not change between experiments:**

```
%LOCALAPPDATA%\EchoCradle\
    models\     <- downloaded weights, shared by everything
    cache\      <- content-addressed generated artifacts
    logs\
```

On non-Windows platforms the same layout is used under `XDG_DATA_HOME`. Set
`ECHOCRADLE_HOME` to override the root (tests, CI). Always resolve it through
`orchestrator.paths.model_store_dir()` rather than building a path yourself, so
every experiment reuses the same downloads.

## Quick start

```python
from orchestrator import ModelOrchestrator, Modality

mo = ModelOrchestrator()
print(mo.capabilities())          # probe + what the planner would choose

mo.ensure_model(Modality.TEXT)    # downloads on first use, then loads
print(mo.chat([{"role": "user", "content": "Hello"}]))

mo.speak("Hello there.", "out.wav")
print(mo.transcribe("out.wav"))

# Live microphone: record one utterance and transcribe it, with partials.
from orchestrator import AudioRecorder

recorder = AudioRecorder(sample_rate=mo.stt.sample_rate)
reply = mo.listen(recorder, on_partial=print)

mo.stop()                         # always release the model subprocesses
```

## How it fits together

```
request ──► Planner ──► ModelStore ──► RuntimeHost ──► artifact
              │             │              │
           catalog +     download +     text / tts / stt
           probe         verify         (pluggable)
```

| Module | Responsibility |
|---|---|
| `catalog.py` + `catalog.json` | the declarative list of open-weight models |
| `probe.py` | VRAM / RAM / disk detection and the tier formula |
| `store.py` | the shared model directory, lazy download, extraction |
| `planner.py` | pick the best model that fits this machine |
| `tasks.py` | requests, priorities, states, artifacts |
| `tools.py` | generic tools the model can call (JSON config read/write) |
| `streaming.py` | the incremental sentence splitter (LLM → TTS bridge) |
| `hosts/text.py` | llama.cpp `llama-server` lifecycle and chat |
| `hosts/speech.py` | sherpa-onnx TTS, streaming and offline STT |
| `orchestrator.py` | wires it all together; the public API |

## Design rules this code follows

- **Local only.** No cloud calls, ever.
- **Open-weight only.** Every catalog entry carries a licence and a `shippable`
  flag; the planner can refuse non-commercial weights.
- **Disk is secondary.** Model choice is driven by VRAM fit and quality, never by
  free disk space. Disk pressure is handled by eviction, not downgrading.
- **Never attribute VRAM per process.** Windows/WDDM returns `[N/A]` for every
  PID, so footprints are measured as deltas of device `used` (exp. 0105).
- **Budget on peaks, not weights.** An LLM's KV cache grows with context; the
  budget uses `min_free_over_window − reserve − fragmentation_slack`.
- **LLM output is untrusted.** Structured output is schema-constrained *and*
  validated before use.
- **Speech is CPU-only.** TTS and STT cost zero VRAM and work on Tier 0.

## Tests

```powershell
cd orchestrator
python -m pytest -q
```

The suite covers the catalog, probe, planner, store, streaming splitter and the
tool layer. It needs no models and no GPU.

## Known limitations

- **One model per host.** The scheduler's VRAM bin-packing is not implemented;
  each host holds a single model at a time.
- **Tool calling needs the right prompt shape.** A prompt that asks for a persona,
  an agenda *and* tool calls at once suppresses the call. Split the work into an
  extraction call and a persona call — see
  [`experiments/llm-runtimes/introduction-test-tts-sst/README.md`](../experiments/llm-runtimes/introduction-test-tts-sst/README.md).
- **Reasoning models need `enable_thinking: false`** for short turns, or they
  return empty content. Recorded per model in the catalog.
- **No HTTP surface yet.** The design doc's FastAPI layer is not built; the
  in-process Python API is the current interface.
- **`llama-server` is spawned per text model.** Router mode (multi-model
  residency) is not wired up yet.
