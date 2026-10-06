"""Instrumented 60-second voice run: log every stage to find the divergence.

Runs the real interview for a fixed wall-clock budget and writes a log with, for
every turn:

* the audio level of each captured utterance (peak / rms),
* the transcript the recogniser produced,
* the exact message list handed to the model,
* the model's reply and any tool calls.

The point is to see *where* what you said stops matching what the model got.

Usage::

    python trace_run.py --seconds 60
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[4]
_ORCHESTRATOR_PARENT = _REPO_ROOT / "orchestrator"
if _ORCHESTRATOR_PARENT.is_dir() and str(_ORCHESTRATOR_PARENT) not in sys.path:
    sys.path.insert(0, str(_ORCHESTRATOR_PARENT))

import numpy as np  # noqa: E402

from orchestrator import (  # noqa: E402
    AudioPlayer,
    ContinuousRecorder,
    JsonConfigTool,
    ModelOrchestrator,
    Modality,
    ToolRegistry,
    pick_input_device,
    write_wav,
)  # noqa: E402
from orchestrator.errors import OrchestratorError  # noqa: E402

from config_schema import CONFIG_SCHEMA  # noqa: E402
from interview import Interview  # noqa: E402

OUT = Path(__file__).resolve().parent.parent / "out" / "trace"
LOG = OUT / "trace.log"


class _Tee:
    """Write to stdout and a log file at once."""

    def __init__(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self._file = path.open("w", encoding="utf-8")

    def __call__(self, text: str = "") -> None:
        print(text)
        self._file.write(text + "\n")
        self._file.flush()

    def close(self) -> None:
        self._file.close()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--seconds", type=float, default=60.0)
    args = parser.parse_args()

    log = _Tee(LOG)
    mo = ModelOrchestrator()
    try:
        return _run(mo, args.seconds, log)
    finally:
        mo.stop()
        log.close()


def _run(mo: ModelOrchestrator, seconds: float, log: _Tee) -> int:
    try:
        mo.ensure_model(Modality.TEXT)
        mo.ensure_model(Modality.TTS, model_id="kokoro-en-v0_19")
        mo.ensure_model(Modality.STT, model_id="sensevoice-small")
    except OrchestratorError as exc:
        log(f"Cannot run: {exc}")
        return 2
    mo.tts.speaker_id = 7

    device = pick_input_device(sample_rate=mo.stt.sample_rate)
    recorder = ContinuousRecorder(device=device, sample_rate=mo.stt.sample_rate)
    if not recorder.start():
        log(f"mic unavailable: {recorder.error}")
        return 2
    threshold = recorder.calibrate()
    log(f"device={device} threshold={threshold:.4f} budget={seconds:.0f}s")

    player = AudioPlayer()
    tool = JsonConfigTool(CONFIG_SCHEMA, name="config")
    registry = ToolRegistry()
    tool.register_into(registry)
    interview = Interview(mo, tool, registry, max_turns=20)

    # Record the exact messages the model is given, by wrapping the host call.
    original = mo.text.stream_chat_with_tools

    def traced(messages, reg, **kwargs):
        log("")
        log("  --- MESSAGES SENT TO MODEL ---")
        for m in messages:
            content = (m.get("content") or "").replace("\n", " ")
            log(f"    [{m['role']}] {content[:200]}")
        return original(messages, reg, **kwargs)

    mo.text.stream_chat_with_tools = traced  # type: ignore[method-assign]

    deadline = time.monotonic() + seconds
    turn_index = 0
    opening = True
    try:
        while time.monotonic() < deadline:
            spoken: list[str] = []

            def on_sentence(sentence: str) -> None:
                spoken.append(sentence)
                log(f"ai>  {sentence}")
                samples, rate = mo.tts.synthesize_samples(sentence)
                player.play(samples, rate)

            def should_stop() -> bool:
                # Only armed once the AI has spoken: polling during generation
                # would abort the model before it says a word.
                return bool(spoken) and recorder.speech_detected()

            recorder.reset()
            if opening:
                turn, interrupted = interview.opening_streaming(on_sentence, should_stop)
                opening = False
            else:
                reply = _listen(mo, recorder, log, turn_index)
                if reply.lower() in {"quit", "exit"}:
                    break
                turn, interrupted = interview.respond_streaming(
                    reply or "(no answer)", on_sentence, should_stop
                )
            turn_index += 1

            if interrupted:
                player.abort()
                log("  [barge-in] stopped")
            else:
                if player.wait(should_stop):
                    player.abort()
                    log("  [barge-in] stopped during playback")

            for call in turn.tool_calls:
                log(f"  [tool] {call.name}({json.dumps(call.arguments)})")
            log(f"  config={tool.data} missing={tool.missing()}")
            if turn.done or interview.complete:
                log("  COMPLETE")
                break
    finally:
        player.close()
        recorder.stop()

    log("")
    log(f"FINAL CONFIG: {json.dumps(interview.config())}")
    log(f"log written to {LOG}")
    return 0


def _listen(mo: ModelOrchestrator, recorder: ContinuousRecorder, log: _Tee, index: int) -> str:
    log("  [mic] listening ...")
    captured: list[np.ndarray] = []
    text = mo.listen_continuous(recorder, on_block=captured.append)
    if captured:
        audio = np.concatenate(captured)
        peak = float(np.max(np.abs(audio)))
        rms = float(np.sqrt(np.mean(audio**2)))
        seconds = len(audio) / recorder.sample_rate
        OUT.mkdir(parents=True, exist_ok=True)
        write_wav(OUT / f"utterance_{index:03d}.wav", audio, recorder.sample_rate)
        log(f"  [audio] {seconds:.1f}s peak={peak:.4f} rms={rms:.4f}")
    log(f"you> {text!r}")
    return text.strip()


if __name__ == "__main__":
    raise SystemExit(main())
