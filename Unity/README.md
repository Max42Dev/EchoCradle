# Unity voice interview prototype

Unity **6000.6.3f1**, Windows x64. Open this `Unity` directory in Unity Hub,
then open `Assets/Scenes/Start.unity` and press **Play**. The scene is generated
on first import if absent. New game begins the voice interview; Load game is
intentionally disabled. Completion stops Play Mode in the editor and exits a
standalone game.

The orchestrator starts loading text/TTS/STT models asynchronously as soon as
the application opens. The menu shows preparation status; New game reuses the
loaded service (or waits for the in-progress startup). No save folder is created
until New game. Cancel returns to the menu without unloading the voice models.
Closing the application from the menu also shuts down its owned service.

## Local prerequisites

- Activate a Unity Editor license in Unity Hub.
- Use the existing Python environment with the orchestrator's voice dependencies.
- Text, TTS, STT, llama.cpp and Silero must already be cached. Startup never
  downloads models. See `../docs/SERVICE_USAGE.md` for provisioning assumptions.
- Close other owned orchestrator experiments first: the model store permits
  only one service owner. Use headphones; acoustic echo cancellation is absent.
- Allow desktop microphone access in Windows and select a mono 16 kHz-capable
  input device. Unity requests 16 kHz; unsupported devices fail rather than
  uploading incorrectly labelled audio.

## Configuration

`Assets/Data/Interview.json` contains all interview prompts,
the string-field JSON schema, UI text, generation bounds, voice settings and
save filenames. `Assets/Data/InterviewSettings.asset` references that document.

### Source-defined tools with the official MCP SDK

`InterviewState.RecordPreference` is the typed tool implementation. Its source
contains the standard `[McpServerTool]` and `[Description]` metadata; argument
names/types come from the method signature. Tool JSON no longer lives in the
interview configuration. Allowed preference fields and value bounds are derived
from the document schema when constructing a session.

`tools/ToolDefinitions` compiles the shared domain source with the pinned official
`ModelContextProtocol.Core` **2.2.0** SDK. `McpServerTool.Create` generates the
declarations, and the utility emits direct typed bindings for Unity. From the
repository root, regenerate after changing the domain source:

`dotnet run --project tools/ToolDefinitions/ToolDefinitions.csproj`

For CI, verify the committed artifact without writing:

`dotnet run --project tools/ToolDefinitions/ToolDefinitions.csproj -- --check`

Requires local .NET 10; NuGet dependencies are pinned in `packages.lock.json`.
Use `dotnet restore tools/ToolDefinitions/ToolDefinitions.csproj --locked-mode`
in CI. Unity Play and player builds reject stale source fingerprints. Commit
the generated `.g.cs` with its source change; never hand-maintain its declaration.
The current generator deliberately supports required string arguments and
synchronous `JObject` results only; unsupported signatures fail generation.

The official SDK runs **only during generation**, not in the Unity player.
Invocation uses direct calls, so no runtime reflection or linker preservation is
needed. The SDK's Unity/IL2CPP runtime compatibility has not been certified here.
Existing active-turn validation, session revocation, thread safety and value
validation remain authoritative; a generated schema is not a security boundary.

This is official-SDK tool-definition integration, **not an MCP network endpoint**.
The game continues using the orchestrator's authenticated REST/WebSocket protocol;
it adapts MCP tool metadata to the existing function-declaration format without
adding a second server or replacing the model/audio transport.

Set `service.pythonExecutable` to an absolute existing Python executable.
`service.packageRoot` must contain `orchestrator/service.py`. The default is an
absolute path to the main checkout's package so both Editor and development builds
use the same code. Relative paths resolve against the Unity project folder in
Editor, or the executable directory in builds. The Python environment is shared
with the main checkout. Adjust paths when moving the checkout.

Silero uses the already installed **Sherpa-ONNX 1.13.8** Windows x64 C API DLL
and its ONNX Runtime dependency, loaded from `vad.nativeDirectory`. The model
path expands environment variables. No second copy of the runtime or model is
stored in this project. This is a development installation, not a standalone
redistributable: adjust these paths on another machine and obtain appropriate
model/runtime licences before shipping.

To attach rather than own a service, set `ECHOCRADLE_SERVICE_URL` and
`ECHOCRADLE_SERVICE_TOKEN` in the environment inherited by Unity. Never place a
token in config, arguments or source. Attached clients never shut down the
service; owned clients request graceful shutdown and contain descendants in a
Windows kill-on-close Job Object.

## Voice and interruption

