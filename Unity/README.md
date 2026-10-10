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

`Assets/Data/Interview.json` contains all interview prompts, tool declarations,
the string-field JSON schema, UI text, generation bounds, voice settings and
save filenames. `Assets/Data/InterviewSettings.asset` references that document.

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
PCM remains unchanged. Confirmed speech stops current playback and cancels text
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
`status: interview`. Only after all required fields validate and closing speech
finishes does the client atomically write `config.json` and publish
`status: ready`. The config contains `username`, `style`, `ai_name`, and an
optional `story` supplied voluntarily. A final quiet interval allows a last
correction before committing. Cancelled/error sessions remain incomplete and
are not loadable; no generated transcript or microphone recording is saved.

The path is Unity's `Application.persistentDataPath`; it appears on screen. No
Git-backed save system or Load game implementation is included.

## Validation

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