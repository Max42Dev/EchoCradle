# introduction-test-tts-sst — the orchestrator's first end-to-end run

## Native client tools (2026-10-07)

The interview uses native tool calling on every turn. The client sends its
registry's descriptions and parameter schemas to the service; Granite decides
whether to call a tool. Calls execute only in the client and results return to
the model before it continues. There is no JSON speech envelope, forced tool
choice or separate extraction pass. The service accepts application-defined
tools rather than owning an experiment-specific config schema.

Slow Kokoro synthesis and playback never block the network consumer: sentence
callbacks hand off bounded-per-turn text immediately to the speech worker.
Cancellation and spoken-delivery bookkeeping remain client-owned.

**Status:** bounded service/client slice implemented; real-model offline smoke passed
**Date:** 2026-10-06 (historical in-process findings began 2026-09-30)
**Author:** agent

`src/main.py` is now the **service-client entrypoint**: it automatically starts
one contained owner service (or attaches with `--service-url`). Models and the
llama.cpp runtime must already be cached; **no fresh downloads occur on this
launch path**. REST carries jobs and complete PCM artifacts; JSON WebSocket
carries text and remote compiled `config_set` calls. Capture, local VAD,
playback and delivery/history bookkeeping stay in the Python client.
There is no binary WebSocket/credit transport, server history reconciliation,
joint resource admission/provisioning or Unity client. Native service speech
crashes can terminate the whole process despite its three worker lanes.

See [precise service usage](../../../docs/SERVICE_USAGE.md) and the
[broader design reference](../../../docs/ORCHESTRATOR_SERVICE.md).
Historical `diagnostic_run.py`, `diagnose_*.py`, model benchmarks and hardware
tests below are explicitly **in-process model/device experiments**, not the
current main entrypoint or cached-only service setup instructions.

## Question

Can the **Model Orchestrator** drive a real, multi-modal, multi-turn application
— an LLM that interviews the player by voice and fills a config file — through
a separate local service using already cached models? The original in-process
experiment also measured model selection and first-use downloads; those are
historical findings, not today's service launch behavior.

## Hypothesis

Yes. If the orchestrator's planner, store and hosts are correct, a terminal app
should be able to:

1. probe the machine and choose a text model, a TTS voice and an STT model,
2. reuse the shared store and fail clearly if selected service assets are missing,
3. hold a spoken conversation in which the LLM records structured values,
4. validate the result against a schema and write it to disk.

The experiment is a **test of the orchestrator**, not of the interview idea. The
interview is deliberately small; the orchestrator is the thing under test and is
meant to stay in the project.

## Setup

- **OS:** Windows Server 2022 Datacenter (build 20348), AWS EC2
- **Hardware:** NVIDIA L4 24 GB (23 034 MiB), 16 GB RAM, ~24 GB free disk
- **Runtime:** Python 3.13.15, `sherpa-onnx` 1.13.8, `numpy` 2.5.3, `pytest` 8
- **Text runtime:** llama.cpp `llama-server` build **b11284** (CUDA 12.4), started
  by the orchestrator's text host
- **Models:** chosen by the planner from `orchestrator/catalog.json` — see below
- **Model store:** `%LOCALAPPDATA%\EchoCradle\models` (constant across experiments)

### Models the planner selected on this box

The probe reported **tier 3** (22.5 GB VRAM, 16.8 GB usable after reserve and
fragmentation slack, 15.4 GB RAM). The planner then chose:

| Modality | Model | Size | Licence | Why |
|---|---|---|---|---|
| text | `granite-4.2-8b-q4km` | 5.1 GB | Apache-2.0 | agentic-RL trained; best 8B for tool use |
| tts | `kokoro-en-v0_19` (speaker 7, `bf_emma`) | 298 MB | Apache-2.0 | natural voice, CPU-only, 11 speakers |
| stt | `sensevoice-small` | 229 MB | ⚠️ FunASR terms | 0% WER on real recordings, 21× realtime |

The current defaults are central catalog preferences, not experiment-owned
model IDs. Granite 4.2-8B is the proven text preference even if a larger model
fits. Missing, nonfitting or disallowed preferences fall back to quality/fit
selection. `--shippable-only` excludes SenseVoice (including explicit pins).
Joint portfolio/co-residency optimization with a Unity VRAM reserve is **design
only**, not implemented; today's planner checks individual models against the
probe budget and does not reserve room for a concurrently loaded generator.

### Voice choice

Piper's `amy-low` was used during development only because it downloads in 13 s
versus Kokoro's 49 s. It is the **lowest quality tier** of a 16 kHz VITS model
and sounds noticeably robotic. The orchestrator now defaults centrally to
**Kokoro `bf_emma`** (British female, speaker id 7). The experiment leaves models
and voice unspecified unless a CLI override is supplied. A smaller-RAM Piper
fallback uses its own default speaker 0, not Kokoro's speaker 7.

Measured on this box, one 8 s sentence, CPU synthesis:

| Voice | Synth time | RTF |
|---|---|---|
| Piper amy-low | 0.54 s | 0.06 |
| Piper lessac-medium | 0.83 s | 0.11 |
| **Kokoro** | 6.35 s | **0.79** |

Kokoro is ~13× slower than Piper but still faster than realtime, so
sentence-level streaming stays ahead of the LLM.

