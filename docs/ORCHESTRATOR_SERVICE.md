# ModelOrchestrator service for Unity

**Status: DESIGN REFERENCE — bounded first slice implemented; full design not implemented.**  
**Date:** 2026-10-06  
**Scope:** target architecture for a separate local Python process and future
Unity client. The implementation status below is current as of **2026-10-06**;
sections 2–10 describe the broader proposed contract, not the shipped API.
For actual commands, wire fields and limitations, use
[`SERVICE_USAGE.md`](SERVICE_USAGE.md).

## 1. Decision and current baseline

Unity consumes one long-lived ModelOrchestrator service bound to
`127.0.0.1`, not a Python interpreter embedded in the game. The first slice uses
**FastAPI + uvicorn**, with **one process and `workers=1`**, no development reload.
These dependencies are declared in the package's `service` and `all` extras.
The intended full service owns catalog, provisioning, scheduling and hosts;
provisioning and joint resource scheduling are not implemented. Unity is intended
to own devices, gameplay, saves and delivery evidence; no Unity client exists yet.

REST handles health, capabilities, sessions, jobs and provisioning. A single
authenticated WebSocket per session carries ordered, full-duplex text, PCM,
tool calls, delivery feedback and cancellation. Polling remains sufficient for
non-streaming jobs; SSE is not the voice transport because voice needs traffic
in both directions. No cloud inference or cloud fallback.

### Implemented bounded first slice (2026-10-06)

- `service.py`, `service_protocol.py` and `client.py` provide authenticated
  loopback REST, bounded jobs, JSON WebSocket text/tool events, readiness,
  cancellation, instance checks and a cached-only startup profile (`text`/`voice`).
  Supported job kinds are **text, dialogue, tts, stt**. Actual `JobSpec` fields
  differ from section 4's proposed request; see the usage document.
- Three serialized worker lanes (text, TTS, STT) keep blocking host work off the
  HTTP event loop. Text owns a llama.cpp subprocess; speech uses sherpa-onnx in
  service threads. **A native speech crash can kill the whole service process**;
  lanes are not supervised host-process isolation.
- PCM is uploaded/downloaded as bounded **REST artifacts in memory**. There is
  **no binary WebSocket audio or sample-credit protocol**. STT takes a complete
  utterance and returns final text; offline SenseVoice produces no partials.
- The Python `ServiceClient` automatically spawns/owns a contained service by
  default, or attaches to an explicitly supplied loopback URL. Tokens travel via
  environment/header, not arguments. Attached clients never shut the service down.
  The current service token itself authorizes shutdown when `spawned_owner=True`
  (the CLI default); there is no separate owner-only credential.
- The voice experiment's `main.py` now uses this client. Capture, local Silero
  VAD, playback, DAC delivery ledger and history trimming remain **client-side**.
  The server has **no history mirror or delivery reconciliation**.
- Remote tools use a compiled allowlist containing **only `config_set`**; the
  service validates calls and waits for a correlated client-executed result.
  This is not arbitrary tool upload or a Unity action dispatcher.
- Models and the llama.cpp runtime must already be cached. Startup does not
  download either; missing selected assets fail. Selection still uses individual
  eligibility/fit checks, not joint portfolio admission. There is no provisioning
  endpoint, Unity resource telemetry or Unity client.
- Real-model smoke passed: `out/service_smoke_peter/report.json` in the voice
  experiment, Granite 4.2-8B / Kokoro / SenseVoice, validated config with username
  `Max`, medieval style and AI name `Peter`. This was TTS → offline WAV → STT,
  **not microphone or speaker playback validation**.

### Relationship to earlier designs

Read alongside [`ideas/model-orchestrator/`](../ideas/model-orchestrator/modelorchestrator.md)
and its [text](../ideas/model-orchestrator/text.md),
[audio](../ideas/model-orchestrator/audio.md),
[image](../ideas/model-orchestrator/image.md),
[3D](../ideas/model-orchestrator/three-d.md) and
[catalog](../ideas/model-orchestrator/catalog.md) notes.
Experiment [0107](../experiments/unity-integration/0107-unity-orchestrator-bridge/README.md)
is still inconclusive for Unity. The Python service slice has measured smoke
evidence; that does not establish the full Unity design.

For this service, replace the earlier raw-file-path handoff with opaque artifact
IDs, automatic gameplay-triggered downloads with explicit provisioning, and
independent/free-VRAM-only selection with the total-budget policy below. No
compatibility shim for old proposed endpoints is required. Image, mesh, music
and SFX remain future capabilities, not implied by the transport design.

## 2. Proposed full-design boundaries and execution

