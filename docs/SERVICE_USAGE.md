# Local service and voice experiment usage

**Status:** implemented bounded slice, provisioning updated **2026-10-24**. Source of truth:
[`service.py`](../orchestrator/orchestrator/service.py),
[`client.py`](../orchestrator/orchestrator/client.py) and
[`service_protocol.py`](../orchestrator/orchestrator/service_protocol.py).
The broader [Unity service design](ORCHESTRATOR_SERVICE.md) is a design reference,
not this slice's wire contract. The [Unity client](../Unity/README.md) is
implemented, with typed canonical jobs/tool outcomes and passing offline
EditMode transport tests. Python's application entry is
`orchestrator.client.ServiceClient`; import it explicitly with the `service`
extra installed. Importing `orchestrator` alone does not require client extras.

## Install and prerequisites

From the repository root, using the project's Python environment:

```powershell
cd C:\projects\EchoCradle
python -m pip install -e ".\orchestrator[all,dev]"
```

This installs package dependencies, **not model weights or llama.cpp**. Service
startup automatically downloads missing selected text/speech weights and the
configured llama.cpp runtime into the shared store
(`%LOCALAPPDATA%\EchoCradle\models`; `ECHOCRADLE_HOME` overrides the root).
The service uses an exclusive per-store writer lock; a second service using the
same store fails with `STORE_BUSY`. Provisioning runs off the HTTP event loop;
readiness waits for downloads and model loading. Failed provisioning fails startup
with `STARTUP_FAILED`; there is no offline/cached-only option or separate
provisioning endpoint. This does not provision the experimental visual stack.

### External configuration

The service accepts `--runtime-config PATH` for native engine deployments and
`--model-catalog PATH` for model descriptors/preferences across text, TTS and STT.
Alternatively set `ECHOCRADLE_RUNTIME_CONFIG` and `ECHOCRADLE_MODEL_CATALOG`;
owned Python/Unity service processes inherit these environment variables.
Explicit CLI paths override environment paths; otherwise packaged JSON is used.
Configuration is trusted local operator input, never a job field or model output.
Overrides replace the complete file and require a service restart.

