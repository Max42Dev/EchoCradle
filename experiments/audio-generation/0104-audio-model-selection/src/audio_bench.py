#!/usr/bin/env python3
"""Audio generation benchmark skeleton for EchoCradle (experiment 0104).

This is deliberately *dependency-light*: it uses the standard-library ``wave``
module so it runs anywhere, including on this machine, with no downloads. It
exists to (a) prove the WAV write/measure path works and (b) show the exact
seams where the heavy text-to-audio runtimes plug in.

The heavy model imports are marked ``[HEAVY]`` below. They are commented out so
the script always starts; uncomment a block *after* the corresponding packages
are installed. Do NOT install them just to run this file.
"""

from __future__ import annotations

import argparse
import math
import struct
import subprocess
import sys
import time
import wave
from pathlib import Path

# ---------------------------------------------------------------------------
# [HEAVY] Real inference backends. Left commented so the script stays stdlib-only.
# Each block needs multi-GB downloads; only enable when the disk budget allows.
# ---------------------------------------------------------------------------
#
# [HEAVY] MusicGen (music) -- pip install torch transformers scipy
# import torch
# from transformers import AutoProcessor, MusicgenForConditionalGeneration
#
#   model = MusicgenForConditionalGeneration.from_pretrained(
#       "facebook/musicgen-small", torch_dtype=torch.float16).to("cuda")
#   processor = AutoProcessor.from_pretrained("facebook/musicgen-small")
#   inputs = processor(text=[prompt], padding=True, return_tensors="pt").to("cuda")
#   audio = model.generate(**inputs, max_new_tokens=int(seconds * 50))
#   sample_rate = model.config.audio_encoder.sampling_rate   # 32000
#   wav = audio[0, 0].cpu().numpy()                          # -> write_wav()
#
# [HEAVY] AudioGen (SFX) -- pip install audiocraft torchaudio
# from audiocraft.models import AudioGen
#
#   model = AudioGen.get_pretrained("facebook/audiogen-medium")
#   model.set_generation_params(duration=seconds)
#   wav = model.generate([prompt])[0].cpu().numpy()          # 16000 Hz mono
#
# [HEAVY] Stable Audio Open (music+SFX) -- pip install diffusers soundfile
# import torch
# from diffusers import StableAudioPipeline
#
#   pipe = StableAudioPipeline.from_pretrained(
#       "stabilityai/stable-audio-open-1.0", torch_dtype=torch.float16).to("cuda")
#   out = pipe(prompt, audio_end_in_s=seconds,
#              num_inference_steps=100).audios[0]
#   wav = out.T.float().cpu().numpy()                        # 44100 Hz stereo
#
# [HEAVY] Speech (TTS) -- pip install kokoro soundfile  (needs espeak-ng)
# from kokoro import KPipeline
#
#   pipeline = KPipeline(lang_code="a")
#   for _, _, audio in pipeline(text, voice="af_heart"):
#       write_wav(Path("speech.wav"), audio, 24000)


def write_wav(path: Path, samples: list[float], sample_rate: int) -> None:
    """Write mono 16-bit PCM. ``samples`` are floats in roughly [-1, 1]."""
    path.parent.mkdir(parents=True, exist_ok=True)
    frames = bytearray()
    for s in samples:
        clamped = max(-1.0, min(1.0, s))
        frames += struct.pack("<h", int(clamped * 32767))
    with wave.open(str(path), "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(sample_rate)
        wav.writeframes(bytes(frames))


def synth_tone(seconds: float, sample_rate: int, freq: float = 440.0) -> list[float]:
    """A placeholder tone so the harness has something real to measure."""
    n = int(seconds * sample_rate)
    # Short fade in/out to avoid clicks.
    return [
        math.sin(2 * math.pi * freq * i / sample_rate)
        * min(1.0, i / (0.05 * sample_rate), (n - i) / (0.05 * sample_rate))
        for i in range(n)
    ]


def _gpu_free_mib() -> str:
    """Best-effort free-VRAM note; never fatal. See experiment 0105."""
    try:
        out = subprocess.run(
            ["nvidia-smi", "--query-gpu=memory.free", "--format=csv,noheader,nounits"],
            capture_output=True,
            text=True,
            check=True,
            timeout=5,
        )
        return f"{out.stdout.strip()} MiB free"
    except Exception:  # noqa: BLE001 - purely informational
        return "unavailable"


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Audio bench skeleton (0104).")
    parser.add_argument("--seconds", type=float, default=10.0,
                        help="Requested clip length in seconds (default: 10)")
    parser.add_argument("--sample-rate", type=int, default=32000,
                        help="Sample rate, Hz (MusicGen default 32000)")
    parser.add_argument("--freq", type=float, default=440.0, help="Tone frequency, Hz")
    parser.add_argument("--out", type=Path, default=Path("out/placeholder.wav"),
                        help="Output WAV path")
    parser.add_argument("--prompt", default="calm dungeon ambience",
                        help="Prompt the real model would receive")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    print(f"prompt           : {args.prompt!r}")
    print(f"GPU              : {_gpu_free_mib()}")

    start = time.perf_counter()
    samples = synth_tone(args.seconds, args.sample_rate, args.freq)
    write_wav(args.out, samples, args.sample_rate)
    elapsed = time.perf_counter() - start

    size_bytes = args.out.stat().st_size
    actual_seconds = len(samples) / args.sample_rate
    print(f"sample rate      : {args.sample_rate} Hz")
    print(f"duration         : {actual_seconds:.3f} s")
    print(f"written          : {args.out} ({size_bytes / 1024:.1f} KiB)")
    print(f"write wall time  : {elapsed * 1000:.1f} ms")
    print("\n[stub] Replace synth_tone() with a [HEAVY] backend above to measure "
          "real latency and VRAM.")
    print("       Use experiment 0105's probe.py --samples N to record the peak.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())