```text
Unity: gameplay + device I/O + delivery ledger + validated action dispatcher
                  | REST JSON / WebSocket JSON and PCM
                  v
Python service: auth/validation -> sessions/jobs -> bounded scheduler
                                      |               |
                              history reconciliation  model hosts
                                                      |
                                           local runtime subprocesses
```

| Owner | Responsibilities |
| --- | --- |
| Unity | Capture mic, downmix/resample input, local onset detection for immediate barge-in; output resampling/playback; text UI; game state/action validation; authoritative action and delivery ledgers; scripted fallback; persistence. |
| Service | Server-side VAD/endpointing and STT, text generation, sentence splitting/TTS, selected model portfolio, bounded queues, job/session history mirror, admission, provisioning/cache and diagnostics. |
| Host adapter | Load/unload, bounded inference, cancellation capability, peak resource profile and health. Never own Unity devices or game objects. |

Network work and inference never run in Unity's main-thread or audio callbacks.
Callbacks only exchange data with bounded preallocated rings; dispatch gameplay
actions on the game thread. The client is an engine-neutral async transport,
session/turn coordinator, PCM adapter and action dispatcher, with scene-level
presentation layered above it. No specific Unity API or package is prescribed.

The Python event loop handles transport/control only. Put current blocking
methods in bounded workers, serialize each non-thread-safe host, and marshal
callback output into bounded channels. Do not create a thread per incoming
request or treat a cancelled worker future as terminated inference. A native
crash cannot be contained by `try/except`; supervised host processes are a later
isolation improvement. The first slice must document whole-service crash risk.

## 3. Bootstrap, ownership and health

supported ownership modes:


 **Unity-spawned:** Unity launches a configured local executable with an
   inherited secret pipe/environment, not secrets in command-line arguments.
   Service binds an available loopback port and returns port, protocol version
   and `service_instance_id` over a private bootstrap pipe. Unity retains the
   process handle and owns graceful shutdown/forced termination of this process
   and its supervised children only. Never kill processes by name or port.

No process discovery by unauthenticated broadcast or scans. Do not spawn a
second service because the first is slow to load. The process takes a per-store
ownership lock; a second writer fails rather than racing downloads/cache state.

Health distinctions:

- **Live:** the HTTP control plane responds; not proof that a model can run.
- **Ready:** startup probe, configuration and scheduler are valid, and the
  configured minimum provisioned profile is loaded/warmed within its limits.
  `/health/ready` returns 503 while starting, provisioning, failed or draining.
- **Capability readiness:** each kind reports `unsupported`, `unprovisioned`,
  `cold`, `warming`, `ready` or `unavailable`, plus STT partial support and limits.
  A ready text-only service need not have voice ready. Unity checks its required
  capabilities before capture, not merely process liveness.

Models are not fetched on health checks, socket connection, mic callbacks or
gameplay submissions. Provision explicitly in a loading/setup screen; warm
already-installed models in admitted jobs. If weights are missing, fail with
`MODEL_NOT_PROVISIONED`. Report setup progress separately from gameplay latency.
All inference stays local; downloading approved weights is explicit setup-time
network access, which an offline deployment can disable entirely.

## 4. REST contract

Prefix `/v1`; JSON UTF-8. All routes, including health, require bearer auth.
Unknown fields fail validation (including `model_id` in gameplay requests).
IDs are opaque UUIDs. A successful submission returns 202 promptly after
validation/admission, with no download/inference in the request handler.

| Method and path | Contract |
| --- | --- |
| `GET /v1/health/live` | 200 if control plane live; instance/protocol IDs only. |
| `GET /v1/health/ready` | 200 or 503; startup/drain state and required-profile readiness. |
| `GET /v1/capabilities` | Supported/ready kinds, limits, formats, licence mode, selected-model diagnostics and portfolio revision. No downloads. |
| `POST /v1/provisioning` | Authenticated setup request for a server-defined `profile_id`; explicit download permission and byte ceiling; returns operation ID. Not a gameplay model selector. |
| `GET /v1/provisioning/{id}` | Progress, verification, bytes and typed failure. |
| `POST /v1/sessions` | Create session from bounded persona/context, permitted tool names, delivery mode and current game telemetry. Returns IDs, limits and WebSocket path. |
| `GET /v1/sessions/{id}` | Session state, history revision, active turn, negotiated formats; no secret. |
| `PUT /v1/sessions/{id}/telemetry` | Bounded timestamped game resource observations; acknowledgements include freshness/unknowns. |
| `PUT /v1/sessions/{id}/history` | Reconcile bounded plain-data history/delivery/action snapshot using expected revision; idle/reconciling only; 409 on conflict. |
| `DELETE /v1/sessions/{id}` | Cancel work, close streams, expire runtime context; does not delete Unity's save. |
| `POST /v1/jobs` | Submit bounded typed spec, optional session/turn IDs, seed, deadline and idempotency key; returns job ID/state. |
| `GET /v1/jobs/{id}` | State, result/artifact IDs, error, revision and selected-model diagnostics. |
| `POST /v1/jobs/{id}/cancel` | Idempotent cancel request; returns logical/host cancellation state. |
| `GET /v1/artifacts/{id}` | Authorized bounded binary download by cache ID, size/hash/content type; range support for larger future assets. No filesystem path. |
| `POST /v1/control/shutdown` | Spawned-owner credential only; drain, cancel, unload and close; forbidden to ordinary attached clients. |

