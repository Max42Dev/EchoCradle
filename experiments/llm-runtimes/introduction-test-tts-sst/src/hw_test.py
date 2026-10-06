"""Hardware / setup test: play a phrase while recording the mic, then transcribe.

This is deliberately independent of the interview. It answers one question:
*does audio come out of the speakers and go into the microphone, and can the
recogniser read what it captured?*

What it does:

1. Loads the TTS and STT models through the orchestrator.
2. Opens the microphone and starts recording.
3. Plays a known phrase through the speakers **while** recording.
4. Records for ``--seconds`` (default 10), printing a live level meter.
5. Saves the recording and transcribes it.

How to read the result:

* Transcript matches the spoken phrase -> the mic is a **loopback** of the
  output (it hears the AI, not you). Fine for a smoke test, wrong for dialogue.
* Transcript matches what *you* said -> the mic is your real microphone.
* Empty transcript and a flat level meter -> the mic is not delivering audio
  (check remote-desktop microphone redirection).

Usage::

    python hw_test.py
    python hw_test.py --seconds 10 --device 1
    python hw_test.py --phrase "the quick brown fox jumps over the lazy dog"
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

# The orchestrator lives at the repo root; make it importable when run directly.
# src/ -> introduction-test-tts-sst/ -> llm-runtimes/ -> experiments/ -> repo root
_REPO_ROOT = Path(__file__).resolve().parents[4]
_ORCHESTRATOR_PARENT = _REPO_ROOT / "orchestrator"
if _ORCHESTRATOR_PARENT.is_dir() and str(_ORCHESTRATOR_PARENT) not in sys.path:
    sys.path.insert(0, str(_ORCHESTRATOR_PARENT))

import numpy as np  # noqa: E402

from orchestrator import (  # noqa: E402
    AudioPlayer,
    ModelOrchestrator,
    Modality,
    pick_input_device,
    write_wav,
)  # noqa: E402
from orchestrator.errors import OrchestratorError  # noqa: E402

DEFAULT_TTS_MODEL = "kokoro-en-v0_19"
DEFAULT_SPEAKER_ID = 7  # bf_emma
#: Offline model: it decodes the whole 10s clip, so leading silence does not
#: make it endpoint early the way the streaming recogniser does.
DEFAULT_STT_MODEL = "sensevoice-small"
DEFAULT_PHRASE = "the quick brown fox jumps over the lazy dog"
OUT_DIR = Path(__file__).resolve().parent.parent / "out" / "hw_test"


def run(args: argparse.Namespace) -> int:
    OUT_DIR.mkdir(parents=True, exist_ok=True)

    print("Probing this machine ...")
    mo = ModelOrchestrator(shippable_only=args.shippable_only)
    try:
        return _run(args, mo)
    finally:
        mo.stop()


def _run(args: argparse.Namespace, mo: ModelOrchestrator) -> int:
    probe = mo.report
    print(
        f"  tier {probe.tier}  |  "
        f"{probe.total_vram_gb:.1f} GB VRAM  |  {probe.ram_gb:.1f} GB RAM"
    )

    print("\nLoading models ...")
    try:
        mo.ensure_model(Modality.TTS, model_id=args.tts_model)
        mo.ensure_model(Modality.STT, model_id=args.stt_model)
    except OrchestratorError as exc:
        print(f"\nCannot run on this machine: {exc}", file=sys.stderr)
        return 2
    print(f"  tts -> {args.tts_model} (speaker {args.speaker_id})")
    print(f"  stt -> {args.stt_model}")

    # Synthesize the phrase up front so playback starts the instant we record.
    samples, rate = mo.tts.synthesize_samples(args.phrase)
    phrase_wav = OUT_DIR / "phrase.wav"
    write_wav(phrase_wav, samples, rate)
    print(f"\nPhrase ({len(samples) / rate:.1f}s): {args.phrase!r}")
    print(f"  written to {phrase_wav}")

    # Capture with sounddevice directly rather than ContinuousRecorder: this is
    # a hardware test, and the recorder's fixed blocksize is rejected by some
    # virtual devices (they return zeros instantly instead of blocking).
    import sounddevice as sd  # noqa: PLC0415

    player = AudioPlayer(device=args.output_device)
    rate_in = 16000
    frames = int(args.seconds * rate_in)
    device = args.device
    if device is None:
        device = pick_input_device(sample_rate=rate_in)
        print(f"  (auto-picked input device {device})")
    print(f"\nRecording for {args.seconds:.0f}s — speak now, and listen for the phrase.")
    try:
        rec = sd.rec(frames, samplerate=rate_in, channels=1, dtype="float32",
                     device=device)
    except Exception as exc:  # noqa: BLE001
        print(f"\nCannot open microphone: {exc}", file=sys.stderr)
        return 2

    # Play the phrase a moment in, so the first second is clean room tone.
    time.sleep(1.0)
    played = player.play(samples, rate)
    if not played:
        print(f"\n  [play] unavailable: {player.error}")
    sd.wait()
    player.wait()
    player.close()

    audio = np.asarray(rec, dtype=np.float32).reshape(-1)
    peak_so_far = float(np.max(np.abs(audio))) if len(audio) else 0.0
    rms = float(np.sqrt(np.mean(audio**2))) if len(audio) else 0.0
    print(f"\n  [mic] rms={rms:.4f} peak={peak_so_far:.4f}")

    rec_wav = OUT_DIR / "recording.wav"
    write_wav(rec_wav, audio, rate_in)
    seconds = len(audio) / rate_in
    print(f"\nCaptured {seconds:.1f}s -> {rec_wav}")
    print(f"  peak={peak_so_far:.4f}  (speech is usually > 0.05; < 0.01 is too quiet)")

    print("\nTranscribing ...")
    text = mo.stt.transcribe_utterance(audio, rate_in)
    print(f"\n  transcript: {text!r}")
    print(f"  expected  : {args.phrase!r}")
    if not text.strip():
        print("\n  -> Nothing recognised. The mic is not delivering usable audio.")
    elif text.strip().lower() == args.phrase.lower():
        print("\n  -> Matches the played phrase: the mic is a LOOPBACK of the output.")
    else:
        print("\n  -> Heard something else: the mic is capturing your voice.")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--seconds", type=float, default=10.0, help="record duration")
    parser.add_argument("--phrase", default=DEFAULT_PHRASE, help="phrase to play")
    parser.add_argument("--device", type=int, default=None, help="input device index")
    parser.add_argument("--output-device", type=int, default=None, help="output device index")
    parser.add_argument("--tts-model", default=DEFAULT_TTS_MODEL)
    parser.add_argument("--stt-model", default=DEFAULT_STT_MODEL)
    parser.add_argument("--speaker-id", type=int, default=DEFAULT_SPEAKER_ID)
    parser.add_argument("--shippable-only", action="store_true")
    return run(parser.parse_args())


if __name__ == "__main__":
    raise SystemExit(main())
