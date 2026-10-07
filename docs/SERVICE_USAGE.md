# Local service and voice experiment usage

**Status:** implemented bounded first slice, **2026-10-06**. Source of truth:
[`service.py`](../orchestrator/orchestrator/service.py),
[`client.py`](../orchestrator/orchestrator/client.py) and
[`service_protocol.py`](../orchestrator/orchestrator/service_protocol.py).
The broader [Unity service design](ORCHESTRATOR_SERVICE.md) is a design reference,
not this slice's wire contract. No Unity client is implemented.

## Install and prerequisites

From the repository root, using the project's Python environment:

```powershell
cd C:\projects\EchoCradle
python -m pip install -e ".\orchestrator[all,dev]"
```

This installs package dependencies, **not model weights or llama.cpp**. Runtime
startup, capabilities and jobs never download models or runtime binaries. Have
the selected models and `llama-server.exe` already installed in the shared store
(`%LOCALAPPDATA%\EchoCradle\models`; `ECHOCRADLE_HOME` overrides the root).
The service uses an exclusive per-store writer lock; a second service using the
same store fails with `STORE_BUSY`. Missing selected weights/runtime fail with
`MODEL_NOT_PROVISIONED`; do not expect automatic selection of a cached substitute.
Provisioning is not a service endpoint in this slice.

Default catalog preferences, subject to licence, individual memory fit and
eligibility, are Granite `granite-4.2-8b-q4km`, Kokoro `kokoro-en-v0_19` (speaker
7, `bf_emma`) and SenseVoice `sensevoice-small`. SenseVoice is offline/final-only
and **not marked shippable**. `--shippable-only` changes startup eligibility, not
licence clearance for the development default. Any newly selected model must
also already be cached. Microphone mode additionally needs the local cached
`silero-vad\silero_vad.onnx` and a working 16 kHz input device.

## Default: the experiment owns its service

```powershell
cd C:\projects\EchoCradle\experiments\llm-runtimes\introduction-test-tts-sst\src
python main.py --play --mic
```

`main.py` automatically launches **one contained owner service**, waits for
readiness and uses `ServiceClient`; do not start another service first. On Windows
the client contains its spawned service and descendants in a kill-on-close Job
Object, including the text host's llama.cpp child. Closing the owner requests
graceful shutdown, then terminates only its owned process tree if needed.
The token is generated internally and inherited in the environment, never put
in process arguments. The listener binds `127.0.0.1` on an allocated port.

Use headphones: there is no acoustic echo cancellation. The client owns device
capture, local VAD/endpointing, playback and the existing DAC-timestamp delivery
ledger. It uploads each **complete** mono 16 kHz utterance for final STT. Native
TTS is sentence/chunk synthesis, not token-level audio streaming. Client-side
history trimming retains delivered sentences and explicitly approximate partial
word prefixes; this is not server reconciliation or exact word alignment.
Device failure can fall back to typing/silent playback; check console diagnostics.

Other launches from the same directory:

```powershell
# Typed replies, no TTS: starts only the text profile.
python main.py --no-tts

# Cached voice profile: starts/loads it, prints capabilities and closes it.
python main.py --capabilities

# Cached text profile only.
python main.py --no-tts --capabilities

# Typed replies with synthesized WAVs; playback is opt-in.
python main.py --play

# Completed PCM16 WAV input (at most 30 seconds), instead of microphone/typing.
python main.py --stt-file reply.wav
```

TTS is enabled by default, playback is disabled by default. `--no-tts` without
`--mic`/`--stt-file` chooses `text`; otherwise the experiment chooses `voice`,
which loads text, TTS and STT even if only one speech direction is needed.
`--capabilities` is **not** a cold, probe-only command: client construction waits
for the configured profile to load; it does not download anything.
Default output is the experiment's `out/config.json`, with reply WAVs and
`delivery.jsonl` alongside it. Use `--out` to choose another config file.

`--text-model`, `--tts-model`, `--stt-model`, `--speaker-id` and
`--shippable-only` are **startup policy** for an owned service, not gameplay
job fields or hot-swap controls. Do not pass them when attaching; the client
rejects supplied model/speaker overrides or `--shippable-only` in attached mode.