First-slice job kinds: `text` and `dialogue`; standalone TTS/STT follow only as
their adapter contracts exist. Reject unimplemented modalities with
`UNSUPPORTED_KIND`. `dialogue` jobs specify text input or are allocated by
`turn.start` for voice input. Streaming output requires an attached session
socket; otherwise reject rather than silently generating undeliverable audio.

Example gameplay request (no model knobs):

```json
{
  "kind": "dialogue",
  "session_id": "6edce4a1-6632-45ca-b541-d65c40475b33",
  "turn_id": "e5c89f1e-2c30-4ba9-a0c1-1cd06cfced33",
  "input": {"text": "Tell me about this town."},
  "output": {"text": true, "speech": true},
  "seed": 42,
  "deadline_ms": 30000,
  "idempotency_key": "dialogue-17"
}
```

Model preferences, quantization, devices and speaker defaults are server policy.
Capabilities/job diagnostics report the resolved models and reasons for fallback;
gameplay chooses intent/quality constraints within advertised bounds, not model
IDs. Record seed, model/catalog/runtime revisions and execution settings for
reproduction. Seeds do not promise bitwise determinism across runtime/device
changes; include those settings in future artifact cache keys.

## 5. WebSocket protocol and ordered events

`/v1/sessions/{id}/events`, bearer auth in the upgrade header, never in the URL.
One active socket per session. `hello` negotiates exact protocol version,
PCM rates and window limits before any input is accepted. No legacy protocol
translation; incompatible clients fail clearly. Server messages include
`service_instance_id`; every turn-scoped message includes job/turn IDs.

Each direction has its own unsigned 64-bit `seq`, starting at 1 for each
connection and strictly increasing across **both JSON and binary messages**.
A single writer per direction assigns sequence numbers after scheduling.
Ordering is connection-local, not global across REST, reconnections or clients.
A gap/duplicate/out-of-order frame fails the affected stream; never guess missing
audio. REST reads are snapshots with revisions, not events in this sequence.

Example server envelope:

```json
{
  "v": 1,
  "seq": 8,
  "type": "text.delta",
  "service_instance_id": "98406968-9394-4c40-b0d9-1d018ba8d883",
  "session_id": "6edce4a1-6632-45ca-b541-d65c40475b33",
  "job_id": "440a6bf8-6e99-4111-8bfc-b7636e354814",
  "turn_id": "e5c89f1e-2c30-4ba9-a0c1-1cd06cfced33",
  "payload": {"text": "Welcome to the town. "}
}
```

| Direction | Events |
| --- | --- |
| Client → server | `hello`, `turn.start`, `audio.stream.start`, `input.utterance.end`, `audio.stream.end`, `flow.credit`, `turn.cancel`, `delivery.progress`, `delivery.final`, `text.delivery`, `tool.result`, `ping`. |
| Server → client | `hello.accepted`, `turn.accepted`, `job.state`, `stt.partial`, `stt.final`, `text.delta`, `text.end`, `audio.stream.start`, `audio.stream.end`, `flow.credit`, `tool.call`, `turn.cancel.accepted`, `history.committed`, `turn.completed`, `error`, `pong`. |

`turn.start` carries a new UUID and input type; service allocates a job and
returns `turn.accepted` with initial input credit. Send mic PCM only afterwards.
Each new utterance has a new input stream and turn ID. In the first version only
one response turn is active per session. An interrupting utterance may be
captured while cancelled native work finishes; new generation still obeys host
admission. HTTP dialogue submission and `turn.start` use the same deduplication
and state machine; do not submit the same turn by both paths.

### Binary PCM: exact framing

One WebSocket binary message = a **64-byte header** plus signed **PCM16,
little-endian, mono** samples. No WAV, base64, compression or floats on the wire.
Header multi-byte integers are little-endian; UUIDs use canonical 16-byte UUID
octet order, not platform-specific GUID memory layout.

