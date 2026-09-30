"""Streaming STT harness: emit text while the player is still talking.

Defines the interface the orchestrator's speech host will implement, and runs
today with no model installed via the ``null`` engine (which replays a WAV in
chunks and prints partial/final hypotheses so the streaming contract is
exercised end to end).

Engines
-------
``null``        Replays a WAV in chunks; emits synthetic partials. Stdlib only.
``sherpa``      [HEAVY] sherpa-onnx online recognizer (Apache-2.0, CPU, C# API).
``vosk``        [HEAVY] Vosk (Apache-2.0, CPU, ~50 MB, C# bindings).
``whisper``     [HEAVY] faster-whisper (MIT). Chunked, not truly streaming --
                included to show the wrapper needed to fake a live mode.

The contract is the same for all engines: feed audio frames, receive
``Partial`` events as they are recognised and a ``Final`` event at an endpoint.

Usage
-----
    python stt_stream.py --wav ..\\sample.wav --engine null --chunk-ms 200
"""

from __future__ import annotations

import argparse
import struct
import time
import wave
from dataclasses import dataclass
from pathlib import Path

SAMPLE_RATE = 16_000  # ASR models expect 16 kHz mono.


@dataclass
class Hypothesis:
    """One recognition result. ``final`` marks an endpointed utterance."""

    text: str
    final: bool
    t: float  # seconds since the stream started


class NullSttEngine:
    """Replays a WAV in chunks and emits synthetic partials.

    This proves the *streaming contract* (partials arrive before the audio ends)
    without any model. The synthetic text is derived from the chunk index so the
    timing of partial vs. final events is visible.
    """

    name = "null"

    def __init__(self, wav_path: Path, chunk_ms: int = 200) -> None:
        self._wav_path = wav_path
        self._chunk_ms = chunk_ms

    def stream(self) -> list[Hypothesis]:
        with wave.open(str(self._wav_path), "rb") as wav:
            rate = wav.getframerate()
            frames_per_chunk = max(1, int(rate * self._chunk_ms / 1000))
            results: list[Hypothesis] = []
            start = time.perf_counter()
            index = 0
            words: list[str] = []
            while True:
                raw = wav.readframes(frames_per_chunk)
                if not raw:
                    break
                # Simulate the recogniser's work on this chunk.
                time.sleep(self._chunk_ms / 1000.0)
                index += 1
                words.append(f"word{index}")
                t = time.perf_counter() - start
                results.append(Hypothesis(" ".join(words), final=False, t=t))
            if words:
                results.append(Hypothesis(" ".join(words), final=True,
                                          t=time.perf_counter() - start))
            return results