## Standalone service and attached experiment

In a PowerShell terminal, generate a cryptographically random token into an
environment variable without printing it. No actual secret is shown here:

```powershell
cd C:\projects\EchoCradle
$bytes = New-Object byte[] 32
$rng = [System.Security.Cryptography.RandomNumberGenerator]::Create()
try { $rng.GetBytes($bytes) } finally { $rng.Dispose() }
$env:ECHOCRADLE_SERVICE_TOKEN = [Convert]::ToBase64String($bytes)
python -m orchestrator.service --port 5010 --profile voice
```

The service prints one bootstrap JSON line (port, instance ID, protocol version),
then logs to stderr. Bootstrap is not a readiness guarantee. There is one uvicorn
process (`workers=1`), no reload, and three serialized worker lanes.

In another terminal with the **same environment token** supplied through a
trusted local environment/secret mechanism, attach:

```powershell
cd C:\projects\EchoCradle\experiments\llm-runtimes\introduction-test-tts-sst\src
python main.py --service-url http://127.0.0.1:5010 --play --mic
```

A separately opened terminal does not inherit the first terminal's newly set
variable. Arrange the matching environment securely; never put the token in
arguments, URLs, docs, logs or committed files. The client reads
`ECHOCRADLE_SERVICE_TOKEN` and sends bearer headers. An attached client cancels
its own active jobs/closes sockets on exit, but **never shuts down or kills the
standalone service**. Stop that service in its owner terminal.

**Shutdown authorization limitation:** `ServiceConfig.spawned_owner` defaults to
`True`, including this standalone CLI. The **same bearer token grants access to
`POST /v1/control/shutdown`**; there is no separate owner credential. Attached
client behavior is a lifecycle convention, not a token privilege boundary.
Programmatic `spawned_owner=False` denies shutdown, but there is no CLI flag for it.

## Implemented API (protocol version 1)

Every REST route and WebSocket upgrade requires `Authorization: Bearer ...`.
Host must match the loopback listener; browser origins are rejected by default.
JSON is UTF-8, capped at 64 KiB, with duplicate keys, non-finite values, excessive
depth and unknown spec fields rejected. No compatibility translation is provided.

| Route | Actual behavior |
| --- | --- |
| `GET /v1/health/live` | Instance/protocol identity; not proof models are ready. |
| `GET /v1/health/ready` | 200 when the entire configured profile is ready; otherwise 503, state and startup error. |
| `GET /v1/capabilities` | Cached-only status, planned/actual models, kind readiness, limits and feature flags. No downloads. |
| `POST /v1/sessions` | 201; accepts `tools` (native function declarations) and `delivery_mode` (`text`, `speech`, `both`; default `text`). Returns session ID/events path. Delivery mode does not implement a ledger. |
| `DELETE /v1/sessions/{id}` | Cancels session jobs and closes its socket. |
| `POST /v1/jobs` | 202 after validation/quota admission; inference runs in a worker lane. |
| `GET /v1/jobs/{id}` | Snapshot: state, revision, `host_pending`, result/error and selected-model diagnostics. |
| `POST /v1/jobs/{id}/cancel` | Logical cancellation; native work may still be pending. |
| `POST /v1/artifacts` | 201; raw PCM16 little-endian mono, `Content-Type: application/octet-stream`, `X-Sample-Rate: 16000`, 1..480,000 samples (30 s / 960,000 bytes). Returns artifact ID. |
| `GET /v1/artifacts/{id}` | Raw PCM, sample rate/count, SHA-256 and ETag headers. Not WAV; no range contract. |
| `DELETE /v1/artifacts/{id}` | Release published PCM storage; repeated deletion returns 404. The client deletes verified TTS artifacts immediately. |
| `POST /v1/control/shutdown` | 202 drain when configured `spawned_owner=True`; same service bearer token authorizes it. |

### Exact `JobSpec`

Only these fields are accepted; `kind` is required:

