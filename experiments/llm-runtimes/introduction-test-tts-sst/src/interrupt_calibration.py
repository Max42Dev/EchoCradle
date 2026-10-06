"""LLM-free scripted barge-in exercise using the real orchestrator speech hosts."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from types import SimpleNamespace
from concurrent.futures import ThreadPoolExecutor

from main import AudioPlayer, ContinuousRecorder, ModelOrchestrator, Modality
from main import _run_turn, _listen, pick_input_device
from interview import TurnResult


class ScriptedInterview:
    """Produce one known reply; no LLM or config extraction involved."""

    def __init__(self, sentences: list[str]) -> None:
        self.sentences = sentences
        self.history: list[dict[str, str]] = []
        self.turns = 0
        self.interruption_context = None

    def opening_streaming(self, emit, stop):
        self.turns += 1
        emitted = []
        for sentence in self.sentences:
            if stop():
                break
            emitted.append(sentence)
            emit(sentence)
        text = " ".join(emitted)
        self.history.append({"role": "assistant", "content": text})
        return TurnResult(say=text, config={}, done=False), stop()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()
    args.out.mkdir(parents=True, exist_ok=True)
    mo = ModelOrchestrator()
    player = AudioPlayer()
    recorder = None
    try:
        mo.ensure_model(Modality.TTS, model_id="kokoro-en-v0_19")
        mo.tts.speaker_id = 7
        mo.ensure_model(Modality.STT, model_id="sensevoice-small")
        cue = "When you hear the word now, interrupt me by saying: My name is Max."
        print(cue, flush=True)
        samples, rate = mo.tts.synthesize_samples(cue)
        player.play(samples, rate)
        player.wait()
        device = pick_input_device(sample_rate=mo.stt.sample_rate)
        recorder = ContinuousRecorder(device=device, sample_rate=mo.stt.sample_rate,
                          vad=mo.create_vad())
        if not recorder.start():
            raise RuntimeError(recorder.error)
        print("Stay quiet for calibration.", flush=True)
        threshold = recorder.calibrate(seconds=1)
        print(f"Device={device}, native={recorder.capture_sample_rate}, "
              f"threshold={threshold:.5f}", flush=True)
        interview = ScriptedInterview([
            "We are testing whether your interruption reaches the recording intact.",
            "Now. This sentence should stop as soon as you say your name, "
            "and its unheard ending should never enter the conversation history.",
        ])
        with ThreadPoolExecutor(max_workers=1) as executor:
            options = SimpleNamespace(tts=True, play=True, tts_executor=executor)
            turn, interrupted, _ = _run_turn(
                interview, mo, player, recorder, args.out, 0, options, opening=True
            )
            text = _listen(mo, None, recorder, capture_dir=args.out / "captures",
                           interrupted=interrupted)
        report = {"expected": "My name is Max.", "transcript": text,
                  "interrupted": interrupted, "retained": turn.say,
                  "history": interview.history, "device": device,
                  "native_rate": recorder.capture_sample_rate, "threshold": threshold}
        (args.out / "result.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
        print(json.dumps(report, indent=2), flush=True)
        return 0
    finally:
        player.close()
        if recorder is not None:
            recorder.stop()
        mo.stop()


if __name__ == "__main__":
    raise SystemExit(main())