| Offset | Bytes | Field |
| --- | --- | --- |
| 0 | 4 | ASCII magic `ECAP` |
| 4 | 1 | Protocol version = 1 |
| 5 | 1 | Direction: 1 mic input, 2 synthesized output |
| 6 | 2 | Reserved flags = 0 |
| 8 | 8 | Connection-direction `seq` shared with JSON |
| 16 | 16 | `turn_id` UUID |
| 32 | 16 | `stream_id` UUID |
| 48 | 8 | `sample_offset` from stream start, at wire rate |
| 56 | 4 | `sample_count` |
| 60 | 4 | `sample_rate_hz`, equal to negotiated stream rate |
| 64 | `2 * sample_count` | PCM samples |

`audio.stream.start` JSON precedes binary and supplies stream/turn/job IDs,
direction, rate, channels=1, format=`pcm_s16le`. Output streams are **one per
sentence**, with `sentence_id`, exact text span and sentence order. All offsets
are contiguous; count/length/rate/IDs must match, otherwise fail the stream.
Stream IDs are never reused. A binary frame is at most 20 ms of PCM; receivers
use the actual count, not an assumed fixed packet length.

Proposed baseline: input 16,000 Hz and output 24,000 Hz; advertise supported
rates before negotiation. Unity downmixes and statefully resamples the device
rate to input wire rate; service resamples to STT's actual model rate as needed.
Service converts native float TTS to clipped PCM16 and statefully resamples to
output wire rate. Unity resamples output to its device rate and maps output
device positions back to wire-sample offsets for delivery. Do not reinterpret
sample rates or reset resamplers at packet boundaries. Devices/rates can differ;
report actual negotiated/native rates in diagnostics.

`input.utterance.end` seals input with `stream_id`, last seq, final sample count
and reason (local endpoint, push-to-talk or server endpoint acknowledgement).
Server VAD may detect an endpoint and request sealing; accept the finite
already-in-flight tail, then finalize once all sealed samples arrive. Offline
STT decodes only now. `audio.stream.end` declares producer completion and total
samples (including an explicit zero-sample stream); it is **not** an assertion
that the listener heard them. `text.end` similarly means generation finished,
not text delivery. Cancellation ends streams with `reason=cancelled`; no late
binary data may become playable.

### Bounded buffering and backpressure

Proposed initial limits, configurable downward and returned by `hello`:

| Resource | Hard limit / behavior |
| --- | --- |
| JSON | 64 KiB/message, bounded nesting/strings/arrays; tool args/results ≤ 16 KiB. |
| Mic | 20 ms/frame; 500 ms queued PCM per endpoint; utterance ≤ 30 s / 960,000 PCM bytes at 16 kHz. |
| Playback transport | 500 ms pending wire PCM per endpoint; 250 ms credit grants. Driver/device queues are additionally bounded by the Unity adapter. |
| Sentence synthesis | One native synthesis at a time; at most two pending sentences; ≤ 300 characters and 20 s audio per sentence. Chunk long clauses or fail on the cap. |
| Control | Separate 64-message control queue with ≤ 256 KiB total; reserve slots for cancel/error/tool results. Coalesce telemetry/progress. |
| Sessions/jobs | At most 4 sessions, 16 queued jobs total (including blocked), 1 response/session, 1 text generation and 1 native TTS call globally to start. |
| Text/history | Max context 4,096 model tokens including prompt/tools/output reserve; output ≤ 512 tokens; history snapshot ≤ 256 KiB, bounded turns. |

The 64 KiB JSON limit applies to socket events and ordinary REST bodies; the
history reconciliation endpoint alone permits a 256 KiB snapshot. Enforce both
byte and model-token limits after tokenization, reserving output/tool-round space;
reject over-limit history or explicitly compact it at an idle reconciliation
boundary rather than silently overflowing/truncating an active turn.

Binary sending requires receiver-issued **sample credits per stream**. Credits
count outstanding samples, not messages; receiver returns them only when the
bounded stage releases capacity. Input releases after STT/VAD consumption or
bounded offline-utterance storage. Output releases after PCM leaves the playback
ring toward the bounded device queue, **not** as proof of DAC delivery. JSON
`flow.credit` includes stream ID and cumulative granted samples; cumulative
counters avoid accidentally granting capacity twice. Sender enforces outstanding
samples and receiver rejects excess. End/cancel control never requires PCM credit.

Always run the socket reader even when inference/playback is busy. Prioritize
control ahead of unsent bulk data; wire seq is assigned at actual send time.
Existing sent bytes cannot be overtaken, so small PCM frames and capped transport
write buffers are essential. Cancellation also stops Unity playback locally,
without waiting for a socket acknowledgement.