The default native runtime is pinned in `orchestrator/runtimes.json` to llama.cpp
`b11471`, variant `win-cuda-12.4-x64`. Its cache is now
`models/llama.cpp/b11471/win-cuda-12.4-x64/`, including executable and DLLs.
Old flat version directories are not searched; missing variant deployments are
downloaded automatically. Changed selections are provisioned on the next startup.
See [configuration details](../orchestrator/README.md#external-model-and-runtime-configuration).

Default catalog preferences, subject to licence, individual memory fit and
eligibility, are Granite `granite-4.2-8b-q4km`, Kokoro `kokoro-en-v0_19` (speaker
7, `bf_emma`) and SenseVoice `sensevoice-small`. SenseVoice is offline/final-only
and **not marked shippable**. `--shippable-only` changes startup eligibility, not
licence clearance for the development default. Any newly selected model must
be anonymously downloadable. Microphone mode additionally needs the local cached
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

# Voice profile: provisions/loads it, prints capabilities and closes it.
python main.py --capabilities

# Text profile only; provisions missing text weights/runtime.
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
for the configured profile to provision and load. First launch needs network
access for missing assets; existing deployments are reused.
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
process (`workers=1`), no reload, and four serialized worker lanes (text, TTS,
STT and optional visual work).

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

Experimental cached character generation is also available: `image` →
`image_to_3d` → `animate_3d`, with PNG/GLB artifact handoffs. Enable it with trusted
server-owned `--visual-config` or owned-client `visual_config`; this is not a job
field. See [visual API and Python examples](VISUAL_GENERATION_API.md) for the
complete staged contract and remaining release limitations. Text/voice startup
does not require visual workers. Shippable-only mode rejects these experimental
stages, whose full licensing audit is incomplete.

Every REST route and WebSocket upgrade requires `Authorization: Bearer ...`.
Host must match the loopback listener; browser origins are rejected by default.
JSON is UTF-8, capped at 64 KiB, with duplicate keys, non-finite values, excessive
depth and unknown spec fields rejected. No compatibility translation is provided.

| Route | Actual behavior |
| --- | --- |
| `GET /v1/health/live` | Instance/protocol identity; not proof models are ready. |
| `GET /v1/health/ready` | 200 when the entire configured profile is ready; otherwise 503, state and startup error. |
| `GET /v1/capabilities` | Planned/actual models, kind readiness, limits; `cached_only=false`, `features.provisioning=true` for text/speech/runtime startup. Reading this route does not itself trigger downloads; visuals remain cached-only. |
| `POST /v1/sessions` | 201; accepts only `tools` (native function declarations, default `[]`). Returns session ID/events path. |
| `DELETE /v1/sessions/{id}` | Cancels session jobs and closes its socket. |
| `POST /v1/jobs` | 202 after validation/quota admission; inference runs in a worker lane. |
| `GET /v1/jobs/{id}` | Canonical snapshot, shared with submit/cancel and `job.state`: version, instance/session/job/turn identity, kind, state, revision, `host_pending`, result/error and `selected_model` diagnostics. |
| `POST /v1/jobs/{id}/cancel` | Same canonical snapshot; logical cancellation, native work may still be pending. |
| `POST /v1/artifacts` | 201; raw PCM16 little-endian mono, `Content-Type: application/octet-stream`, `X-Sample-Rate: 16000`, 1..480,000 samples (30 s / 960,000 bytes). Returns artifact ID. |
| `GET /v1/artifacts/{id}` | Raw PCM, sample rate/count, SHA-256 and ETag headers. Not WAV; no range contract. |
| `DELETE /v1/artifacts/{id}` | Release published PCM storage; repeated deletion returns 404. The client attempts deletion after verifying TTS; failed cleanup does not discard usable audio. |
| `POST /v1/control/shutdown` | 202 drain when configured `spawned_owner=True`; same service bearer token authorizes it. |

### Exact `JobSpec`

Only these fields are accepted; `kind` is required:

| Field | Default / restriction |
| --- | --- |
| `kind` | `text`, `tts`, `stt`. Other kinds, including the removed `dialogue`, rejected. |
| `session_id`, `turn_id` | Optional UUIDs; a turn requires a session. |
| `messages` | `[]`; at most 64 objects with exactly `role` and `content`. Roles: `system`, `user`, `assistant`; content ≤16,384 characters. |
| `text` | Optional nonempty string; ≤300 characters for TTS, otherwise ≤16,384. |
| `input_artifact_id` | Optional UUID; required for STT, forbidden for other kinds. |
| `max_tokens` | 512; integer 1..512. |
| `temperature` | 0.7; finite number 0..2. |
| `deadline_ms` | 30,000; integer 1,000..120,000, including queue time. |
| `idempotency_key` | Optional nonempty string ≤128 characters, scoped to session/instance. |
| `streaming` | `false`; boolean, only text. Requires a negotiated session socket. |
| `json_schema` | Optional schema object, text jobs only (streaming or not). Valid JSON Schema, ≤16 KiB; references are rejected. Grammar constrains generation; bounded final JSON values (object, array or scalar roots) are validated. Tool-enabled sessions cannot combine it with tools. |

`text` requires messages or text; supplied text is appended as a user
message. There is no automatic STT → text → TTS voice-pipeline job.
TTS requires text, no messages or streaming. STT requires an uploaded
PCM artifact, no text/messages/streaming; submission consumes the input artifact.
TTS/STT require the voice profile. Text jobs in a session with permitted tools
also require a negotiated socket, even if `streaming=false`.
`json_schema` with a tool-enabled session fails `INVALID_SPEC` rather than
silently bypassing tools. Request/event envelopes remain JSON objects even when
generated JSON has an array/scalar root; text results contain the serialized
JSON string, not a decoded application value.

Example accepted standalone text submission body:

```json
{"kind":"text","text":"Tell me about this town.","max_tokens":128,"temperature":0.7,"deadline_ms":30000,"streaming":false}
```

There are **no `input`, `output`, `seed`, `model_id` or speaker job fields**,
and session `delivery_mode` is removed. Unknown fields are rejected, not translated.
Text results contain `text` and `calls`; TTS results contain
`artifact_id`, `sample_rate`, `sample_count`, `sha256`; STT results contain `text`.
Each text call record contains `id` (model-call identity, possibly null),
`dispatch_id` (nullable UUID), `name`, `arguments`, `result`, `outcome`
(`rejected`, `completed`, `unknown`) and `result_delivered`. Result and complete
REST/WebSocket envelope byte budgets are checked before declaring success.

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
Socket `tool.call.call_id` and `tool.result.call_id` are dispatch UUIDs, **not**
model-call IDs. Final job snapshots include rejected attempts that never reached
the client and interrupted/unknown attempts, not just locally dispatched calls.

### Python application client

Connection ownership is explicit:

```python
import threading

from orchestrator.client import ServiceClient, ServiceClientError
from orchestrator.client_types import Capabilities, Message, TextResult

messages: list[Message] = [{"role": "user", "content": "Tell me about this town."}]
cancellation = threading.Event()
with ServiceClient.start_owned(profile="text") as client:
  capabilities: Capabilities = client.capabilities()
  try:
    result: TextResult = client.stream_chat(
      messages, on_text=lambda delta: print(delta, end="", flush=True),
      cancellation=cancellation, timeout=30,
    )
    if result.state == "succeeded":
      print(result.text)
    else:
      print("Cancelled", result.job_id)
  except ServiceClientError as error:
    print(error.code, error.status, error.retryable, error.job_id, error.phase)
```

Use `ServiceClient.attach("http://127.0.0.1:5010")` to attach with the inherited
environment token; an explicit `token` may instead come from a trusted secret
source. Attachment never acquires process ownership or changes startup policy.
`start_owned(profile="text" | "voice", ...)` waits for automatic provisioning
and readiness, and owns shutdown. Unity's `--text-only` launch selects `text`
instead of `voice`, without speech jobs or audio-device initialization. The
separate `visual` profile runs configured cached visual workers without loading
text/speech; it does not provision them or the visual stack.

`chat(messages, ...)`, `stream_chat(messages, on_text=..., ...)`,
`chat_with_tools(messages, registry, ...)` and
`stream_chat_with_tools(messages, registry, on_text=..., ...)` all return
`TextResult`. Streaming methods are blocking callback APIs, **not iterators**.
The result contains terminal `state`, authoritative final or partial `text`,
`job_id`, nullable `turn_id` and immutable tuple `calls`. Successful and confirmed
cancelled text turns return typed outcomes; failed jobs/transport/callbacks raise
`ServiceClientError`, optionally carrying `partial_result`. Never treat partial
text or a cancelled turn as successful completion. No `should_stop` predicate
is accepted by this application API: pass `cancellation=threading.Event()` and
set it from any thread. Direct in-process hosts have a different contract.

Types live in `orchestrator.client_types`: `Message`, `TextResult`, `ToolOutcome`,
`Capabilities`, `KindCapability`, `PcmCapability` and the `ToolRegistry` protocol.
The package root re-exports result/capability types without client imports;
root `ToolRegistry` remains the concrete implementation from `orchestrator.tools`.
The registry protocol requires `specs() -> list[dict[str, Any]]` (native function
declarations) and `invoke(name, arguments) -> Any` (bounded JSON result).
The concrete registry is compatible; applications may supply their own.
Declarations are validated before creating a session; arbitrary validated names
are supported, not just `config_set`. Tool choice is automatic, never required.
Tool turns currently fix `max_rounds=4`; other values fail `UNSUPPORTED_OPTION`.

`ToolOutcome` separates nullable `model_call_id` and `dispatch_id`, immutable
`arguments`/`result`, server `outcome`, `result_delivered` and `executed_locally`.
`completed` means the service received a result, **not successful gameplay**;
`result_delivered` means a response was observed to a model continuation containing
the result, **not registry receipt or a socket send**. Rejected attempts can have
their error result delivered too. Failed/empty continuations conservatively leave
delivery unconfirmed; this flag does not prove the model acted on the result.
Local execution evidence is independent and can be present for an unknown
outcome or a missing final server record. An immediate cancellation snapshot may
lack attempts while native work remains pending; Python merges local calls and
Unity retains `LocallyExecutedDispatchIds` separately. Cancellation/timeout never undo side
effects; keep implementations short, idempotent where appropriate, and revocable.

Exceptions preserve sanitized `code`, nullable HTTP `status`, `retryable`,
nullable `job_id`, `phase` and optional `partial_result`; they do not expose
arbitrary remote prose/transport exceptions. Service application, routing and
unexpected errors share a bounded `error` envelope (`code`, `message`,
`retryable`, `job_id`). Nonapplication HTTP/transport/protocol failures remain
explicit client errors. `ServiceCancelledError` is available for cancellation
errors outside a confirmed typed text result.

Text callbacks and `registry.invoke` run only on the calling thread.
Callbacks must hand off work immediately, not wait for synthesis/playback.
The interview does so with a separate speech worker. Queued text is bounded by
the service's per-turn token/output limits; PCM/playback remain serialized.
Plain `chat` and `stream_chat` remain available, including optional response
schemas for applications that explicitly need structured text. The interview
does not use a response schema or ask for JSON speech.

#### Deadlines, cancellation and artifact ownership

REST defaults to `deadline_ms=30000` including queue time. Python submissions
use 120,000 ms; text methods separately accept `timeout` in `(0, 120]` seconds
(default 120), with one monotonic total budget across session/negotiation,
admission, polling/streaming and ordinary cleanup. Transport waits are bounded
to the remaining budget and at most five seconds, not reset indefinitely by
incoming bytes. Startup/readiness and owner shutdown have separate budgets.
An expired budget or failed cancellation reconciliation raises an explicit
error; the client does not invent confirmed remote cancellation. Best-effort
cancel cleanup can continue on a detached bounded thread after return without
replacing the primary failure or extending the caller's operation wait.

The watcher can revoke/cancel remote work while user code is running, but Python
cannot forcibly interrupt a synchronous callback/tool. Such code may overrun
the total budget and has no hard return-time guarantee. An already-started
side effect can still finish after cancellation; handle revocation in the tool.

`client.tts.synthesize_samples(sentence)` returns `(float32_samples, sample_rate)`
and chunks within a 2,400-character / 120-second combined budget, including
artifact transport and best-effort deletion. `transcribe_samples(samples,
rate=16000)` returns final `str` within its 120-second transport/inference budget;
`transcribe(wav_path)` first validates/downmixes/resamples a bounded PCM16 file.
No service STT partial callback is supported. Verified TTS audio survives failed
deletion. Python and Unity delete unconsumed STT uploads on definite validated 4xx
or `503 NOT_READY` admission rejection (Python also handles local validation
failure), not arbitrary 5xx, malformed errors or ambiguous transport failures;
TTL handles uncertain ownership. Admitted STT
uploads are consumed by the service. Do not retry mutations blindly after an
unknown outcome.

## Bounds, lifecycle and limitations

- At most four sessions, one active job/session, 16 queued/active jobs including
  native-cancel-pending work, 256 retained jobs. Text/TTS/STT each have one worker;
  one lane per kind. Worker lanes are threads, **not crash
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
  The client attempts to release verified TTS artifacts immediately; completed job records
  remain subject to the retention quota.
  The shared installed model store persists; generated service PCM does not.
- States: queued/running/waiting_tool → succeeded/failed/cancelled. Logical cancel
  revokes delivery immediately, but `host_pending` stays true until native work
  returns. An in-flight TTS call is not forcibly interruptible; late audio is
  discarded. Do not equate terminal job state with freed host resources.
- The Python client API is synchronous/blocking despite remote worker execution;
  text callbacks/tools run on the calling thread, with independent socket-reader,
  heartbeat and cancellation threads. Do not call it on a UI/audio callback.
- No server history/delivery reconciliation, automatic visual provisioning,
  joint VRAM/RAM admission, game telemetry or music/SFX. Image/3D/animation are
  opt-in cached experimental stages, not shipping-cleared features. Generic tools provide a
  validated client action boundary, not a durable gameplay/action ledger.
  Portfolio revision 1 is not proof of a jointly feasible portfolio.
- STT receives completed audio and returns final text only, including when using
  an online recognizer internally. SenseVoice is offline; recognition partials
  are not exposed by this service/client. Microphone barge-in uses local VAD.

## Real-model smoke and evidence

The 2026-10-10 hardening is covered by offline service/client boundary tests and
the passing Unity EditMode suite documented in [Unity validation](../Unity/README.md#validation).
Final Python integration audit: **369 orchestrator tests passed; 122 interview
tests passed, 1 live-model test deselected**. No C# changes were made in that audit,
so the preceding Unity batch result was not rerun.
No models, audio devices, audible playback or new player build were exercised
for this documentation/export cleanup. Final combined test totals belong to the
integration report. The runs below are **historical 2026-10-06 evidence**, not
a rerun of the changed contracts or proof of current native/audio behavior.

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