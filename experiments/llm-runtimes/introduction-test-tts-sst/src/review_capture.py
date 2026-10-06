"""Compare saved utterance boundaries and offline STT preprocessing variants."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

import numpy as np

from main import ModelOrchestrator, Modality, write_wav
from orchestrator.hosts.speech import normalise_audio, read_wav, trim_silence


def locate(audio: Any, continuous: Any) -> int | None:
    """Find an exact PCM subsequence using a non-silent anchor."""
    loud = np.flatnonzero(np.abs(audio) > 0.02)
    if not len(loud):
        return None
    anchor = int(loud[0])
    for candidate in np.flatnonzero(continuous == audio[anchor]):
        start = int(candidate) - anchor
        if start < 0 or start + len(audio) > len(continuous):
            continue
        if np.array_equal(continuous[start:start + len(audio)], audio):
            return start
    return None


def decode(mo: ModelOrchestrator, audio: Any, rate: int) -> str:
    """Bypass preprocessing intentionally to isolate its effects on the same model."""
    recognizer = mo.stt._recognizer
    stream = recognizer.create_stream()
    stream.accept_waveform(rate, audio)
    recognizer.decode_stream(stream)
    return str(stream.result.text).strip()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("session", type=Path)
    args = parser.parse_args()
    output = args.session / "audio-review"
    output.mkdir(exist_ok=True)
    continuous, rate = read_wav(args.session / "microphone.wav")
    events = [json.loads(line) for line in
              (args.session / "events.jsonl").read_text(encoding="utf-8").splitlines()]
    transcripts = [event["text"] for event in events if event["event"] == "transcript"]
    mo = ModelOrchestrator()
    results: list[dict[str, Any]] = []
    try:
        mo.ensure_model(Modality.STT, model_id="sensevoice-small")
        import sounddevice as sd

        print("Input devices (metadata only; microphone not opened):")
        for index, device in enumerate(sd.query_devices()):
            if device["max_input_channels"]:
                api = sd.query_hostapis(device["hostapi"])["name"]
                print(f"  {index}: {device['name']} / {api} / "
                      f"default rate {device['default_samplerate']}")
        for index, path in enumerate(sorted((args.session / "captures").glob("*.wav"))):
            audio, audio_rate = read_wav(path)
            assert rate == audio_rate == 16000
            start = locate(audio, continuous)
            trimmed = trim_silence(audio)
            normalized = normalise_audio(audio)
            processed = normalise_audio(trimmed)
            variants = {"raw": audio, "trim_only": trimmed,
                        "normalize_only": normalized, "previous_production": processed}
            result: dict[str, Any] = {
                "file": path.name, "live_transcript": transcripts[index],
                "duration_s": len(audio) / rate,
                "continuous_exact_match": start is not None,
                "continuous_start_s": start / rate if start is not None else None,
                "trim_removed_s": (len(audio) - len(trimmed)) / rate,
                "variants": {},
                "current_host_transcript": mo.stt.transcribe_utterance(audio, rate),
            }
            if start is not None:
                # Additional context tests whether capture cut out speech.
                extended = continuous[max(0, start - rate):
                                      min(len(continuous), start + len(audio) + rate)]
                variants["extended_context"] = normalise_audio(trim_silence(extended))
            for name, waveform in variants.items():
                result["variants"][name] = decode(mo, waveform, rate)
                write_wav(output / f"{path.stem}_{name}.wav", waveform, rate)
            results.append(result)
            print(json.dumps(result, ensure_ascii=False), flush=True)
        (output / "comparison.json").write_text(
            json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        return 0
    finally:
        mo.stop()


if __name__ == "__main__":
    raise SystemExit(main())