If downstream is full, stop scheduling TTS, then backpressure token consumption;
bound the sentence splitter and generated text too. If a host cannot pause,
cancel/fail rather than accumulate output. **Never block an audio callback.** If
mic ingress has no credit and its ring fills, seal/fail with `INPUT_OVERRUN` and
ask for repetition; never silently drop samples and transcribe a damaged utterance.
A stream stalled for 5 s fails with `SLOW_CONSUMER`; control-queue overflow closes
the socket after best-effort error. Bound WebSocket-library and OS-facing buffers
as well as application queues; credits alone do not bound native allocations.

## 6. Voice turns, cancellation and delivery reconciliation

Voice flow: Unity capture → service endpoint/STT → final transcript → text
generation → sentence splitter → serial TTS → PCM → Unity playback. Streaming
STT can emit `stt.partial` during speech, followed by a final transcript. The
default **offline SenseVoice must advertise `partials=false` and emit only
`stt.final`** after endpointing; uploading PCM incrementally does not make its
recognizer streaming. Genuine online STT requires an eligible online model.
Do not market sentence TTS as token-level native synthesis.

Barge-in procedure:

1. Unity confirms local speech onset, preserves a bounded pre-roll (initially
   300 ms), immediately stops old playback and freezes its delivery ledger.
2. Send `turn.cancel` for the old turn and `delivery.final`; start the new input
   turn without flushing captured speech. Mark the old turn locally revoked.
3. Service invalidates old stream IDs, removes pending sentences and closes text
   generation transport where possible. Queued work is removed; late callbacks
   and native PCM are discarded by turn/job identity.
4. Every Unity receive/playback stage rejects revoked/non-current turn IDs,
   including already-queued PCM. New speech can be captured/recognized while an
   old non-interruptible TTS call completes, but that call retains its host lane
   and resource reservation until it really returns.
5. Reconcile the old turn before generating a response to the new final input.
   A missing final delivery report waits at most 2 s, then uses the last confirmed
   lower bound; flag uncertainty. Never commit generated-but-undelivered text.

Logical cancellation is immediate eligibility revocation, not guaranteed native
termination. Current text cancellation interrupts body reads, not connection/
header setup or an already executing tool; stopping the connection need not stop
the underlying runtime immediately. Current native TTS cannot be forcibly
cancelled safely in its calling thread. Track `host_pending=true` separately
from client-visible `cancelled`; release resources only on actual completion or
supervised process death. Do not promise a cancellation latency without testing.

### Delivery ledger: Unity is authoritative

Keep separate generated, synthesized, transmitted, queued and **DAC-delivered**
positions. `delivery.progress`/`delivery.final` contain stream/sentence/turn IDs,
`heard_samples`, total samples, final flag and measurement quality. Only advance
heard position when samples' device presentation time has passed, accounting
for driver latency. Neither network receipt nor dequeue into a device buffer
counts as heard. DAC progress is evidence of device delivery, not proof a person
was listening; muted/failed output cannot count as speech delivery.

```json
{
  "v": 1,
  "seq": 21,
  "type": "delivery.final",
  "session_id": "6edce4a1-6632-45ca-b541-d65c40475b33",
  "job_id": "440a6bf8-6e99-4111-8bfc-b7636e354814",
  "turn_id": "e5c89f1e-2c30-4ba9-a0c1-1cd06cfced33",
  "payload": {
    "stream_id": "784ba055-cb76-4418-aade-dc8d32b0bb92",
    "sentence_id": 1,
    "heard_samples": 12000,
    "total_samples": 48000,
    "measurement": "dac_timed",
    "reason": "barge_in"
  }
}
```

Validate monotonic counts ≤ transmitted/total samples; duplicate reports are
idempotent. Feedback remains acceptable for a recently revoked turn during
reconciliation, unlike new audio/tool requests. If the chosen Unity playback
adapter cannot obtain trustworthy DAC timing, label reports `estimated` and use
a conservative latency-corrected lower bound (or zero); do not claim exact timing.
Validate the adapter against a real device in the implementation milestone.

Commit complete heard sentences; for a partially heard sentence, map the sample
fraction to an **approximate word prefix** unless real word alignment exists.
Preserve an explicit `partial_text_approximate` flag and interruption note, not
a fabricated verbatim transcript. Trim undelivered suffixes in the service's
history mirror and acknowledge a new history revision. Reconcile tool execution
records independently: an applied game action stays applied even if its verbal
confirmation was interrupted.

