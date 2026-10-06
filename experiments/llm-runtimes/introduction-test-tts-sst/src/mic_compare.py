"""Record one controlled native-rate input-backend sample without an LLM or TTS."""

from __future__ import annotations

import argparse
import json
import threading
import time
from pathlib import Path

import numpy as np
import sounddevice as sd

from main import ModelOrchestrator, Modality, write_wav


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--backend", choices=("MME", "Windows WASAPI"), required=True)
    parser.add_argument("--seconds", type=float, default=12)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()
    device = next((i for i, d in enumerate(sd.query_devices())
                   if d["max_input_channels"] and "AWS" in d["name"]
                   and sd.query_hostapis(d["hostapi"])["name"] == args.backend), None)
    if device is None:
        raise RuntimeError(f"No AWS microphone found on {args.backend}")
    metadata = dict(sd.query_devices(device, "input"))
    rate = int(metadata["default_samplerate"])
    target_frames = int(args.seconds * rate)
    chunks = []
    statuses = []
    frames_captured = 0
    done = threading.Event()

    def callback(indata, frames, timing, status):
        nonlocal frames_captured
        if done.is_set():
            return
        count = min(frames, target_frames - frames_captured)
        chunks.append(indata[:count, 0].copy())
        frames_captured += count
        if status:
            statuses.append(str(status))
        if frames_captured >= target_frames:
            done.set()

    mo = ModelOrchestrator()
    try:
        mo.ensure_model(Modality.STT, model_id="sensevoice-small")
        print(f"Device {device}: {metadata['name']} / {args.backend} / {rate} Hz", flush=True)
        with sd.InputStream(device=device, samplerate=rate, channels=1, dtype="float32",
                            blocksize=rate // 10, callback=callback):
            print("RECORDING NOW — say the phrase once, then stay quiet.", flush=True)
            if not done.wait(args.seconds + 5):
                raise RuntimeError("Audio driver did not deliver the expected sample count")
        audio = np.concatenate(chunks)
        args.out.mkdir(parents=True, exist_ok=True)
        stem = args.backend.replace(" ", "_").lower()
        write_wav(args.out / f"{stem}_native.wav", audio, rate)
        np.save(args.out / f"{stem}_native.npy", audio, allow_pickle=False)
        prepared = []
        mo.stt.on_decode_audio = lambda samples, sr: prepared.append((samples.copy(), sr))
        transcript = mo.stt.transcribe_utterance(audio, rate)
        for samples, sr in prepared:
            write_wav(args.out / f"{stem}_model_input.wav", samples, sr)
            np.save(args.out / f"{stem}_model_input.npy", samples, allow_pickle=False)
        report = {"backend": args.backend, "device": device, "metadata": metadata,
                  "sample_rate": rate, "duration_s": len(audio) / rate,
                  "rms": float(np.sqrt(np.mean(audio ** 2))),
                  "peak": float(np.max(np.abs(audio))), "driver_statuses": statuses,
                  "transcript": transcript, "completed_at": time.time()}
        (args.out / f"{stem}.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
        print(json.dumps(report, indent=2), flush=True)
        return 0
    finally:
        mo.stop()


if __name__ == "__main__":
    raise SystemExit(main())