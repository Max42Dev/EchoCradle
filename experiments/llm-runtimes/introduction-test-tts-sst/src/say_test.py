"""Audible end-to-end microphone test for the interview.

Everything is cued with **sound**, because during a remote session the user
cannot watch text in real time:

* two low beeps     - get ready
* rising two-tone   - **speak now**
* falling two-tone  - the utterance ended

Uses the orchestrator's real path (``AudioRecorder`` + ``SttHost``), so this
tests the code the game will actually run, not a parallel implementation.

Default sentence is deliberately easy: common words only, nothing a
non-native speaker has to fight with. Pass ``--sentence`` to change it.

Usage::

    python say_test.py                 # one round
    python say_test.py --rounds 3
    python say_test.py --sentence "Call me Alice."
"""

from __future__ import annotations

import argparse
import difflib
import sys
import time
from pathlib import Path

import numpy as np

REPO = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO / "orchestrator"))

from orchestrator import (  # noqa: E402
    AudioPlayer,
    AudioRecorder,
    ModelOrchestrator,
    Modality,
)
from orchestrator.hosts.speech import write_wav  # noqa: E402

HERE = Path(__file__).resolve().parent

DEFAULT_SENTENCE = "Hello, I like music and games."


def tone(freq: float, seconds: float, rate: int = 24000, amplitude: float = 0.35):
    t = np.arange(int(rate * seconds), dtype=np.float32) / rate
    data = amplitude * np.sin(2 * np.pi * freq * t)
    fade = max(1, int(rate * 0.01))
    data[:fade] *= np.linspace(0, 1, fade, dtype=np.float32)
    data[-fade:] *= np.linspace(1, 0, fade, dtype=np.float32)
    return data


def play(player: AudioPlayer, *tones) -> None:
    for samples in tones:
        player.play(samples, 24000)
        player.wait()
        time.sleep(0.05)


def cue_ready(player: AudioPlayer) -> None:
    play(player, tone(600, 0.12), tone(600, 0.12))


def cue_speak(player: AudioPlayer) -> None:
    play(player, tone(800, 0.10), tone(1100, 0.28))


def cue_done(player: AudioPlayer) -> None:
    play(player, tone(1100, 0.10), tone(650, 0.28))


def words(text: str) -> list[str]:
    cleaned = "".join(c.lower() if c.isalnum() or c.isspace() else " " for c in text)
    return [w for w in cleaned.split() if w]


def word_error_rate(expected: str, actual: str) -> float:
    exp, act = words(expected), words(actual)
    if not exp:
        return 0.0
    matcher = difflib.SequenceMatcher(a=exp, b=act)
    correct = sum(block.size for block in matcher.get_matching_blocks())
    return ((len(exp) - correct) + (len(act) - correct)) / len(exp)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Audible microphone test.")
    parser.add_argument("--rounds", type=int, default=1)
    parser.add_argument("--sentence", default=DEFAULT_SENTENCE)
    parser.add_argument("--stt-model", default=None, help="pin an STT model id")
    parser.add_argument(
        "--playback",
        action="store_true",
        help="play each capture back so you can hear what was recorded",
    )
    args = parser.parse_args(argv)

    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")

    print("=" * 74)
    print("  SAY THIS SENTENCE:")
    print()
    print(f"      {args.sentence}")
    print()
    print("  Cues:  beep-beep = get ready   <rising> = SPEAK   <falling> = done")
    print("=" * 74, flush=True)
    mo = ModelOrchestrator(shippable_only=False)
    player = AudioPlayer(enabled=True)
    if not player.available:
        print(f"audio output unavailable: {player.error}")
        return 2

    try:
        stt = mo.ensure_model(Modality.STT, model_id=args.stt_model)
        print(f"\nSTT model: {stt.id} (streaming: {mo.stt.is_streaming})", flush=True)

        print("Calibrating the room - stay quiet for a moment ...", flush=True)
        threshold = AudioRecorder.calibrate(sample_rate=mo.stt.sample_rate)
        print(f"  threshold = {threshold:.5f}", flush=True)

        recorder = AudioRecorder(
            enabled=True,
            sample_rate=mo.stt.sample_rate,
            threshold=threshold,
            silence_s=1.4,
            no_speech_s=12.0,
        )

        summary: list[tuple[int, float, str]] = []
        for round_index in range(args.rounds):
            print(f"\n--- round {round_index + 1}/{args.rounds} ---", flush=True)
            cue_ready(player)
            time.sleep(0.4)
            print(f"  GET READY, then say: {args.sentence}", flush=True)

            partials: list[str] = []
            captured: list[np.ndarray] = []

            def on_ready() -> None:
                # Fires once the microphone is genuinely capturing audio. The
                # cue must come *after* that moment, or the user starts talking
                # while the device is still opening and the first word is lost.
                cue_speak(player)

            def on_block(mono: np.ndarray) -> None:
                # Keep a copy so a bad transcript can be diagnosed afterwards.
                captured.append(np.array(mono, dtype=np.float32))

            text = mo.listen(
                recorder,
                on_partial=lambda p: partials.append(p),
                on_ready=on_ready,
                on_block=on_block,
            )
            cue_done(player)

            if captured:
                audio = np.concatenate(captured)
                path = HERE / f"cap_r{round_index + 1}.wav"
                write_wav(path, audio, recorder.sample_rate)
                peak = float(np.max(np.abs(audio))) if len(audio) else 0.0
                rms = float(np.sqrt(np.mean(audio**2))) if len(audio) else 0.0
                print(f"  captured {len(audio) / recorder.sample_rate:.2f}s  "
                      f"peak={peak:.4f} rms={rms:.5f}  -> {path.name}", flush=True)

                if peak < 0.001:
                    # Nothing above the noise floor reached the device. Playing
                    # that back is just silence, so say what happened instead.
                    print("  MICROPHONE DELIVERED NOTHING (peak below 0.001).",
                          flush=True)
                    print("  Check that your remote-desktop client is sending",
                          flush=True)
                    print("  audio in: the virtual microphone goes silent when it",
                          flush=True)
                    print("  is not.", flush=True)
                elif args.playback:
                    # Play the capture back so the operator can hear exactly
                    # what the recogniser received.
                    cue_ready(player)
                    print("  PLAYBACK - this is what was recorded:", flush=True)
                    gain = min(1.0 / peak, 40.0)
                    play(player, np.clip(audio * gain, -1.0, 1.0))
                    time.sleep(0.2)

            if recorder.error:
                print(f"  recorder error: {recorder.error}", flush=True)
                continue
            if not text:
                print("  nothing recognised", flush=True)
                continue

            wer = word_error_rate(args.sentence, text)
            print(f"  HEARD: {text!r}", flush=True)
            print(f"  WER  : {wer:.0%}", flush=True)
            if partials:
                print(f"  partials: {partials[-1]!r}", flush=True)
            summary.append((round_index + 1, wer, text))

        if summary:
            print("\n" + "=" * 74)
            print("SUMMARY")
            for index, wer, text in summary:
                print(f"  round {index}: WER={wer:>4.0%}  {text!r}")
            mean = sum(w for _i, w, _t in summary) / len(summary)
            print(f"  mean WER: {mean:.0%}")
            print("=" * 74)
    finally:
        player.close()
        mo.stop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