Session delivery policy is explicit: `speech`, `text` or `both`. Text generation
deltas are provisional. In text mode, `text.delivery` reports the span actually
presented (not merely received). In speech mode history uses speech delivery;
on interruption retract/mark the unspoken UI suffix. In `both`, store displayed
and spoken spans separately and construct next-turn context with explicit
distinction rather than claiming the displayed suffix was heard. `turn.completed`
is emitted only after delivery reconciliation; a job's successful inference
alone does not finalize conversation history.

No echo cancellation is promised in the first slice. Use headphones or
push-to-talk; speaker echo must not be mistaken for player barge-in. AEC is a
separately measured device/client feature.

## 7. Remote tools and authoritative gameplay

Unity exposes a **compiled allowlist** of named actions and bounded schemas.
Session creation selects a subset of this registered contract; it cannot upload
new executable tools. Service advertises those schemas to the model, buffers
complete tool arguments, validates them, and emits `tool.call`. Unity validates
again against authoritative game state, permissions and expected state revision,
then executes through its own dispatcher. Never expose arbitrary C#, reflection,
console commands, Python, shell, generic file write or arbitrary Unity commands.

```json
{
  "v": 1,
  "seq": 12,
  "type": "tool.call",
  "service_instance_id": "98406968-9394-4c40-b0d9-1d018ba8d883",
  "session_id": "6edce4a1-6632-45ca-b541-d65c40475b33",
  "job_id": "440a6bf8-6e99-4111-8bfc-b7636e354814",
  "turn_id": "e5c89f1e-2c30-4ba9-a0c1-1cd06cfced33",
  "payload": {
    "call_id": "a13d8e3a-79a5-462e-8cb9-2d74d42f6b82",
    "name": "npc.set_known_fact",
    "arguments": {"npc_id": "blacksmith", "fact_id": "player_visited"},
    "expected_game_revision": 17,
    "timeout_ms": 5000
  }
}
```

`tool.result` echoes call/job/turn IDs, `ok`, bounded result or typed error and
the new game revision. Deadlines use a server monotonic timer beginning at send;
Unity starts its own shorter local timeout on receipt. One pending call/session,
at most 4 tool rounds/turn initially. A pending remote result yields execution
to the service job manager; it must not block the HTTP event loop.

Refactor current `ToolRegistry.invoke`/text tool loop into validated **pending
call → correlated result → continue generation**. This is more than forwarding
`on_text`. The service does not execute the existing file/config tool on Unity's
behalf. JSON config mutations, if allowed, become specific Unity actions with
schema validation and normal game transactions.

Reject unknown, late or mismatched results; record late confirmations for
reconciliation without resuming a cancelled turn. Unity deduplicates call IDs
in an action ledger and returns the prior result on retry. Timeout/disconnect
after dispatch means **outcome unknown**, not definitely unexecuted. Query/reconcile
Unity's ledger before retrying a mutation; do not promise network exactly-once
execution or undo an applied action merely because dialogue was cancelled.

## 8. Joint resources and dynamic admission

At startup, select a **jointly feasible portfolio**, not the independently
largest model for each modality. Consider required text+STT+TTS together (and
future generator lanes only if configured), preferred models after eligibility,
licence mode, quantization/offload, installed inventory and allowed setup policy.
Freeze a portfolio revision during active work. CPU speech still consumes RAM,
CPU time and workspace; GPU-less capability is conditional on those fitting.

Use **total device VRAM** as the planning accounting base:

```text
required_peak = sum(unique resident model weights)
              + sum(admitted per-task bounded KV/context allocations)
              + peak overlapping temporary/native workspaces
              + Unity engine reserve
              + other driver/runtime context overhead
              + safety/fragmentation headroom
admit only if required_peak <= total_vram
```

Each resource profile names measured versus estimated terms, context size,
resolution/duration, dtype/offload and runtime version. Do not count a measured
all-inclusive model peak again as both weights and KV/workspace: decompose it or
use one conservative inclusive term. Co-resident weights are counted once;
concurrent task KV/workspaces are counted according to actual overlap. Track
host RAM equivalents, model mappings, resampling/utterance buffers and CPU-thread
budgets with OS/game reserves, not just GPU fit.

Unity periodically reports current/peak game GPU/RAM use where observable,
scene/load mode, expected near-term growth and timestamp. Engine reserve is at
least the configured floor and observed game requirement plus declared growth;
**observed use is inside that term, not added twice**. Unknown/stale telemetry
(older than 5 s initially) uses a conservative reserve and pauses discretionary
admission. Windows/WDDM may not expose reliable per-process GPU attribution;
mark uncertainty and use conservative device observations, not invented PID data.

