# Unity character creation flow

The existing interview scene/settings asset is the entry point. No scene edits are
required: `InterviewController` adds `PlayerCharacterPreview` when absent. Use the
Built-in Render Pipeline required by that preview component.

## Player controls

1. **New game** starts the existing configuration conversation (username, world,
   companion name). Voice mode retains microphone/VAD, speech recognition,
   synthesized speech and barge-in. **Send text** also submits an answer while
   listening.
2. Configuration is saved as a **draft**, not ready. A **new service session** and
   new `InterviewState` ask for the player character's appearance, using
   `Assets/Data/PlayerDescription.json`. No configuration history is reused.
3. Generate an image using the recorded description and a fresh cryptographic
   uint seed. **Approve image** starts static 3D generation. **Reject / describe
   again** starts another empty appearance conversation: no silent image retry.
4. The static model is imported and displayed. At least one rendered frame must
   pass before **Continue: generate animations** can start animation generation.
5. Each returned clip is played in order. **Reviewed: Next / Finish** acknowledges
   the current clip; the last acknowledgement completes the save. An empty or
   invalid clip list fails instead of marking ready.
6. The final preview stays visible. Completion does **not** quit the application.
   Cancel/error leaves an incomplete save. Normal application shutdown still closes
   the owned service; attaching to a service never takes shutdown ownership.

The IMGUI draws `PreviewTexture` explicitly and does not paint a full-screen black
overlay over the model.

## Startup options

- `--text-only`, or `textOnly: true` in `Interview.json`: owned profile **text**,
  with no microphone, VAD construction, STT or TTS calls. Text is submitted through
  the same controller/conversation/tool path as voice transcripts.
- `--visual-config <file>`: owned service visual configuration. Otherwise use
  `service.visualConfig` from the JSON, or discover the project-relative 0110
  `automatic.local.json` when the setting is absent. It must already exist and be
  provisioned; this flow downloads/installs nothing.
- `--orchestrator-python <exe>` and `--orchestrator-root <directory>` override
  developer startup defaults. The root contains the Python `orchestrator` package.
- Relative option/settings paths resolve against the Unity project directory
  (`Application.dataPath/..`). For a standalone build, supply explicit paths when
  it is not beside the repository. No machine-specific drive/user path is embedded.
- Existing service URL/token environment attachment remains supported. An attached
  service must have visuals enabled and be appropriate for the selected profile;
  the client does not reconfigure an already-running external service.

`InterviewSettings.asset` serializes both the initial configuration and the new
appearance TextAsset, so the default scene does not depend on runtime asset lookup.

## Save contract

`persistentDataPath/Saves/<id>/` contains `config.json`, `session.json`, and
`appearance/appearance.json` plus hash-named PNG/GLB files. Provenance includes each
request's random uint seed, exact image/negative prompt, description-session index,
input/output SHA-256, artifact ID, file name, image approval/rejection and ordered
clip/review count. Rejected images remain available for traceability. Binary files
are flushed before the appearance manifest is replaced. Before publishing ready,
the save verifies every referenced output hash; `session.status=ready` is the last
atomic commit marker. Runtime save/state classes contain no Unity objects.

## Parent-owned real smoke run

No Unity instance or editor was started by this implementation. Build/run the
existing interview scene with graphics enabled. Do **not** use `-nographics` (the
test intentionally renders models and captures screenshots), `-quit`, or an editor
test runner as a replacement for running the scene.

Pass these player arguments: `--text-only --character-smoke`, with
`--visual-config <absolute path to provisioned automatic.local.json>` and, for a
build outside the repository, the Python/root overrides above.

Optional arguments:

- `--character-smoke-transcript <file>`: JSON matching
  `Unity/Assets/Data/CharacterSmokeTranscript.json`. Defaults are embedded in code
  for a build with no external transcript. `configuration` and `descriptions` are
  arrays of literal player responses; the last response repeats on clarification.
  Every response goes through **production `SubmitText`**; tools/results are never
  injected or faked. Set `rejectImages: 1` to exercise the NEW description session
  and additional fresh image request. The same explicit description may be reused
  by that rejection test, but its session and seed are new.
- `--character-smoke-output <directory>`: numbered stage/review PNG screenshots,
  copied transcript and `result.json`; otherwise use
  `persistentDataPath/CharacterSmoke/<timestamp>/`.
- Transcript `reviewSeconds` defaults to 2; `timeoutSeconds` defaults to 5400.

The driver uses `BeginNewGame`, `SubmitText`, `ApproveImage`/`RejectImage`,
`ContinueToAnimation`, and `NextAnimation`. It waits for rendered-review eligibility
and shows every clip before advancing. Monitor `CHARACTER_SMOKE_STAGE`,
`CHARACTER_READY`, and `CHARACTER_SMOKE_PASSED`/`CHARACTER_SMOKE_FAILED` in the player
log. Success requires `result.json.success=true`, final ready session, and every
clip's screenshot/review. The application remains open; **the parent owns teardown**.
These are real model calls, not a deterministic fixed-seed runtime test.

## Tests and current validation

Run EditMode filter `CharacterCreationFlowTests` using Unity Test Runner, or the
parent's existing command-line test runner with `-runTests -testPlatform EditMode
-testFilter CharacterCreationFlowTests -testResults <file>` against `Unity/`.

New tests cover review gates, rejection/new description sessions, unique seeds,
input/output provenance, independent bounded description schemas, portable options,
draft persistence, final-clip gating, hash integrity and atomic cleanup.

An isolated C# 9 compile of all Interview and orchestration sources and the new test
file succeeded using the installed Unity assemblies. Since glTF dependencies had
not been imported by Unity yet, that diagnostic compile used an explicitly stubbed
preview **API surface only**; it does not validate preview rendering/import.
The real NUnit pure-state gate/rejection tests and an independent pure C#
schema/options/persistence harness passed. Unity EditMode execution and real full
GPU/player smoke are left to the parent; no claim of rendered-flow success is made.