| Field | Default / restriction |
| --- | --- |
| `kind` | `text`, `dialogue`, `tts`, `stt`. Other kinds rejected. |
| `session_id`, `turn_id` | Optional UUIDs; a turn requires a session. |
| `messages` | `[]`; at most 64 objects with exactly `role` and `content`. Roles: `system`, `user`, `assistant`; content ≤16,384 characters. |
| `text` | Optional nonempty string; ≤300 characters for TTS, otherwise ≤16,384. |
| `input_artifact_id` | Optional UUID; required for STT, forbidden for other kinds. |
| `max_tokens` | 512; integer 1..512. |
| `temperature` | 0.7; finite number 0..2. |
| `deadline_ms` | 30,000; integer 1,000..120,000, including queue time. |
| `idempotency_key` | Optional nonempty string ≤128 characters, scoped to session/instance. |
| `streaming` | `false`; boolean, only text/dialogue. Requires a negotiated session socket. |
| `json_schema` | Optional object, text jobs only (streaming or not). Valid JSON Schema, ≤16 KiB; references are rejected. Grammar constrains generation; final output is validated. |

`text`/`dialogue` require messages or text; supplied text is appended as a user
message. **Dialogue is the text/tool lane, not an automatic STT → TTS voice
pipeline.** TTS requires text, no messages or streaming. STT requires an uploaded
PCM artifact, no text/messages/streaming; submission consumes the input artifact.
TTS/STT require the voice profile. Text jobs in a session with permitted tools
also require a negotiated socket, even if `streaming=false`.

Example accepted standalone text submission body:

```json
{"kind":"text","text":"Tell me about this town.","max_tokens":128,"temperature":0.7,"deadline_ms":30000,"streaming":false}
```

There are **no `input`, `output`, `seed`, `model_id` or speaker job fields**.
Text results contain `text` and `calls`; TTS results contain
`artifact_id`, `sample_rate`, `sample_count`, `sha256`; STT results contain `text`.

### JSON WebSocket and remote tools

`/v1/sessions/{id}/events` allows one socket per session. Each direction has its
own contiguous sequence starting at 1. Envelopes carry `v: 1`, `seq`, `type`,
`payload` and identity fields; the server supplies instance/session/job/turn IDs.
First send `hello` with `{}`; wait for `hello.accepted` before submitting streaming
jobs via REST. Client events: `hello`, `ping`, `turn.cancel` (payload `job_id`),
`tool.result` (payload `call_id`, `result`). Server events: `hello.accepted`,
`pong`, `job.state`, `text.delta`, `text.end`, `tool.call`, `error`.
There is no `turn.start`, binary PCM, audio-stream framing, sample credits,
delivery event or STT-partial event. Ping interval is 5 s; idle timeout 15 s.

The client supplies at most 16 native function declarations (name, description,
JSON Schema parameters), bounded to 16 KiB total. Schema references and duplicate
names are rejected. The service validates arguments; the client performs domain
validation and executes only its registered implementation, returning a correlated
result. No executable callbacks are uploaded or executed by the server. One pending
call per session, four generation rounds, at most eight calls/job, 16 KiB tool
arguments/results and a 5 s result timeout. Late/duplicate results cannot resume
cancelled work; a timeout after dispatch means the action outcome is unknown.
The client has bounded per-turn call-ID deduplication, not a durable action ledger.

### Python client with native tools

`ServiceClient.chat_with_tools(messages, registry)` returns `(text, calls)`.
`stream_chat_with_tools(messages, registry, on_text=callback, should_stop=stop)`
additionally streams spoken text and supports cancellation. Descriptions and
schemas come from `ToolRegistry.specs()` and are supplied to native tool calling
on every generation round. Tool choice is automatic, never required. Complete
calls execute through `registry.invoke()` in the client; results return to the
model for continuation. The server has no game-specific tool implementations.

Callbacks must hand off work immediately, not wait for synthesis/playback.
The interview does so with a separate speech worker. Queued text is bounded by
the service's per-turn token/output limits; PCM/playback remain serialized.
Plain `chat` and `stream_chat` remain available, including optional response
schemas for applications that explicitly need structured text. The interview
does not use a response schema or ask for JSON speech.