Device free/used samples are a **runtime pressure sanity check**, not a second
full-portfolio budget. Never subtract Unity/resident weights from already-free
VRAM and compare the complete portfolio against that remainder. If making an
incremental load check against measured free, compare only incremental allocations
and not-yet-reflected reserves/headroom, without re-subtracting existing use.
Unattributed device pressure can veto admission even when the estimate fits.

Before every load/job, admit against bounded max context/output/duration and
current reservations/telemetry. Initial concurrency is deliberately low. When
pressure rises, stop background admission, bound queues, then reject with
`RESOURCE_BUSY`/`RESOURCE_EXHAUSTED` or use a preapproved scripted fallback.
Do not hot-swap/unload a model underneath active or native-cancel-pending tasks.
Replan only at an idle boundary; serialize optional heavy generators instead of
forcing all modalities resident. If even the smallest required portfolio cannot
fit, advertise that capability unavailable.

Profiles are estimates, **not OOM guarantees**. Monitor measured peaks, surface
allocation failures, update profiles conservatively and preserve game fallback.
Neither today's per-model planner nor a probe tier proves simultaneous fit.
Disk remains scarce: provision only approved catalog files, estimate temporary
and final disk needs, evict only unpinned idle entries under byte quotas, clean
intermediates, and return `DISK_BUDGET_EXCEEDED` if safe provisioning cannot fit.
Do not download every modality or silently change quality because of disk space.

## 9. Errors, lifecycle and recovery

REST error body and WebSocket `error.payload` use the same shape:

```json
{
  "error": {
    "code": "RESOURCE_BUSY",
    "message": "Text host is occupied by cancelled native work.",
    "retryable": true,
    "retry_after_ms": 500,
    "request_id": "8b0f71b0-aa8c-4835-87a7-e96b5f1adfb4",
    "job_id": null,
    "details": {"resource": "text", "portfolio_revision": 3}
  }
}
```

HTTP mapping: 400 invalid protocol/JSON; 401/403 auth/permission; 404 unknown or
expired ID; 409 state/history/idempotency conflict; 413 oversized payload;
422 invalid spec/schema/unsupported kind; 429 queue/concurrency saturation;
503 not-ready/resource pressure/unprovisioned capability; 500 internal failure.
WebSocket auth fails before upgrade; protocol/payload violations close with
1002/1009, policy violations with 1008, internal failure with 1011. Error details
must not contain secrets, raw paths, prompts or native tracebacks.

Service: `starting → ready → draining → stopped`, with degraded capability health.
Session: `created → connected → active → reconciling → connected`, or
`disconnected/closing → expired`. Job: `queued → warming → running ↔ waiting_tool
→ awaiting_delivery → succeeded`, or `failed/cancelled`. Cancellation can occur
at any nonterminal state; physical host cleanup is tracked independently.
All transitions have revisions; queue/tool/delivery/generation deadlines are
finite and separate. Job records expire after 10 minutes and disconnected
sessions after 60 s initially; leases/artifact pinning must not leak disk forever.

- Ping every 5 s; after 15 s without response mark transport lost. On disconnect,
  stop local audio and cancel session work immediately. Service uses the last
  delivery lower bound; never continues unobservable game actions. Persist Unity's
  delivery/action state before reconnecting. Detached noninteractive REST jobs
  may continue only when explicitly requested and already admitted.
- On reconnect to the **same instance**, attach a new seq space, fetch snapshots
  and reconcile history/action/delivery revisions before starting a turn. Do not
  replay old PCM or automatically resend ambiguous tools. No event replay guarantee
  is needed in the first version; bounded job snapshots are the recovery mechanism.
- On service crash/restart, `service_instance_id` changes and old session/job IDs
  become invalid. No transparent resurrection of inference or transient mic data.
  Unity recreates a session from its plain-data save/history and resolved action
  ledger. Retry only safe idempotent work with a new turn ID. Surface unresolved
  tool outcomes for reconciliation rather than assuming failure.
- Initial jobs/sessions are in memory; verified model files and completed
  content-addressed artifacts survive restart. Partial files are never published
  as artifacts. Clean orphaned temporary files within configured quotas.
- Bounded idempotency records bind key to canonical request hash and job ID within
  an instance (10-minute retention initially); reusing a key with different content
  is 409. After retention/restart, do not assume submission deduplication persists.
- Spawned shutdown stops admission, asks cancellation, waits up to 5 s for workers,
  unloads hosts, then owner terminates its supervised process tree if necessary.
  Failed shutdown cannot safely release reservations while native work is alive.

Unity owns save data: plain C# records with game state, seeds, stable content IDs,
committed history and action/delivery metadata. Never save Unity objects, service
objects, open connections or host handles. A service session UUID is a transient
reference, not the identity of an NPC or save slot.