The wider TTS landscape — GPU-capable models, voice-design-from-description,
non-verbal tags, and the licence trade-offs — is documented in
[`ideas/model-orchestrator/audio.md`](../../../ideas/model-orchestrator/audio.md#tts-landscape-surveyed-2026-09).

### Live microphone input

`--mic` uses the VM's default input device, which NICE DCV redirects from the
client machine. See *Finding: the microphone is redirected, by DCV* below for
the measurements and the two fixes that were needed.

> **Ports are closed at the AWS level.** The security group for this instance
> allows only RDP (3389); 80, 443 and any custom port time out from outside.
> The instance role has no `ec2:` permissions, so this can only be changed in
> the AWS console.

## How to run (current service architecture)

```powershell
cd C:\projects\EchoCradle
python -m pip install -e ".\orchestrator[all,dev]"
cd experiments\llm-runtimes\introduction-test-tts-sst\src

# Automatically launches/owns one cached voice service; headphones required.
python main.py --play --mic

# Typed replies with no synthesis: cached text profile only.
python main.py --no-tts

# Starts and loads the cached profile, then prints capabilities; no downloads.
python main.py --capabilities

# Typed replies with speaker playback (playback is off by default).
python main.py --play

# Completed PCM16 WAV input instead of the microphone.
python main.py --stt-file reply.wav

# Startup-only voice override for an owned service, never a gameplay field.
python main.py --play --speaker-id 9

# Real-model service smoke; --out must be new/empty. Add --play optionally.
python service_smoke.py --out ..\out\service_smoke_new_run
```

Dependencies are not model/runtime provisioning. Cached selected text/TTS/STT
and llama.cpp are prerequisites; `--mic` also requires cached Silero VAD.
`--text-model`, `--tts-model`, `--stt-model`, `--speaker-id` and
`--shippable-only` configure startup policy, not gameplay or hot swaps. Attached
clients reject these supplied policy overrides. For standalone port 5010,
cryptographically generated **environment** token and
`--service-url http://127.0.0.1:5010`, follow
[SERVICE_USAGE.md](../../../docs/SERVICE_USAGE.md#standalone-service-and-attached-experiment).
An attached client never shuts down the service; the default service token
nevertheless grants the shutdown route (no separate owner credential).

Offline test commands from the repository root:

```powershell
cd C:\projects\EchoCradle\orchestrator
python -m pytest -q
cd ..\experiments\llm-runtimes\introduction-test-tts-sst
python -m pytest -q
```

Historical diagnostic scripts and `llm`-marked direct-host tests are separate
in-process model experiments, not `main.py` launch instructions; their setup
paths may download assets. Use `service_smoke.py` for current service evidence.

## Results

### Service/client and real-model smoke (2026-10-06)

`main.py`, including `--mic` and `--stt-file`, now uses `ServiceClient`. The
owned service resolves omitted IDs via central preferences at startup. Its
control loop submits bounded jobs to serialized text/TTS/STT worker lanes;
the Python client calls and tool/text callbacks are still synchronous on their
calling thread. The experiment coordinates local voice interruption/playback.
Preferred SenseVoice decodes a completed utterance, with no service partials.
The exact implemented `JobSpec` supports text/dialogue/tts/stt and differs from
the original design; see [usage](../../../docs/SERVICE_USAGE.md#exact-jobspec).

Real service smoke **passed**, recorded in `out/service_smoke_peter/report.json`:
Granite 4.2-8B / Kokoro (speaker 7) / SenseVoice produced a validated config with
`username: Max`, `style: medieval with mountains and ancient castles`,
`ai_name: Peter`. Auth/Origin/Host/payload checks, idempotency, text cancellation
and lane recovery, remote config calls and TTS → saved WAV → STT ran against
real models. The report explicitly records **no speaker playback and no
microphone test**; it does not validate full-duplex voice or live barge-in.
The existing client delivery ledger is not a server history mirror/reconciliation.

### Historical in-process findings

The findings below retain earlier measurements and debugging chronology.
Descriptions of first-use downloads, direct host calls, recognizer partials and
earlier capture/endpointing implementations are **not current service behavior**.

### Voice interruption fixes (2026-10-04)

The microphone stays open. Idle audio is bounded to a 300 ms pre-roll; confirmed
speech onset and subsequent audio are retained until a pause endpoints the
utterance. SenseVoice transcribes the complete utterance offline. Between-turn
resets no longer discard unread speech, and empty transcripts do not become
fictional `(no answer)` user messages. Spoken `Quit.` and `Exit!` stop the loop.

Generation, synthesis and playback are separate stages. A 10 ms coordinator
polls microphone onset even during native TTS synthesis or stalled generation.
Playback uses a PCM callback and immediate stream abort. Cancellation interrupts
stalled SSE body reads and skips pending tool calls. An in-flight native TTS call
cannot be killed, but its result is discarded and transcription proceeds while
it finishes; shared TTS access remains serialized. Connection/header setup and
an already executing tool are not cancellable.

Assistant history is reconciled after playback: completed sentences are retained,
unplayed text is omitted, and partial sentences use a proportional word estimate
from DAC-timestamped sample progress with an explicit interruption/approximation
marker. This is **not exact word alignment** or proof of what a remote audio
client physically heard. Driver/remote-desktop buffering can add uncertainty.

`src/diagnostic_run.py --play --mic` creates a unique `out/sessions/` folder with
continuous microphone audio, utterance/reply WAVs, console transcript, exact model
requests/deltas, history snapshots, config snapshots and `delivery.jsonl`.
Generated text, queued PCM and estimated heard text are recorded separately.
Use headphones: acoustic echo cancellation is not implemented.

Follow-up offline hardening: interruption bookkeeping is supplied as private
system context, never appended to assistant dialogue. Spoken-output filtering
rejects internal notes and stage directions across streaming chunks. Correction
instructions cover spelled names and replacement of already-filled fields,
without hardcoding player values or overriding model-owned tool calls.

Diagnostic microphone file writes run on a bounded background writer, not the
audio callback. Events record hardware timestamps, driver overflow flags and
dropped diagnostic blocks; gaps are explicitly mapped to WAV sample offsets.
`model-input/` saves exact prepared float32 STT input as `.npy` plus WAV previews.
SenseVoice preserves captured boundary silence; Whisper alone trims and pads.
These changes passed 169 orchestrator and 95 experiment offline tests. Actual
correction behavior, microphone muffling and input-backend differences still
need a controlled live retest; fake tests do not establish recognition accuracy.

Speech-aware capture now uses a local Silero VAD (~644 KB) loaded through
`ModelOrchestrator.create_vad()` from the shared store. Inference runs on one
bounded worker, never on the audio callback. Its block decisions are reused for
both onset and endpointing; 500 ms pre-roll retains the start of speech. Raw
captured/STT audio is not altered by VAD-only gain. Diagnostics record `vad_block`
speech decisions; this sherpa API does not expose posterior probabilities.
Saved-audio replay detected speech in all eight reviewed utterances and processed
111 s of continuous audio in under 1 s. It also marked some early audio as speech:
that does not yet prove the user-reported false interruption is eliminated.
Headphones remain required, and no live VAD test has been run.

### The orchestrator works end to end

All three modalities were verified against real models on this box:

| Check | Result |
|---|---|
| Probe + tier | tier 3, 16.8 GB usable VRAM, correct per the design doc's bands |
| Planner (historical run) | picked Granite 4.2-8B text / Kokoro TTS / Zipformer STT; current preferred STT is SenseVoice |
| Lazy download | text 43.6 s, Kokoro 48.6 s, Piper 12.7 s, Zipformer 21.0 s (first run only) |
| Reuse | second run loads from the store with no download |
| Text chat | `'ready'` in **0.06 s**; schema-constrained JSON in **0.31 s** |
| TTS | Piper 4.55 s of audio @ 16 kHz; Kokoro 3.17 s @ 24 kHz, 11 speakers |
| STT | `sensevoice-small`, **3/3 exact** on real microphone recordings |
| Speech round trip | TTS output transcribed back to the exact input text |
| Config written | `{"username": "Ada", "style": "medieval", "ai_name": "Bob"}` |

The speech round trip is the strongest single result: the orchestrator
synthesised a sentence, then transcribed its own audio back to the exact text,
with partials arriving while the audio was still being fed.

### The interview works

A full run produced a valid config, with the model objecting to the name on its
own and accepting it when the player insisted:

```text
ai>  Pray, tell me - what shall I call you? A name is the first stone upon
     which your world is built.
  [voice] line_000.wav  (6.0s)
you> Ada
ai>  Ada - elegant, precise, a name that could belong to a clockmaker or a
     cipher. Very well. Now, what style shall our world take?
  [tool] config_set({"field": "username", "value": "Ada"})
you> medieval
ai>  And what shall I be called, noble Ada?
  [tool] config_set({"field": "style", "value": "medieval"})
you> I want to call you Bob
ai>  "Bob"? Hm. I had hoped for something with a little more gravitas. I shall
     consider it, but only if you truly mean it. Convince me, or offer me
     something better.
you> no, I insist on Bob
ai>  Bob it is - though I suspect even dragons might chuckle at such a simple
     name. May I ask if you wish to give your tale a thread?
  [tool] config_set({"field": "ai_name", "value": "Bob"})
```

### Finding: the problem was instruction competition, not model size

The first design used a single prompt that asked the model to (a) hold a
persona, (b) follow a multi-step agenda and (c) decide when to call a tool. It
failed — the config stayed empty.

Bisecting the system prompt isolated the cause:

| System prompt | Tool calls emitted |
|---|---|
| `"You fill a config. Use config_set when the user gives their name."` | ✅ yes |
| + "Reply with plain text, no JSON" | ✅ yes |
| + "Never invent a value" | ✅ yes |
| + "Ask about ONE thing at a time" | ❌ **no** |
| + "Object playfully when the player names you" | ❌ **no** |
| Full interview prompt | ❌ **no** |

Each instruction is harmless alone; together they make the model *converse*
instead of *record*. This is **instruction competition**, not a capability wall.

**The fix is to split the work into two calls per turn:**

1. an **extraction** call with no persona and no agenda, which only decides what
   to record, and
2. a **persona** call with no tools, which only decides what to say.

Measured on this box:

| Setup | Tool calls emitted |
|---|---|
| Qwen2.5-7B, single combined prompt | 0 / 3 |
| Granite 4.2-8B, single combined prompt | 1 / 3 |
| Granite 4.2-8B, **extraction-only prompt** | **4 / 4** |
| Granite 4.2-8B, **split interview (end to end)** | ✅ config complete |

The split costs nothing extra in VRAM — it is two calls to the same model — and
it means the extraction step can later be pointed at a different or smaller
model without touching the conversation.

> **Correction to an earlier claim.** An initial version of this document said
> "native tool calling is unreliable at 7B". That was too strong. The failure
> was caused by the prompt, not the parameter count, and it is fixed by
> separating the two jobs. The model still matters — Qwen2.5-7B is a 2024 model
> and scored 0/3 where Granite 4.2-8B scored 1/3 on the *same* combined prompt —
> but prompt structure dominates.

> **Correction, later.** The two-call split described above was measured but is
> **not what ships**. A single call per turn turned out to be sufficient once the
> prompts were short and the field descriptions fixed (see below), and one call
> is simpler. `interview.py` makes one `chat_with_tools` call per turn.

### Finding: it was the field *names*, not the model

With the snob objection removed (below), Granite 4.2 8B still failed every run —
but in one specific way. Asked about `style`, it recorded the answer under
`username`:

```
you> Medieval, I think.
  [tool] config_set({"field": "username", "value": "Medieval"})   <- wrong field
```

`style` therefore never filled, the interview re-asked forever, and it hit the
turn limit. That looked like a model limitation. It was not.

`config_set`'s `field` argument was an enum of **bare names**:

```json
"field": { "enum": ["username", "style", "ai_name", "story"] }
```

That says *which* fields exist but never *what belongs in them*, so the model had
to infer the mapping from the conversation — and guessed. The schema descriptions
existed all along, but the `_set_tool` builder collected them into a local dict
that was then never used.

**The fix is to attach the meaning to the field selector:**

- each field's schema description is listed in the tool description, and
- the `field` argument's description spells out `name = meaning` for every field.

Measured on the scripted interview, same model and prompts otherwise:

| `config_set` field argument | Result |
|---|---|
| bare names | `username` and `style` confused; `style` never set |
| names + descriptions | `username`, `style`, `ai_name` all correct |

**Values are never constrained.** `style` and `story` are open text — someone
must be able to answer "sci-fi steampunk noir". Only the *selector* was unclear;
the *values* were always free.

### Design rule: the model owns the config; code only checks it

The interview was enforcing one behaviour in code: the AI is supposed to be
snobbish and object the first time the player names it. The code did that by
popping `ai_name` out of the config and speaking a hard-coded objection:

```python
proposed = self.config_tool.data.get("ai_name")
if proposed and not self.rejected_name_once:
    self.config_tool.data.pop("ai_name", None)   # withdraw it
    say = self._object_to_name(proposed)         # scripted line
```

This was worse than doing nothing. The value was thrown away, and the **model
was then expected to remember to record it again** after the player insisted.
It usually did not, so `ai_name` was missing at the end — in **every** run:

| Snob rule | Scripted interview completion |
|---|---|
| enforced in code | **0 / 4** |
| described in the prompt only | **3 / 3** |

The rule now lives in `SYSTEM_PROMPT` as character, and may or may not fire —
both are fine:

> When the player first names you, you may object to it once. If they insist,
> accept it gracefully and move on.

The general rule this establishes:

* **Character behaviour belongs in the prompt.** Never mutate a value the model
  recorded, and never fake its dialogue from code.
* **The only enforcement is outside the turn.** The loop keeps asking until the
  config validates against its schema. Nothing interferes inside a turn.
* One line added to the prompt stopped the model re-recording `username` every
  turn: *"Once a value is recorded, leave it alone unless the player changes it."*

### The config tool is now a single, self-contained `config_set`

The tool started with three functions — `config_get`, `config_set`,
`config_validate` — and a description that talked about "the player". Both were
wrong for a *generic* JSON-document tool:

* **`get` and `validate` were redundant.** If every `set` returns the whole
  document, the model never needs a separate read or check. The tool now exposes
  **one** function, and every result — success *or* failure — carries the full
  state:

  ```json
  {"ok": true, "field": "style", "value": "medieval",
   "config": {"username": "Ada", "style": "medieval"},
   "missing": ["ai_name"], "complete": false,
   "valid": false, "errors": ["missing required field: ai_name"]}
  ```

* **The domain language was removed.** "Record a value the player has given you"
  became "Set one field of the configuration document". The interview is one
  instance of the tool, not its definition.

* **The schema lives in the tool description, not the prompt.** The fields and
  their meanings are listed in the tool description and in the `field` argument,
  so the system prompt no longer embeds the JSON schema. Measured on Granite
  4.2-8B, embedding it made **no difference** to the completion rate (3/5 either
  way), so the smaller prompt wins.

### Finding: the model accepts a value but does not record it

A live test now drives the **real** model end to end (`tests/test_interview_live.py`,
marked `llm`). Both sides are LLMs: the AI is the system under test, and the
player is a second model call that invents its own name, style and the AI's name.
The conversation is stochastic on both sides; the contract is fixed — within a
bounded number of turns the config must be complete, valid, and written to disk.

On the first runs the test failed about **1 in 6**. The recorded transcript
(`out/failing_live_*.txt`) showed the cause precisely. The model would say:

```text
[user] You may be called Rowan.
[assistant] Rowan it is—your AI companion will be called Rowan. ...
            tool_calls: null          <- accepted the name, never recorded it
```

`username` and `style` recorded fine; `ai_name` did not. The model *understood*
the value and even repeated it back, but never emitted the `config_set` call.

**This is a fixed point, not a temperature problem.** Both models run at
`temperature=0.8`, but once the context contains the same exchange repeated a
few times, the next-token distribution collapses onto repeating it. The loop
becomes self-reinforcing:

```text
[user] El it is—my name is El. Shall we begin?
[assistant] Rowan it is—your AI companion will be called Rowan. ...
[user] El it is—my name is El. Shall we begin?
[assistant] Rowan it is—your AI companion will be called Rowan. ...
```

### Fix: a stall-breaker in the loop

When the same field has been missing for **three turns**, the interview injects a
firmer internal note:

> You have asked about 'ai_name' several times without recording it. The player
> has already given you a value for it. Stop asking and call config_set now, in
> this turn, with the value the player gave.

This breaks the fixed point. Measured over 20 consecutive live runs:

| Loop | Live-test completion |
|---|---|
| plain | ~5 / 6 (83%) |
| **with stall-breaker** | **20 / 20 (100%)** |

Runs that would have failed now take a few more exchanges and then recover,
which is why some runs take ~30 s instead of ~15 s.

The stall-breaker lives in `Interview._turn`, so the **real** voice experiment
(`main.py`) uses it too — the only difference between the test and the real run
is the player (an LLM in the test, the operator on the microphone in the real
run).

### `Conversation`: history as its own object

`ModelOrchestrator.chat()` is deliberately stateless — every call is independent
and the caller supplies the whole message list. That is the right primitive, but
it meant every caller re-implemented the same bookkeeping. `Conversation`
(`orchestrator/conversation.py`) now owns it: a system prompt, an alternating
user/assistant history, and transient notes that are sent for one call and then
discarded. Both the interview and the test's player use it.

### Finding: the mic cue fired before the device was open

The live run is **turn-based, not full-duplex**: `main.py` waits for the AI's
playback to finish (`player.wait()`) before opening the microphone, so the AI
cannot hear the player while it is speaking. That is deliberate — on a loopback
device the mic would otherwise transcribe the tail of the AI's own question.

But the "listening" cue was printed *before* the capture device was open:

```python
print("  [mic] listening ... (speak, then pause)")   # printed too early
text = mo.listen(recorder, on_partial=...)           # device opens inside here
```

Opening a capture device takes a moment, and anything said during that window is
lost. The operator saw the cue, started talking, and lost their first word — the
green mic light appeared a few seconds *after* the AI stopped.

`record_utterance` already supported `on_ready`, which fires only once the device
is genuinely capturing. `_listen` now uses it, so the cue is truthful.

### Streaming STT for the microphone

The catalog default (`sensevoice-small`) is **offline**: it decodes only after
the utterance ends, so no partials appear while the player speaks. `--mic` now
pins `zipformer-en-streaming` instead, so partials arrive live. The trade-off is
accuracy — 23% WER against SenseVoice's 0% on real speech — so
`--stt-model sensevoice-small` restores the accurate, non-streaming path.

### Finding: the microphone audio is ~40 dB too quiet

The single biggest accuracy problem was not the model — it was the **level**.
Recordings captured from the redirected microphone measured:

| Recording | Peak | RMS |
|---|---|---|
| `utterance_000` | 0.0110 | 0.0012 |
| `utterance_001` | 0.0220 | 0.0024 |

A peak of 0.011 is about **−39 dBFS**: the loudest sample is 1% of full scale.
The words are all present — the per-250 ms RMS profile shows a clear speech
burst — but the recogniser cannot use them at that level.

The proof is that **amplifying the same recording recovers the words**:

| Recording | As recorded | Amplified to 0.9 peak |
|---|---|---|
| `utterance_000` | `'St.'` | `'Second sentence. My name is Stax.'` |
| `utterance_001` | `'My.'` | `'Forour sentence, my name is still next.'` |

So the diagnosis is neither "recording problem" nor "transcription problem" in
the usual sense: the recording is complete, and the model is fine — the **input
is too quiet**.

**Fix: normalise before transcribing.** `normalise_audio()` scales a whole
utterance's peak to 0.9 (capped at 100× gain); `_RunningAgc` does the same
per-block for the streaming path. Both are applied in every transcription path.
Measured on the same recordings, `sensevoice-small` went from `'FOR'` to
`'First sentence, My name is D Me.'`.

### Finding: the mic should use the accurate model, not the streaming one

The mic uses `sensevoice-small` (the catalog default). A streaming recogniser
was tried for live partials, but its transcript is markedly worse on quiet
redirected audio, and nothing in the design needs partials — the always-open
recorder segments on energy, not on the recogniser.

### The recorder threshold is calibrated, not fixed

Redirected audio is quiet enough that a fixed threshold either misses speech or
trips on noise. `main.py` now calls `AudioRecorder.calibrate()` at startup and
uses the measured value.

### Finding: turn-based capture loses anything said while the AI talks

The original design opened the microphone only after the AI stopped speaking
(`player.wait()`), so a sentence spoken while the AI was talking or thinking was
simply never captured. An intermediate attempt at "barge-in" (open the mic
during playback and stop the AI on speech) failed on a loopback device: the mic
heard the AI's own voice and triggered on that.

The fix is to **keep the microphone open for the whole session** and **let the
recogniser do the endpointing**. `ContinuousRecorder` opens the device once and
streams every block — silence included — to a consumer. `SttHost.listen_stream`
feeds those blocks straight into the recogniser and stops when the model reports
an endpoint (`is_endpoint`).

This replaced an energy-threshold segmenter that had two bugs:

* it dropped the blocks that confirmed speech, so "first sentence" became
  "sntence" — the onset of a word is where its first phoneme lives;
* it cut only on a fixed silence gap, so sentences with a shorter pause merged
  into one utterance.

The recogniser knows where speech ends; a volume level does not. Removing our
own segmentation removed both bugs.

This is only safe with **headphones** — on a loopback device the microphone
hears the AI and would transcribe that as the player's speech. The experiment
prints a reminder, and this is the only mic mode: there is no turn-based or
barge-in variant to choose between.

### Finding: the reply was fully generated before any of it was spoken

The first working voice run exposed the real problem with the conversation, and
it was not the microphone. `chat_with_tools` was a **blocking, non-streaming**
call: the model generated its *entire* reply (measured at 7 sentences, ~19 s of
audio) before a single word was spoken. By the time the player heard the first
sentence, the model had already committed to all seven. An interruption was
therefore impossible to notice — the model had finished thinking long before the
player could react, and the player's words only arrived as the *next* turn.

The fix is to **stream the model's output and speak it as it arrives**:

* `TextHost.stream_chat_with_tools` streams the completion, calls
  `on_text(chunk)` for every delta, and accumulates tool-call arguments across
  deltas (llama.cpp sends `function.arguments` as a partial string).
* `Interview.respond_streaming` feeds those deltas through the
  `SentenceSplitter` and speaks each complete sentence the moment it exists.
* Measured time to first spoken sentence dropped from ~19 s to **1.3 s**.

### Barge-in: stop, truncate, and continue from what was heard

With streaming in place, an interruption can actually interrupt. While the AI
speaks, `ContinuousRecorder` watches the level and latches `speech_detected()`
after two loud blocks. When it fires:

1. `AudioPlayer.abort()` closes the output stream, so the rest of the line is
   never heard (a *paused* stream would still hold the queued audio).
2. `should_stop()` returns True, so `stream_chat_with_tools` abandons the
   completion mid-generation.
3. The turn's history keeps **only the sentences that were actually spoken** —
   not the full reply the model had planned.

So if the model wanted to say *"Hi, this is a game, please give me your name and
also name me"* but the TTS only reached *"Hi, this is a game, please give me
your name"* before the player cut in with *"hi, my name is Max, how are you"*,
the next turn's context is exactly:

```text
model> Hi, this is a game, please give me your name
user>  hi, my name is Max, how are you
```

The model continues from what the player heard, not from what it intended. A
barge-in during the *last* sentence is caught too: `AudioPlayer.wait()` polls
`should_stop()` while the audio drains, so generation being finished does not
hide an interruption.

### Finding: the STT model was the problem, not the microphone

The old default, `zipformer-en-20m-streaming`, is a **20-million-parameter**
streaming model. It loses the beginning of nearly every sentence and sometimes
returns nothing at all. On clean, synthesised speech — no microphone involved:

```text
"Hello, my name is Max and I like music."  ->  "O MY NAME IS MAX AND I LIKE MUSIC"
"The cat sat on the mat."                  ->  "T SAT ON THE MAT"
"I would like a cup of tea please."        ->  ""            (nothing)
```

Because TTS-generated audio produced the same failures, the microphone was
never the cause. A two-minute debug of the recogniser's per-chunk output
confirmed no words were being discarded by the host: the model simply emits
nothing for the first ~1 s and then starts mid-word.

`stt_benchmark.py` scores candidate models on a fixed 20-sentence corpus that is
synthesised once and reused, so every model sees identical bytes:

| Model | WER | Exact | Realtime | Streaming |
|---|---|---|---|---|
| **whisper-base.en** | **10%** | 18/20 | 4.2× | no |
| whisper-small.en | 10% | **19/20** | 1.8× | no |
| moonshine-tiny.en | 15% | 17/20 | 6.7× | no |
| moonshine-base.en | 21% | 16/20 | 5.9× | no |
| sensevoice-small | 22% | 16/20 | 16.6× | no |
| zipformer-en-streaming (2023-06) | 23% | 16/20 | 3.2× | **yes** |
| ~~zipformer-en-20m-streaming~~ | **78%** | **0/20** | 6.6× | yes |

The incumbent got **zero** sentences exactly right. Every alternative beats it
by a wide margin, so the catalog default is now `sensevoice-small`.
`zipformer-en-streaming` is retained for its partials.

#### Judged on real microphone audio

Synthesised speech is a *best case*: a clean, loud, native-accented voice. The
decision therefore also used recordings of the actual operator speaking into the
actual remote-desktop microphone (`compare_on_recordings.py`):

| Model | WER on real speech | exact | speed | needed size |
|---|---|---|---|---|
| **sensevoice-small** | **0%** | **3/3** | **21× realtime** | **228 MB** |
| whisper-small.en | 0% | 3/3 | 3.7× | 640 MB |
| whisper-base.en | 11% | 2/3 | 8.9× | 278 MB |
| moonshine-tiny.en | 17% | 2/3 | 18× | 119 MB |
| zipformer-en-streaming | 44% | 1/3 | 4.4× | 320 MB |
| zipformer-en-20m (old) | 72% | 0/3 | — | 130 MB |

SenseVoice is the clear winner: the equal-best accuracy, six times faster than
whisper-small, and a third of its size. It is **encoder-only and
non-autoregressive**, so unlike the Whisper decoder it cannot fall into a
repetition loop — it transcribed perfectly the recording where Whisper produced
`"Hello Hello Hello Hello ..."`.

> **Licence caveat.** `sensevoice-small` is marked `shippable: false`. The
> sherpa-onnx wrapper is Apache-2.0 but the FunASR weights carry their own
> terms, which have not been reviewed. `whisper-base-en` (MIT) and
> `zipformer-en-streaming` (Apache-2.0) remain available via `--shippable-only`.

#### Streaming, for interruption

Interruption and barge-in need *partial* results — the game should react while
the player is still talking. Whisper and SenseVoice both decode only completed
utterances, so `zipformer-en-streaming` (23% on synthesised speech, but
incremental) is kept alongside them.

`SttHost.load()` selects the loader from the model's `kind` parameter, so
switching between streaming and offline is a catalog change, not a code change.

#### Two practical notes

* **Level.** Whisper expects roughly unit-scale input; at the −30 dBFS of
  redirected microphone audio its decoder loops. The host normalises the peak
  for Whisper only — SenseVoice normalises internally and prefers the original.
* **Silence must be trimmed.** Whisper treats padding as a cue to keep
  generating. `trim_silence()` removes leading and trailing quiet (keeping a
  100 ms pad); this is functional, not cosmetic.

### Finding: the microphone *is* redirected, by DCV

An earlier version of this document claimed the VM had no working microphone.
That was **wrong**, and the error came from testing a played tone rather than
the operator's voice.

NICE DCV (running as `dcvserver`/`dcvagent`) redirects the client's microphone
into the session. RDP audio, which is playback-mostly, is not what carries it.
Measured speech arrived around −30 dBFS — quiet, but present and intelligible.

Two consequences, both now handled:

* the recorder's default threshold had to drop from `0.012` to `0.0025`
  (`AudioRecorder.calibrate()` measures the room), and
* the cue to start speaking must fire **after** the capture device is open
  (`record_utterance(on_ready=...)`), otherwise the first word is lost while the
  device is still opening.

Also worth recording: the earlier transcribe bug that made every sentence lose
its beginning (`"The cat sat on the mat"` → `"T SAT ON THE MAT"`) was not caused
by any of this. It was the 20M model's own accuracy limit, and it reproduced on
perfectly synthesised audio.

### Finding: DCV microphone redirection is stateful and drops silently

The redirection is not permanent. It was working (three recordings captured
perfectly at peaks 0.19–0.38), then later delivered **exact digital silence**
(`peak=0.00003`) while `dcvserver`/`dcvagent` were still running and the DCV
session was still active. Nothing in this project touches the audio device
configuration, so the redirection was lost on the client side or between client
and session.

The diagnostic that separates the two cases is one line — capture straight from
the device and look at the peak:

```powershell
python -c "import numpy as np,sounddevice as sd; \
rec=sd.rec(int(5*16000),samplerate=16000,channels=1,dtype='float32'); \
sd.wait(); print(np.max(np.abs(rec)))"
```

* `peak < 0.0001` → the virtual microphone is silent. Reconnect the DCV client
  with **microphone redirection enabled**, or use a client that supports it.
  Nothing in the code can fix this.
* `peak` around `0.1–0.4` with a reply that is *"nothing recognised"* → the
  audio is fine and the problem is the STT model (see the tables above).

Because this looked identical to "the operator was not speaking" from inside the
app, `say_test.py` now reports it explicitly instead of playing back silence.

### Finding: the default input device is silent; device 6 works

The default input (`[1, 3]` — MME "Microphone (AWS Virtual Microphone Device)")
delivers **exact digital silence** (`peak=0.0000`), even while the DCV
redirection is live. The same physical microphone is exposed under several
backends, and only some of them carry audio. Probing every input device for
1.5 s gave:

| idx | backend | device | peak |
|----:|---------|--------|-----:|
| 0 | MME | Microsoft Sound Mapper - Input | 0.006 |
| 1 | MME | Microphone (AWS Virtual Microphone) | 0.016 |
| 5 | DirectSound | Primary Sound Capture Driver | 0.041 |
| **6** | **DirectSound** | **Microphone (AWS Virtual Microphone Device)** | **0.102** |
| 12 | WASAPI | Microphone (AWS Virtual Microphone Device) | *invalid sample rate* |
| 14 | WDM-KS | Microphone (AWS Virtual Microphone Wave) | *invalid device* |

`pick_input_device()` now does this probe at startup and picks the loudest
device, so `--mic` no longer depends on the default being correct. Pass
`--device N` to pin one.

### Finding: a blocking `stream.read()` returns zeros on the virtual devices

This was the real reason the microphone never worked, and it was not the device
or the model. `sounddevice` offers two ways to read a stream:

* **blocking** — `stream.read(frames)` waits for audio, and
* **callback** — the driver calls a function with each block.

On the AWS/DCV virtual microphone the **blocking read returns zeros
immediately** instead of waiting. A ten-second recording therefore produced
**314 seconds** of silent audio: the loop spun as fast as it could, each read
returning instantly. The callback path delivers real audio on the same device
(`peak=0.048`).

`ContinuousRecorder` now uses a callback stream. The queue and `next_block()`
API are unchanged, so nothing downstream moved. `hw_test.py` captures with
`sounddevice` directly for the same reason.

### Finding: the recogniser endpoints on silence, not only on speech

`zipformer-en-streaming` fires `is_endpoint` on a run of silence as well as at
the end of an utterance. Honouring that before the player had spoken ended the
utterance immediately and returned `''`. `listen_stream` now ignores an endpoint
until there is actually text to return, so it waits for speech and then ends on
the pause after it.

### Finding: reasoning models return empty content

Granite 4.2 is a reasoning model. With a small `max_tokens` it spends the whole
budget on hidden reasoning and returns `content: ""` with
`finish_reason: "length"`. The catalog records `enable_thinking: false` for such
models and the text host passes it as `chat_template_kwargs`, which fixes it.
The host also now starts `llama-server` with `--jinja` so the model's own chat
template (and therefore native tool calling) is active.

### Finding: the text host leaked `llama-server` processes

The first runs left four `llama-server` processes holding **12.4 GB** of VRAM,
which then made the planner refuse a model that had previously fit. Two fixes:

- `main.py` now wraps the run in `try/finally` and always calls `mo.stop()`,
- `TextHost.__del__` is a safety net so a dropped host never leaves a server
  holding VRAM.

After the fix, a full run leaves **zero** processes behind.

### Finding: the Windows console cannot print model output

Models emit typographic characters (U+2011 non-breaking hyphen, curly quotes)
that the default cp1252 console codec cannot encode, which crashed the run with
`UnicodeEncodeError`. `main.py` now reconfigures stdout/stderr to UTF-8 with
`errors="replace"`.

### Store footprint

| Item | Size |
|---|---|
| `llama.cpp` b11284 (CUDA) | 1 135 MB |
| `granite-4.2-8b-q4km` | 5 100 MB |
| `qwen2.5-7b-instruct-q4km` | 4 466 MB |
| `qwen2.5-1.5b-instruct-q4km` | 940 MB |
| `kokoro-en-v0_19` | 352 MB |
| `sensevoice-small` | 229 MB |
| `whisper-base-en` | 278 MB |
| `zipformer-en-streaming` | 320 MB |
| `zipformer-en-20m-streaming` | 130 MB |
| `piper-en-amy-low` | 77 MB |
| **Total** | **~12.6 GB** |

The Qwen2.5 models were downloaded during the tool-calling investigation and are
not needed for a normal run, which uses ~6.7 GB. All of it is reusable by any
future experiment.

## Conclusion

`promising` — **the orchestrator is sound and the experiment validates it.**

- The planner, probe, store and all three hosts work against real models. Model
  selection, lazy download, reuse and cleanup all behave.
- Speech is the standout: CPU-only, streaming, and accurate enough to round-trip
  its own TTS output.
- The interview works. The key lesson is that **prompt structure dominates model
  size** for tool calling: splitting persona from extraction took the same model
  from 1/3 to 4/4.
- The orchestrator is worth keeping and extending; the interview is a throwaway
  harness that happens to be a good integration test.

## Next steps

- [ ] Promote the tool-calling finding to `ideas/model-orchestrator/text.md`.
- [ ] Add the remaining hosts (image, mesh3d, music, sfx) behind the same contract.
- [x] Implement the bounded Python service/client and real-model offline smoke.
- [ ] Build a Unity client; implement binary PCM/credits and server delivery/history.
- [ ] Validate the service-based microphone/playback/barge-in path live.
- [ ] Add explicit provisioning and joint resource admission from the design reference.
- [ ] Implement the scheduler's VRAM bin-packing (currently one model per host).
- [ ] Try a dedicated extraction model (xLAM-2-3b, ToolACE-2-8B) for the second call.
- [ ] Re-test the single-prompt design on a 14B+ model to see if the split is still needed.

## Links

- Service usage: [`../../../docs/SERVICE_USAGE.md`](../../../docs/SERVICE_USAGE.md)
- Design reference/status: [`../../../docs/ORCHESTRATOR_SERVICE.md`](../../../docs/ORCHESTRATOR_SERVICE.md)
- Orchestrator: [`../../../orchestrator/`](../../../orchestrator/)
- Related idea: [`../../../ideas/model-orchestrator/modelorchestrator.md`](../../../ideas/model-orchestrator/modelorchestrator.md)
- Text runtime: [`../../llm-runtimes/0101-text-runtime-selection/`](../0101-text-runtime-selection/)
- Speech engines: [`../../audio-generation/0109-streaming-tts-stt/`](../../audio-generation/0109-streaming-tts-stt/)
- VRAM probing: [`../../benchmarks/0105-vram-probe-residency/`](../../benchmarks/0105-vram-probe-residency/)