# ---------------------------------------------------------------------------
# [HEAVY] Real engines. Uncomment the one you want; each needs its package.
# ---------------------------------------------------------------------------
#
# class SherpaSttEngine:
#     """sherpa-onnx online recognizer: true streaming, Apache-2.0, CPU."""
#     name = "sherpa"
#
#     def __init__(self, model_dir: str, chunk_ms: int = 200) -> None:
#         import sherpa_onnx                        # [HEAVY] pip install sherpa-onnx
#         self._rec = sherpa_onnx.OnlineRecognizer.from_transducer(
#             tokens=f"{model_dir}/tokens.txt",
#             encoder=f"{model_dir}/encoder.onnx",
#             decoder=f"{model_dir}/decoder.onnx",
#             joiner=f"{model_dir}/joiner.onnx",
#             num_threads=2,
#             sample_rate=SAMPLE_RATE,
#         )
#         self._stream = self._rec.create_stream()
#         self._chunk_ms = chunk_ms
#
#     def stream(self) -> list[Hypothesis]:
#         import numpy as np
#         results: list[Hypothesis] = []
#         start = time.perf_counter()
#         for chunk in read_pcm_chunks(self._wav_path, self._chunk_ms):
#             samples = np.frombuffer(chunk, dtype=np.int16).astype(np.float32) / 32768.0
#             self._stream.accept_waveform(SAMPLE_RATE, samples)
#             while self._rec.is_ready(self._stream):
#                 self._rec.decode_stream(self._stream)
#             text = self._rec.get_result(self._stream)
#             results.append(Hypothesis(text, final=False, t=time.perf_counter() - start))
#             if self._rec.is_endpoint(self._stream):
#                 self._rec.reset(self._stream)
#                 results.append(Hypothesis(text, final=True, t=time.perf_counter() - start))
#         return results
#
#
# class VoskSttEngine:
#     """Vosk: Apache-2.0, ~50 MB, streaming API, C# bindings."""
#     name = "vosk"
#
#     def __init__(self, model_dir: str, chunk_ms: int = 200) -> None:
#         from vosk import KaldiRecognizer, Model   # [HEAVY] pip install vosk
#         self._rec = KaldiRecognizer(Model(model_dir), SAMPLE_RATE)
#         self._chunk_ms = chunk_ms
#
#     def stream(self) -> list[Hypothesis]:
#         import json
#         results: list[Hypothesis] = []
#         start = time.perf_counter()
#         for chunk in read_pcm_chunks(self._wav_path, self._chunk_ms):
#             if self._rec.AcceptWaveform(chunk):
#                 text = json.loads(self._rec.Result())["text"]
#                 results.append(Hypothesis(text, final=True, t=time.perf_counter() - start))
#             else:
#                 text = json.loads(self._rec.PartialResult())["partial"]
#                 results.append(Hypothesis(text, final=False, t=time.perf_counter() - start))
#         return results
#
#
# class WhisperSttEngine:
#     """faster-whisper: MIT, best accuracy, but CHUNKED -- not truly streaming.
#
#     To fake a live mode you must buffer audio and re-transcribe a sliding
#     window (see Whisper-Streaming / WhisperLive). Partials therefore lag by a
#     full chunk, which is why a streaming model is preferred for live captions.
#     """
#     name = "whisper"
#
#     def __init__(self, model_size: str = "small", chunk_ms: int = 1000) -> None:
#         from faster_whisper import WhisperModel   # [HEAVY] pip install faster-whisper
#         self._model = WhisperModel(model_size, device="cpu", compute_type="int8")
#         self._chunk_ms = chunk_ms
#
#     def stream(self) -> list[Hypothesis]:
#         # Buffer a sliding window, transcribe it, emit the tail as a partial.
#         ...


def read_pcm_chunks(wav_path: Path, chunk_ms: int) -> list[bytes]:
    """Read a WAV as a list of raw PCM chunks (helper for real engines)."""
    with wave.open(str(wav_path), "rb") as wav:
        frames_per_chunk = max(1, int(wav.getframerate() * chunk_ms / 1000))
        chunks: list[bytes] = []
        while True:
            raw = wav.readframes(frames_per_chunk)
            if not raw:
                break
            chunks.append(raw)
        return chunks


def make_engine(name: str, wav_path: Path, chunk_ms: int) -> object:
    if name == "null":
        return NullSttEngine(wav_path, chunk_ms)
    raise SystemExit(
        f"engine {name!r} is not wired up in this spike; "
        "uncomment its class above and install its package."
    )


def run_stream(engine: object) -> None:
    results: list[Hypothesis] = engine.stream()  # type: ignore[attr-defined]
    print(f"{'time':>8}  {'kind':<7}  text")
    print("-" * 60)
    first_partial: float | None = None
    for h in results:
        if not h.final and first_partial is None:
            first_partial = h.t
        kind = "FINAL" if h.final else "partial"
        print(f"{h.t:8.3f}  {kind:<7}  {h.text}")
    print("-" * 60)
    if first_partial is not None:
        print(f"time to first partial: {first_partial:.3f}s")
    if results:
        print(f"time to final        : {results[-1].t:.3f}s")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--wav", type=Path, required=True, help="16 kHz mono WAV")
    parser.add_argument("--engine", default="null",
                        choices=["null", "sherpa", "vosk", "whisper"])
    parser.add_argument("--chunk-ms", type=int, default=200,
                        help="audio chunk size fed to the recogniser")
    args = parser.parse_args()

    if not args.wav.exists():
        raise SystemExit(f"WAV not found: {args.wav}")

    engine = make_engine(args.engine, args.wav, args.chunk_ms)
    print(f"engine={args.engine}  wav={args.wav}  chunk={args.chunk_ms}ms")
    run_stream(engine)


if __name__ == "__main__":
    main()
