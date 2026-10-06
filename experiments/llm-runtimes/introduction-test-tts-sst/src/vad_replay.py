"""Offline VAD replay: no microphone or speaker is opened."""

from __future__ import annotations

import argparse
import json
import time
from pathlib import Path

import numpy as np

from main import ModelOrchestrator
from orchestrator.hosts.speech import read_wav


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("session", type=Path)
    args = parser.parse_args()
    mo = ModelOrchestrator()
    try:
        vad = mo.create_vad()
        files = [args.session / "microphone.wav",
                 *sorted((args.session / "model-input").glob("*.npy"))]
        results = []
        for path in files:
            if path.suffix == ".npy":
                audio = np.load(path, allow_pickle=False)
                rate = 16000
            else:
                audio, rate = read_wav(path)
            if rate != 16000:
                positions = np.arange(int(len(audio) * 16000 / rate)) * rate / 16000
                audio = np.interp(positions, np.arange(len(audio)), audio).astype(np.float32)
            vad.reset()
            vad.set_noise_floor(0.0)
            ranges = []
            active = None
            before = time.monotonic()
            for start in range(0, len(audio), 1600):
                detected = vad.process(audio[start:start + 1600])
                if detected and active is None:
                    active = start / 16000
                elif not detected and active is not None:
                    ranges.append([active, start / 16000])
                    active = None
            if active is not None:
                ranges.append([active, len(audio) / 16000])
            result = {"file": str(path), "duration_s": len(audio) / 16000,
                      "speech_ranges_s": ranges,
                      "processing_s": time.monotonic() - before}
            results.append(result)
            print(json.dumps(result), flush=True)
        (args.session / "vad-replay.json").write_text(json.dumps(results, indent=2),
                                                     encoding="utf-8")
        return 0
    finally:
        mo.stop()


if __name__ == "__main__":
    raise SystemExit(main())