Unity continuously captures audio into fixed buffers. One bounded background
worker runs Silero with the experiment's VAD-only running gain controls; recorded
PCM remains unchanged. Before the first turn, stay quiet during the 0.6-second
noise-floor calibration. The worker measures median block RMS and prevents gain
boosting below four times that floor, matching the experiment. Calibration audio
is discarded before onset/pre-roll; it is not transcribed. This is not word
detection and does not guarantee rejection of louder breaths. Confirmed speech stops current playback and cancels text
and synthesis delivery. Late native TTS work may finish, but its result is not
played. Cancelled dialogue sessions are deleted and recreated before the next
turn. Client-side `config_set` validates every value; obsolete sessions and
cancelled turns cannot write preferences.

Streamed sentence chunks are handed off immediately; synthesis and playback are
serialized separately from socket consumption. History keeps delivered speech,
using a proportional word estimate for interrupted clips plus private interruption
context. This is **not exact word alignment**, physical-hearing measurement,
server reconciliation, or echo cancellation. Remote desktop audio buffering can
reduce interruption accuracy. Tune VAD durations/gain in config after live tests.

## Saves

New game immediately creates:

`%USERPROFILE%/AppData/LocalLow/EchoCradle/EchoCradle/Saves/<random-id>/`

`session.json` records a random deterministic-generation seed, timestamps and
`status: interview`. As soon as all required fields are recorded, the client stops
microphone capture, playback and generation, atomically writes `config.json` and publishes
`status: ready`. The config contains `username`, `style`, `ai_name`, and an
No story question is part of this interview. No farewell, quiet interval, or additional
utterance is required. Cancelled/error sessions remain incomplete and
are not loadable; no generated transcript or microphone recording is saved.

The path is Unity's `Application.persistentDataPath`; it appears on screen. No
Git-backed save system or Load game implementation is included.

## Validation

### Deterministic PCM replay regression harness

`SpeechCaptureProcessor` is the production microphone worker's endpointing code,
not a parallel simulation. It accepts fixed 16 kHz mono float blocks and an
injectable VAD function; production binds `SileroDetector.Process`. Replay tests
use scripted VAD decisions and known synthetic sample values, including clipped
PCM16 values, wrapped pre-roll and reused input buffers. The same `CaptureHandoff`
publishes ordered onset/PCM events. Queue each block's sample watermark before
processing; only successful processing advances it, after event/state publication.
One worker owns processing and buffers; readers observe state only, checking the
processing watermark before speech state. Polling and event-callback disposal
remain in `MicrophoneCapture` unchanged.

`SpeechCaptureProcessorTests` covers consecutive utterances, onset confirmation
reset, resumed speech resetting silence, maximum-length truncation, input bounds,
and a worker held inside VAD by an explicit gate (no timing-based onset guess).
Two `OrchestrationTransportTests` replay confirmed onset during scripted text and
a stalled TTS artifact read, then cancel the real client's token. They validate
transport cancellation/absence of returned synthesis audio, **not** controller
playback, late native synthesis completion, or physical audible interruption.

These deterministic tests do not load models, record microphone input or validate
recognizer accuracy. Fake service transcripts are protocol controls, not STT
evidence. No saved-WAV real Silero/recognition replay is added in this change:
that remains an opt-in integration task with human-labelled 16 kHz mono WAV
controls and already-cached models/service (no provisioning/downloads). Full
`InterviewController` FIFO/admission, late TTS/playback suppression, cancellation
during callbacks, real recognition and save completion still need integration
acceptance. The new tests have not been run as part of this change; the main
assistant must compile and run them in Unity.

### Orchestrator contract (2026-10-10)

`OrchestratorClient.TextAsync` submits `kind: text` and returns `OrchestratorJob`,
not an ordinary string on cancellation. Session creation sends **tools only**;
there is no `dialogue` job kind or `delivery_mode`. `SubmitAsync`, `GetJobAsync`
and `CancelAsync` validate the same canonical version/instance/job snapshot.
Inspect `State`, `Error`, `Revision`, `HostPending` and `SelectedModel`. A terminal
cancelled job can still have native work pending. `Text` is authoritative server
text; `PartialText` is received streaming text, which may not have been spoken.
The interview flushes speech and commits a ready save only on successful turns.