## Bounds, lifecycle and limitations

- At most four sessions, one active job/session, 16 queued/active jobs including
  native-cancel-pending work, 256 retained jobs. Text/TTS/STT each have one worker;
  text and dialogue share their lane. Worker lanes are threads, **not crash
  isolation**: a native crash can kill the entire service.
- JSON event queue: 64 messages / 256 KiB, 5 s slow-consumer guard. Prompt budget
  uses conservative serialized-prompt tokenization with a 4,096-token context;
  exact chat-template accounting is not implemented. Generated text ≤24 KiB.
- Server TTS ≤300 chars / 20 s per job. The client can split a sentence into
  ≤300-character whitespace chunks, bounded to 2,400 chars / 120 s total; it
  downloads and integrity-checks completed PCM artifacts, not binary socket audio.
- Artifact/input storage budget: 64 MiB, ≤256 published artifacts. Artifacts and
  completed jobs expire after 600 s; disconnected sessions after 60 s. Jobs,
  sessions, artifacts and idempotency records are **in memory**, lost on restart.
  The client releases verified TTS artifacts immediately; completed job records
  remain subject to the retention quota.
  The shared installed model store persists; generated service PCM does not.
- States: queued/running/waiting_tool → succeeded/failed/cancelled. Logical cancel
  revokes delivery immediately, but `host_pending` stays true until native work
  returns. An in-flight TTS call is not forcibly interruptible; late audio is
  discarded. Do not equate terminal job state with freed host resources.
- The Python client API is synchronous/blocking despite remote worker execution;
  text callbacks/tools run on the calling thread, with independent socket-reader,
  heartbeat and cancellation threads. Do not call it on a UI/audio callback.
- No server history/delivery reconciliation, provisioning, joint VRAM/RAM
  admission, game telemetry, general gameplay actions, image/3D/music/SFX or
  Unity client. Portfolio revision 1 is not proof of a jointly feasible portfolio.
- STT receives completed audio and returns final text only, including when using
  an online recognizer internally. SenseVoice is offline; recognition partials
  are not exposed by this service/client. Microphone barge-in uses local VAD.

## Real-model smoke and evidence

From the experiment's `src` directory, choose a **new, empty output directory**:

```powershell
python service_smoke.py --out ..\out\service_smoke_new_run
# Optional actual speaker playback; use another new empty directory.
python service_smoke.py --out ..\out\service_smoke_new_play --play
```

The script owns a voice service, checks auth/Origin/Host/payload boundaries,
idempotency, text cancellation/lane recovery, remote config calls and the real
TTS → saved WAV → final STT interview. It refuses a nonempty output directory and
writes `report.json`, validated `config.json` and audio evidence. `--play` requires
actual playback; **neither mode tests microphone capture**.

Recorded **passed** run on 2026-10-06:
`experiments/llm-runtimes/introduction-test-tts-sst/out/service_smoke_peter/report.json`.
Actual models: Granite 4.2-8B Q4_K_M, Kokoro (speaker 7, 24 kHz), SenseVoice
(16 kHz, `partials=false`). Config: username **Max**, style **medieval with
mountains and ancient castles**, AI name **Peter**. `play_requested=false`,
`speaker_playback_completed=false`, `microphone_tested=false` and
`runtime_voice_validation=false`: a successful real-model offline round trip,
**not** evidence of microphone, speaker or full-duplex voice validation.

The primary `main.py` entrypoint also completed a real streamed text/tool plus
concurrent sentence-TTS run, writing `out/service_main/config.json`. After the
artifact/housekeeping fixes, real TTS → STT returned “The service is ready.”,
text inference succeeded, and the owned service exited with code 0 with no
remaining `llama-server.exe`. Existing regression suites passed: **238
orchestrator tests and 96 experiment tests**, with one live test deselected.

Historical diagnostic scripts (`diagnostic_run.py`, `diagnose_*.py`, speech
benchmarks and hardware tests) are **in-process model/device experiments**, not
the service launch entrypoint. Their direct host paths can have different setup
and download behavior; use `main.py` for the current service experiment and
`service_smoke.py` for service smoke evidence.