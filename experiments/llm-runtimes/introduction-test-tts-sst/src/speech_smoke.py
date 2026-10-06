"""Real orchestrator speech/abort smoke test; does not open the microphone."""

from __future__ import annotations

import argparse
import time
from pathlib import Path

from main import AudioPlayer, ModelOrchestrator, Modality


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("recording", type=Path)
    args = parser.parse_args()
    mo = ModelOrchestrator()
    player = AudioPlayer()
    try:
        mo.ensure_model(Modality.STT, model_id="sensevoice-small")
        print(f"Recorded-speech replay: {mo.transcribe(args.recording)!r}")
        mo.ensure_model(Modality.TTS, model_id="kokoro-en-v0_19")
        sentence = "The voice pipeline is ready for another test."
        samples, rate = mo.tts.synthesize_samples(sentence)
        before = time.monotonic()
        assert player.play(samples, rate, text=sentence), player.error
        print(f"Playback enqueue: {time.monotonic() - before:.4f}s")
        # Wait on a timer event, not a terminal sleep/poll loop.
        from threading import Event, Timer

        ready = Event()
        timer = Timer(0.25, ready.set)
        timer.start()
        ready.wait()
        timer.join()
        print(f"Before abort: {player.delivery()}")
        before = time.monotonic()
        player.abort()
        print(f"Abort: {time.monotonic() - before:.4f}s; delivery={player.delivery()}")
        return 0
    finally:
        player.close()
        mo.stop()


if __name__ == "__main__":
    raise SystemExit(main())