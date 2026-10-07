# EchoCradle Model Orchestrator

One local model service, with an engine-neutral Python `ServiceClient`, backed
by the planner, shared store and host adapters. The **bounded first slice is
implemented as of 2026-10-06**: loopback REST jobs/artifacts and JSON WebSocket
text/tool events. It starts from **cached models and runtime binaries only**;
there are no automatic service downloads. The Python client is synchronous and
can block; use a worker for UI integration. No Unity client exists yet.

This implements parts of the design in
[`ideas/model-orchestrator/`](../ideas/model-orchestrator/modelorchestrator.md).
The reusable package is **not** an experiment, but the full design is not built.
See [service usage](../docs/SERVICE_USAGE.md) for exact commands and wire fields.

## Status

| Area | State |
|---|---|
| Catalog, probe, planner, store | ✅ implemented and tested |
| Text host (llama.cpp) | ✅ verified end to end |
| Speech hosts (sherpa-onnx TTS + online/offline STT) | ✅ real-model round trip; service returns final STT only |
| Remote tools | ✅ compiled `config_set` only; client executes validated mutations |
| Image / 3D / music / SFX hosts | ⬜ not yet |
| REST jobs/artifacts + JSON WebSocket; Python client | ✅ bounded first slice implemented |
| Unity client / binary WebSocket PCM / credits | ⬜ not yet |
| Server history / delivery reconciliation / provisioning | ⬜ not yet (delivery ledger exists in Python experiment client) |
| VRAM bin-packing scheduler | ⬜ not yet (one model per host) |

[`docs/ORCHESTRATOR_SERVICE.md`](../docs/ORCHESTRATOR_SERVICE.md) remains the
broader Unity **design reference**. Actual audio uses bounded PCM REST artifacts,
not the proposed binary WebSocket transport. Three serialized worker lanes keep
blocking inference off the HTTP event loop; native speech crashes can still kill
the whole service. Joint resource admission is not implemented.

## Model catalog

The catalog's `preferred_models` mapping supplies the current proven defaults:
`granite-4.2-8b-q4km` text, `kokoro-en-v0_19` TTS (catalog `params.speaker_id: 7`,
`bf_emma`), and `sensevoice-small` STT. Preferences apply only after modality,
quality, licence/shippable and memory-fit checks. Missing, disallowed or nonfitting
preferences fall back to highest quality, then lowest tier, among eligible models.
Custom catalogs without preferences retain that quality/fit selection.

Explicit IDs remain optional for benchmarks and also obey eligibility checks.
In particular, SenseVoice is **not marked shippable** because of its weight licence;
`ModelOrchestrator(shippable_only=True)` selects eligible STT such as Whisper
instead. Do not treat the development default as licence clearance.

| Tier | Text model | Size | Licence |
|---|---|---|---|
| 0 | `qwen2.5-1.5b-instruct-q4km` | 1.0 GB | Apache-2.0 |
| 1 | `granite-4.2-3b-q4km` | 2.1 GB | Apache-2.0 |
| 2 | `granite-4.2-8b-q4km` | 5.1 GB | Apache-2.0 |
| 2 | `k2-horizon-7b-q4km` (experimental option) | 5.21 GiB | Apache-2.0 |
| 2 | `qwen3-8b-q4km` | 4.7 GB | Apache-2.0 |
| 3 | `qwen2.5-14b-instruct-q4km` | 8.4 GB | Apache-2.0 |

Horizon uses the existing `model_id` override in `ModelOrchestrator` or
`text_model` in `ServiceClient`; the interview CLI accepts
`--text-model k2-horizon-7b-q4km`. Granite remains the default. Direct
`ModelOrchestrator.ensure_model` provisions the selected weights on demand into
the same shared store. Service startup remains cached-only: provision first.
The shared text runtime is now pinned to llama.cpp **b11471**, required for
Horizon support; this changes the runtime for Granite too and requires that
build to be cached for service launches. Existing older cached builds are not
deleted. Horizon's native JSON tool format is configured in the catalog.
See [the interview comparison](../docs/HORIZON_INTERVIEW_COMPARISON.md) for its
tool-use limitations and the invalid player-agent benchmark caveat.

Speech models are CPU-only but must still fit RAM: `kokoro-en-v0_19` (TTS, 11 voices),
`piper-en-amy-low` / `piper-en-lessac-medium` (TTS), `whisper-base-en` and
`zipformer-en-streaming` (STT), `silero-vad` (endpointing).
TTS loads its model-specific catalog voice, or speaker 0 if absent/invalid.
An explicit speaker override persists for the loaded model; switching models
resets it to the new model's default, so a one-speaker Piper fallback uses 0.

## Install

```powershell
cd c:\projects\EchoCradle
python -m pip install -e .\orchestrator          # core, no heavy deps
python -m pip install -e ".\orchestrator[speech]" # + sherpa-onnx, numpy, sounddevice
python -m pip install -e ".\orchestrator[all,dev]" # service + text + speech + tests
```