## 10. Local security and data handling

- Bind loopback only, never `0.0.0.0`; IPv6 support must explicitly bind `::1`.
  Validate Host against configured loopback host/port to resist DNS rebinding.
  Loopback is not authentication: other local apps and browsers can reach it.
- High-entropy bearer token per installation/manual run or spawned instance;
  keep it in a restricted local credential store/private bootstrap channel.
  No tokens in query strings, URLs, logs, saves or capability responses. Native
  Unity uses upgrade headers. A future browser client needs a separately reviewed
  auth flow, not a token URL workaround.
- Reject unexpected browser `Origin` values on REST and WebSocket. CORS disabled
  by default; an explicitly configured development origin is the only exception.
  Native clients may omit Origin but must still authenticate. Token rotation
  invalidates connected sessions. Local privileged malware is outside this boundary.
- Enforce schema, byte/rate/session quotas before allocation; forbid NaN/invalid
  numbers, arbitrary callback/download URLs, filesystem paths and traversal.
  Artifact IDs resolve inside the configured cache; authorize session ownership
  and validate normalized paths/symlinks before reading internally. Future uploads
  use size-limited managed IDs, not filenames supplied as paths.
- Only server-side catalog/profile policy authorizes downloads. Verify declared
  hashes/sizes and licences; do not accept model URLs or executable plugins from
  gameplay/model output. Use shared `orchestrator.paths` store locations rather
  than duplicating weights for the service.
- Treat every generated JSON/tool argument as untrusted. Validate structured
  content before gameplay use; never execute generated code. Raw PCM/transcripts
  are not logged by default; opt-in bounded diagnostics need retention/cleanup.

## 11. Full-design milestones and acceptance gates

The bounded Python slice overlaps parts of control/text, PCM via REST, remote
`config_set` and owned lifecycle. It does **not** complete any full milestone
below: Unity transport, binary framing/credits, server delivery/history,
general gameplay actions and joint admission/provisioning remain future work.
Use [`SERVICE_USAGE.md`](SERVICE_USAGE.md) for implemented behavior, not these
target acceptance gates as claims of validation.

| Slice | Deliverable | Acceptance tests |
| --- | --- | --- |
| 1. Control + text | One-process service, auth, health/capabilities, bounded jobs, fake host then existing text adapter; thin Unity transport. | Stub tests prove fast 202/no inference in request loop, no automatic downloads, 401/Origin/Host/payload rejection, queue limits, idempotency conflict, ready distinct from live, explicit unsupported kinds. Live text smoke produces incremental ordered text without blocking rendering. |
| 2. PCM + STT | Negotiation/framing, client capture, bounded rings/credits, server endpointing and final STT. | Golden binary frames cover endian/UUID/layout/rates/offsets; malformed/gapped/oversized frames fail. Resampling preserves duration across packets. SenseVoice never emits partials; an online stub emits partial/final separately. Overrun/deadline test fails visibly rather than losing samples. |
| 3. Voice + delivery | Sentence TTS, playback adapter, turn revocation, ledger/history reconciliation. | Fake slow TTS proves bounded sentence/PCM memory; cancel discards late native output without releasing its lane early. Real-device test compares DAC timing versus queued buffers; unavailable timing is labeled estimated. Barge-in stops playback locally, preserves pre-roll, drops old PCM and retains only delivered history with approximate partial flags. |
| 4. Remote actions | Pending tools, validated Unity dispatcher, correlated results/action ledger. | Unknown tools/invalid args/revision mismatch rejected; timeout, duplicate result, interrupt and disconnect after mutation do not execute twice or silently undo state. Cancelled turns never resume on a late result. Black-screen interview slice shows voice/text and writes validated config through Unity. |
| 5. Joint admission + recovery | Measured profiles, telemetry, joint portfolio, provisioning and owned lifecycle. | Synthetic budgets prove weights/KV/workspace/reserves counted once, CPU RAM bounded, stale telemetry conservative, rejected overflow and no active hot-swap. Crash/reconnect tests reconcile ledgers, change instance ID, prevent PCM replay, clean partial files and leave user-managed processes running. Real co-residency stress measures peaks and failure behavior; estimates are never presented as guarantees. |

Keep most tests model/GPU/device-free with fake hosts/clocks and fault injection.
Provision the smallest necessary local model subset only for explicitly approved
live tests. Record observed first-text/first-audio, barge-in and queue peaks rather
than claiming latency targets are already met. Validate Editor/player parity,
audio device behavior and main-thread dispatch when a client exists; this design
makes no unverified Unity API claims.