Tool attempts come from the server's final `calls`, including rejected attempts.
`ModelCallId` is model-owned and nullable when interrupted; `DispatchId` is the
nullable dispatch UUID. Socket `tool.call.call_id` is **only** that dispatch UUID.
`Outcome` and `ResultDelivered` are server facts. `LocallyExecuted` and
`LocallyExecutedDispatchIds` record local invocation evidence separately, never
delivery. Cancellation does not undo side effects; already-running synchronous
handlers cannot be forcibly stopped. Late execution after return is not included
in the returned evidence snapshot. Keep handlers short, thread-safe and revocable
(the interview's active-generation guard remains authoritative).

Callbacks and tool handlers run on workers, never on the Unity main thread.
Local tool authority accepts a deliberately narrow schema subset: closed object
roots with required primitive properties, enum, string lengths and numeric bounds.
Unsupported schema keywords fail session creation; socket arguments are validated
before invocation. JSON remains strict, depth/UTF-8/size bounded; IDs, booleans,
states and error metadata are validated. Exceptions retain `Code`, `HttpStatus`,
`Retryable`, `JobId`, `Phase` and optional last `Snapshot`, not remote prose or
inner transport exceptions.

Text's total deadline includes waiting for another turn, admission and reads.
TTS/STT have a 120-second total operation budget including artifact I/O/cleanup;
each request/read is also bounded to five seconds. Cancellation interrupts Mono
streams even when their `ReadAsync` ignores the token. Confirmed cancellation is
returned as a typed terminal snapshot when reconciliation fits the remaining
budget; otherwise cancellation/deadline/transport failure is explicit, never
invented as server completion. Best-effort ambiguous admission recovery uses
the original idempotency key in a detached bounded cleanup task; it does not
extend the caller's operation deadline. Session deletion is an additional safety
net. Advanced `SubmitAsync` only admits: callers own subsequent polling/cancel.

Verified TTS audio survives failed artifact deletion. STT uploads are deleted on
definite admission rejection only (validated 4xx or `503 NOT_READY`), never on an
ambiguous transport/5xx failure. Admitted inputs belong to the service. Cleanup
uses the remaining operation token, so cancellation can leave artifacts for the
service's retention expiry rather than extending the budget.

Offline EditMode boundary tests use a real loopback TCP HTTP/WebSocket fixture,
not private-method transport mocks or model inference. They cover a successful
text/tool/TTS/STT slice, rejected attempts, interrupted tool IDs/delivery,
canonical submit/get/cancel, structured errors, argument rejection, identity
changes, deadline and stalled artifact reads, integrity, and cleanup ownership.
The existing domain tests cover preference validation and atomic ready saves.
Live model reliability, microphone interruption, audible playback, IL2CPP and a
fresh player build still require integration acceptance; the explicit real-model
test is intentionally excluded from offline runs.

Final Unity 6000.6.3f1 batch EditMode verification on 2026-10-10:
**53 passed, 0 failed, 1 explicit real-model test skipped**. Report:
`Logs/orchestration-final-tests.xml` (generated/ignored). The actual Unity editor
compiled the runtime and interview consumers; no external compiler fallback or
model download was needed. `git diff --check` passed.

Verified on 2026-10-07 with Unity 6000.6.3f1:
**40 EditMode tests passed**, **1 PlayMode scene test passed**, and the explicit
**real-model service smoke passed** (native tools, session reset, TTS → STT,
owned shutdown). The **Windows x64 development build succeeded** at
`Builds/Windows/EchoCradle.exe` (generated/ignored, about 153 MB).
No owned orchestrator or llama-server process remained after the real smoke.
Live microphone, playback and interview completion were subsequently confirmed
by the user on 2026-10-10 via the tablet DCV connection. The laptop connection
returned silent input across three Python capture backends and the original
interview too; the tablet delivered measurable audio. Tool-calling reliability
remains model-dependent: the user needed to explicitly encourage tool use.

Player logs include preload status, detected speech onset, captured utterance
duration and STT result length, but not transcripts or microphone recordings.

Use **Window → General → Test Runner**:

- EditMode: bounded JSON transport, artifacts, cancellation, domain validation,
  stale-session revocation, atomic saves, chunks and cached native Silero silence.
- PlayMode: start-scene camera, settings and idle audio wiring.
- Explicit EditMode `RealOrchestratorTests`: starts cached real voice models,
  exercises native tool calling, session reset, TTS artifact verification and STT.
  This test does not use microphone/speakers or prove live interruption quality.

Desktop acceptance: start a new game, interrupt mid-sentence, correct a name,
provide a world style and companion name; confirm a ready save and orderly exit.
Also test cancel, missing microphone, ending Play Mode during model startup,
and retry after an error. Model/STT quality still affects conversation success.

Build Windows x64 with the Start scene enabled in Build Profiles. Python and
cached runtime paths must remain available to the development build.