Dependencies are not weights or llama.cpp provisioning. The service requires
already cached selected models and `llama-server.exe`; microphone mode also
requires cached Silero VAD. Missing assets fail rather than trigger downloads.

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

## Service quick start

```powershell
cd C:\projects\EchoCradle\experiments\llm-runtimes\introduction-test-tts-sst\src
python main.py --play --mic
python main.py --no-tts
python main.py --capabilities
```

Each command automatically starts one contained owner service, waits for its
cached profile and closes it on exit. `--capabilities` loads the profile; it is
not a cold probe. Startup model/speaker flags configure an owned service, not
gameplay requests. For standalone port 5010, secure environment token generation,
attached mode and smoke commands, see [service usage](../docs/SERVICE_USAGE.md).
Attached clients never shut down the service, although the service's same bearer
token authorizes shutdown under its default `spawned_owner=True` configuration.

## In-process host API (model experiments)

`ModelOrchestrator` remains the low-level synchronous host facade used by
in-process diagnostics and benchmarks, **not the voice experiment launch path**.
Its direct store/runtime setup may download missing assets; the service replaces
those paths with cached-only adapters. Do not use direct host calls as service
provisioning or a Unity integration entrypoint.

```python
from orchestrator import ModelOrchestrator, Modality

with ModelOrchestrator() as mo:    # always releases model subprocesses
  print(mo.capabilities())      # probe + planned choices; no downloads
  print(mo.chat([{"role": "user", "content": "Hello"}]))
  mo.speak("Hello there.", "out.wav")  # default voice, no ID required
  print(mo.transcribe("out.wav"))

  # Live microphone (optional; needs an audio device).
  from orchestrator import AudioRecorder

  mo.ensure_model(Modality.STT) # load before reading the host sample rate
  recorder = AudioRecorder(sample_rate=mo.stt.sample_rate)
  reply = mo.listen(recorder)  # default SenseVoice decodes after endpointing
```

### Streaming is synchronous, not async

`stream_chat()` and `speak_stream()` return ordinary iterators; advancing them
performs blocking inference/I/O. Model loading may block before an iterator is
returned. `stream_chat_with_tools()` blocks until the turn finishes and invokes
`on_text` on the calling thread. `speak_streaming()` synthesizes sentence by
sentence and returns a list after completion; it is not a background job.
STT partials are available only with a streaming recognizer, not the preferred
offline SenseVoice model.

```python
with ModelOrchestrator() as mo:
  for delta in mo.stream_chat([{"role": "user", "content": "Hello"}]):
    print(delta, end="", flush=True)
  mo.stream_chat_with_tools(messages, registry, on_text=print)
  for result in mo.speak_stream(iter(["Hello. ", "Welcome back."]), "voice-out"):
    print(result.path)
```

In the tool example, `messages` and `registry` are caller-provided conversation
and tool definitions. The service wraps host inference in three worker lanes;
the interview client separately coordinates generation, sentence TTS and playback.

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
| `planner.py` | eligible catalog preference, or best quality/fit fallback |
| `tasks.py` | requests, priorities, states, artifacts |
| `tools.py` | generic tools the model can call (JSON config read/write) |
| `streaming.py` | the incremental sentence splitter (LLM → TTS bridge) |
| `hosts/text.py` | llama.cpp `llama-server` lifecycle and chat |
| `hosts/speech.py` | sherpa-onnx TTS, streaming and offline STT |
| `orchestrator.py` | synchronous in-process host facade |
| `service_protocol.py` | exact bounded JobSpec, envelopes and compiled tools |
| `service.py` | cached-only profile, auth, REST/JSON WebSocket, jobs and worker lanes |
| `client.py` | synchronous remote API, owned/attached lifecycle and PCM artifacts |

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
- **Joint portfolio selection is design only.** Choosing the best jointly fitting
  text/speech/image/3D portfolio with a Unity VRAM reserve is not implemented.
  Today's planner checks each model independently against the probe's existing
  reserve/slack budget; it does not guarantee combined residency or game headroom.
- **Tool calling needs the right prompt/schema shape.** Instruction competition
  can suppress calls. Splitting extraction/persona was a historical finding;
  the current interview uses one call with short prompts and field meanings — see
  [`experiments/llm-runtimes/introduction-test-tts-sst/README.md`](../experiments/llm-runtimes/introduction-test-tts-sst/README.md).
- **Reasoning models need `enable_thinking: false`** for short turns, or they
  return empty content. Recorded per model in the catalog.
- **Bounded service, not the full Unity design.** PCM uses REST artifacts; there
  are no binary WebSocket credits, server history/delivery reconciliation,
  provisioning or Unity client. Only compiled `config_set` is remotely exposed.
- **Final-only service STT.** Offline SenseVoice has no partials; this client
  does not expose partials from online recognizers either.
- **Native crash risk.** Text/TTS/STT lanes are serialized threads, not isolated
  supervised hosts; native speech failure can terminate the entire service.
- **`llama-server` is spawned per text model.** Router mode (multi-model
  residency) is not wired up yet.
