"""Benchmark speech-to-text models on the same audio.

Motivation
----------
`zipformer-en-20m-streaming` is a 20-million-parameter streaming model. On clean
synthesised speech it loses the start of almost every sentence ("The cat sat on
the mat" -> "T SAT ON THE MAT") and sometimes returns nothing at all. That is a
model-accuracy limit, not a microphone or plumbing problem, so the fix is to
choose a better model — with measurements, not impressions.

What this does
--------------
1. Synthesises a fixed corpus of sentences with the local TTS model, so every
   model is judged on byte-identical input.
2. Runs each candidate recogniser over the same audio.
3. Reports word error rate per sentence, plus per-model accuracy and speed.

Streaming models are fed in small chunks and their **partials are recorded**,
because the game wants to react to speech while it is still happening
(interruptions, barge-in). A model that is accurate only after the utterance
ends is less useful than one that is accurate *and* incremental.

Usage::

    python stt_benchmark.py --list
    python stt_benchmark.py --models zipformer-20m whisper-base
    python stt_benchmark.py --models all --out results.json
"""

from __future__ import annotations

import argparse
import difflib
import json
import sys
import tarfile
import time
import urllib.request
import wave
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import numpy as np

REPO = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO / "orchestrator"))

from orchestrator import ModelOrchestrator, Modality  # noqa: E402
from orchestrator.hosts.speech import read_wav, write_wav  # noqa: E402

HERE = Path(__file__).resolve().parent
CACHE = Path.home() / ".cache" / "echocradle-stt"
CORPUS_DIR = CACHE / "corpus"

RELEASE = "https://github.com/k2-fsa/sherpa-onnx/releases/download/asr-models/"


# --------------------------------------------------------------------------- #
# corpus
# --------------------------------------------------------------------------- #

#: Sentences a player would plausibly say during an interview, plus some
#: deliberately awkward ones. Short words at the start are included on purpose:
#: that is exactly where the current model fails.
CORPUS: tuple[str, ...] = (
    "Hello, my name is Max and I like music.",
    "I would like a cup of tea please.",
    "The cat sat on the mat.",
    "One two three four five six seven.",
    "My character should be a wizard.",
    "I want a dark forest with old ruins.",
    "Call me Alice.",
    "Sci-fi, please.",
    "Medieval fantasy.",
    "Yes.",
    "No.",
    "Maybe later.",
    "That sounds good to me.",
    "Can you repeat the question?",
    "I would like to explore the northern mountains.",
    "The blacksmith sells iron swords.",
    "Tell me a story about a dragon.",
    "What do you think about that?",
    "I am not sure yet.",
    "Let us begin the adventure.",
)


@dataclass
class ModelSpec:
    """A candidate recogniser: where to get it and how to load it."""

    key: str
    label: str
    kind: str  # "streaming" | "offline-whisper" | "offline-transducer"
    archive: str
    folder: str
    streaming: bool = False
    notes: str = ""
    files: dict[str, str] = field(default_factory=dict)


MODELS: dict[str, ModelSpec] = {
    "zipformer-20m": ModelSpec(
        key="zipformer-20m",
        label="streaming-zipformer-en-20M (current)",
        kind="streaming",
        archive="sherpa-onnx-streaming-zipformer-en-20M-2023-02-17.tar.bz2",
        folder="sherpa-onnx-streaming-zipformer-en-20M-2023-02-17",
        streaming=True,
        notes="the incumbent; tiny and fast, loses sentence starts",
    ),
    "zipformer-en": ModelSpec(
        key="zipformer-en",
        label="streaming-zipformer-en-2023-06-26",
        kind="streaming",
        archive="sherpa-onnx-streaming-zipformer-en-2023-06-26.tar.bz2",
        folder="sherpa-onnx-streaming-zipformer-en-2023-06-26",
        streaming=True,
        notes="larger streaming zipformer",
    ),
    "whisper-base": ModelSpec(
        key="whisper-base",
        label="whisper-base.en (offline)",
        kind="offline-whisper",
        archive="sherpa-onnx-whisper-base.en.tar.bz2",
        folder="sherpa-onnx-whisper-base.en",
        streaming=False,
        notes="offline only; no partials",
    ),
    "whisper-small": ModelSpec(
        key="whisper-small",
        label="whisper-small.en (offline)",
        kind="offline-whisper",
        archive="sherpa-onnx-whisper-small.en.tar.bz2",
        folder="sherpa-onnx-whisper-small.en",
        streaming=False,
        notes="offline only; bigger and slower",
    ),
    "moonshine-tiny": ModelSpec(
        key="moonshine-tiny",
        label="moonshine-tiny.en (offline)",
        kind="offline-moonshine",
        archive="sherpa-onnx-moonshine-tiny-en-int8.tar.bz2",
        folder="sherpa-onnx-moonshine-tiny-en-int8",
        streaming=False,
        notes="designed for short utterances",
    ),
    "moonshine-base": ModelSpec(
        key="moonshine-base",
        label="moonshine-base.en (offline)",
        kind="offline-moonshine",
        archive="sherpa-onnx-moonshine-base-en-int8.tar.bz2",
        folder="sherpa-onnx-moonshine-base-en-int8",
        streaming=False,
        notes="short utterances, larger",
    ),
    "sensevoice-small": ModelSpec(
        key="sensevoice-small",
        label="sensevoice-small (offline)",
        kind="offline-sensevoice",
        archive="sherpa-onnx-sense-voice-zh-en-ja-ko-yue-2024-07-17.tar.bz2",
        folder="sherpa-onnx-sense-voice-zh-en-ja-ko-yue-2024-07-17",
        streaming=False,
        notes="fast multilingual; strong on short speech",
    ),
}


def ensure_model(spec: ModelSpec, *, quiet: bool = False) -> Path:
    """Download and extract a model archive; return its directory."""
    target = CACHE / spec.folder
    if target.is_dir() and any(target.iterdir()):
        return target

    CACHE.mkdir(parents=True, exist_ok=True)
    archive = CACHE / spec.archive
    if not archive.exists():
        url = RELEASE + spec.archive
        if not quiet:
            print(f"    downloading {spec.archive} ...", flush=True)

        def hook(block: int, size: int, total: int) -> None:
            if quiet or not total:
                return
            pct = min(100, block * size * 100 // total)
            print(f"\r    {pct:3d}%", end="", flush=True)

        urllib.request.urlretrieve(url, archive, reporthook=hook)
        if not quiet:
            print()
    if not quiet:
        print("    extracting ...", flush=True)
    with tarfile.open(archive) as tar:
        tar.extractall(CACHE, filter="data")
    return target


# --------------------------------------------------------------------------- #
# metrics
# --------------------------------------------------------------------------- #

def words(text: str) -> list[str]:
    cleaned = "".join(c.lower() if c.isalnum() or c.isspace() else " " for c in text)
    # Normalise number words against digits, since Whisper writes "1 2 3".
    digits = {"0": "zero", "1": "one", "2": "two", "3": "three", "4": "four",
              "5": "five", "6": "six", "7": "seven", "8": "eight", "9": "nine"}
    return [digits.get(w, w) for w in cleaned.split() if w]


def word_error_rate(expected: str, actual: str) -> float:
    exp, act = words(expected), words(actual)
    if not exp:
        return 0.0
    matcher = difflib.SequenceMatcher(a=exp, b=act)
    correct = sum(block.size for block in matcher.get_matching_blocks())
    return ((len(exp) - correct) + (len(act) - correct)) / len(exp)


# --------------------------------------------------------------------------- #
# recognisers
# --------------------------------------------------------------------------- #

def resample(samples: np.ndarray, from_rate: int, to_rate: int = 16000) -> np.ndarray:
    """Linear resample. Adequate for speech, and avoids a scipy dependency."""
    if from_rate == to_rate or len(samples) == 0:
        return samples.astype(np.float32)
    count = int(len(samples) * to_rate / from_rate)
    positions = np.linspace(0, len(samples) - 1, count)
    return np.interp(positions, np.arange(len(samples)), samples).astype(np.float32)


class Recogniser:
    """Uniform wrapper so every model is driven the same way."""

    def __init__(self, spec: ModelSpec, directory: Path, threads: int = 4) -> None:
        import sherpa_onnx  # noqa: PLC0415

        self.spec = spec
        self.dir = directory
        self.sherpa = sherpa_onnx
        self.streaming = spec.streaming
        self._load(threads)

    def _find(self, *patterns: str, prefer_int8: bool = True) -> str:
        """Find the first file matching any pattern, preferring an int8 build.

        Filenames differ per family: ``encoder-epoch-*.onnx`` for zipformer,
        ``base.en-encoder.onnx`` for Whisper. Each family also ships both
        quantised and full-precision weights; int8 is much faster and is the
        better default for a realtime path.
        """
        for pattern in patterns:
            matches = sorted(self.dir.rglob(pattern))
            if not matches:
                continue
            if prefer_int8:
                quantised = [m for m in matches if ".int8." in m.name]
                if quantised:
                    return str(quantised[0])
            plain = [m for m in matches if ".int8." not in m.name]
            return str((plain or matches)[0])
        raise FileNotFoundError(f"none of {patterns} under {self.dir}")

    def _load(self, threads: int) -> None:
        s = self.sherpa
        if self.spec.kind == "streaming":
            self._recognizer = s.OnlineRecognizer.from_transducer(
                tokens=self._find("tokens.txt", prefer_int8=False),
                encoder=self._find("encoder*.onnx"),
                decoder=self._find("decoder*.onnx"),
                joiner=self._find("joiner*.onnx"),
                num_threads=threads,
                provider="cpu",
                sample_rate=16000,
                feature_dim=80,
                decoding_method="greedy_search",
                enable_endpoint_detection=True,
            )
        elif self.spec.kind == "offline-whisper":
            self._recognizer = s.OfflineRecognizer.from_whisper(
                encoder=self._find("*encoder*.onnx"),
                decoder=self._find("*decoder*.onnx"),
                tokens=self._find("*tokens.txt", prefer_int8=False),
                num_threads=threads,
                language="en",
            )
        elif self.spec.kind == "offline-moonshine":
            self._recognizer = s.OfflineRecognizer.from_moonshine(
                preprocessor=self._find("preprocess.onnx"),
                encoder=self._find("encode*.onnx"),
                uncached_decoder=self._find("uncached_decode*.onnx"),
                cached_decoder=self._find("cached_decode*.onnx"),
                tokens=self._find("tokens.txt", prefer_int8=False),
                num_threads=threads,
            )
        elif self.spec.kind == "offline-sensevoice":
            self._recognizer = s.OfflineRecognizer.from_sense_voice(
                model=self._find("model*.onnx"),
                tokens=self._find("tokens.txt", prefer_int8=False),
                num_threads=threads,
                language="en",
                use_itn=True,
            )
        else:
            raise ValueError(f"unknown kind {self.spec.kind}")

    def transcribe(self, samples: np.ndarray, rate: int, *, chunk_ms: int = 100) -> tuple[str, list[str]]:
        """Return (final_text, partials). Partials are empty for offline models."""
        if self.streaming:
            return self._transcribe_streaming(samples, rate, chunk_ms)
        return self._transcribe_offline(samples, rate), []

    def _transcribe_offline(self, samples: np.ndarray, rate: int) -> str:
        # Offline models are fed 16 kHz audio. A little leading silence is
        # prepended: Whisper can drop or loop on a waveform that starts
        # mid-phoneme.
        audio = resample(samples, rate, 16000)
        padded = np.concatenate(
            [np.zeros(int(0.3 * 16000), dtype=np.float32), audio]
        ) if len(audio) else audio
        stream = self._recognizer.create_stream()
        stream.accept_waveform(16000, padded)
        self._recognizer.decode_stream(stream)
        return stream.result.text.strip()

    def _transcribe_streaming(
        self, samples: np.ndarray, rate: int, chunk_ms: int
    ) -> tuple[str, list[str]]:
        rec = self._recognizer
        # The recogniser was configured for whatever its model expects; feed it
        # at the corpus rate and let it resample once, rather than twice.
        stream = rec.create_stream()
        chunk = max(1, int(rate * chunk_ms / 1000))
        partials: list[str] = []
        segments: list[str] = []
        last = ""

        for start in range(0, len(samples), chunk):
            stream.accept_waveform(rate, samples[start : start + chunk])
            while rec.is_ready(stream):
                rec.decode_stream(stream)
            text = rec.get_result(stream)
            if text and text != last:
                last = text
                partials.append(text)
            if rec.is_endpoint(stream):
                # Keep the words from the segment being closed, or the start
                # of the sentence is lost.
                if text:
                    segments.append(text)
                    last = ""
                rec.reset(stream)

        stream.accept_waveform(rate, np.zeros(int(0.66 * rate), dtype=np.float32))
        stream.input_finished()
        while rec.is_ready(stream):
            rec.decode_stream(stream)
        final = rec.get_result(stream)
        parts = [*segments]
        if final:
            parts.append(final)
        return " ".join(parts).strip(), partials


# --------------------------------------------------------------------------- #
# corpus generation
# --------------------------------------------------------------------------- #

def build_corpus(mo: ModelOrchestrator, *, speaker_id: int = 7) -> list[tuple[str, Path]]:
    """Synthesise every sentence once; reuse the WAVs on later runs."""
    CORPUS_DIR.mkdir(parents=True, exist_ok=True)
    items: list[tuple[str, Path]] = []
    for index, sentence in enumerate(CORPUS):
        path = CORPUS_DIR / f"{index:02d}.wav"
        if not path.exists():
            mo.speak(sentence, path, speaker_id=speaker_id)
        items.append((sentence, path))
    return items


# --------------------------------------------------------------------------- #
# main
# --------------------------------------------------------------------------- #

def benchmark(
    keys: list[str],
    *,
    threads: int = 4,
    chunk_ms: int = 100,
    out: Path | None = None,
    quiet: bool = False,
) -> dict[str, Any]:
    mo = ModelOrchestrator(shippable_only=False)
    results: dict[str, Any] = {"chunk_ms": chunk_ms, "models": {}}
    try:
        print("Preparing TTS for the corpus ...", flush=True)
        mo.ensure_model(Modality.TTS, model_id="kokoro-en-v0_19")
        corpus = build_corpus(mo)
        total_audio = sum(
            len(read_wav(path)[0]) / read_wav(path)[1] for _text, path in corpus
        )
        print(f"  {len(corpus)} sentences, {total_audio:.1f}s of audio\n", flush=True)

        for key in keys:
            spec = MODELS[key]
            print(f"== {spec.label} ==", flush=True)
            directory = ensure_model(spec, quiet=quiet)

            load_start = time.monotonic()
            recogniser = Recogniser(spec, directory, threads=threads)
            load_s = time.monotonic() - load_start

            wer_sum = 0.0
            decode_s = 0.0
            partial_counts: list[int] = []
            rows: list[dict[str, Any]] = []
            for sentence, path in corpus:
                samples, rate = read_wav(path)
                start = time.monotonic()
                text, partials = recogniser.transcribe(samples, rate, chunk_ms=chunk_ms)
                elapsed = time.monotonic() - start
                wer = word_error_rate(sentence, text)
                wer_sum += wer
                decode_s += elapsed
                partial_counts.append(len(partials))
                rows.append({
                    "expected": sentence,
                    "heard": text,
                    "wer": round(wer, 4),
                    "seconds": round(elapsed, 3),
                    "partials": len(partials),
                    "last_partial": partials[-1] if partials else "",
                })

            mean_wer = wer_sum / len(corpus)
            entry = {
                "label": spec.label,
                "streaming": spec.streaming,
                "mean_wer": round(mean_wer, 4),
                "exact_matches": sum(1 for r in rows if r["wer"] == 0),
                "load_seconds": round(load_s, 2),
                "decode_seconds": round(decode_s, 2),
                "realtime_factor": round(total_audio / decode_s, 2) if decode_s else 0.0,
                "mean_partials": round(sum(partial_counts) / len(partial_counts), 1),
                "rows": rows,
            }
            results["models"][key] = entry

            print(f"  mean WER      : {mean_wer:.0%}")
            print(f"  exact matches : {entry['exact_matches']}/{len(corpus)}")
            print(f"  realtime x    : {entry['realtime_factor']}")
            print(f"  partials/utt  : {entry['mean_partials']}")
            print(f"  load          : {load_s:.1f}s\n", flush=True)

            for row in rows:
                if row["wer"] > 0:
                    print(f"    WER {row['wer']:>4.0%}  want {row['expected']!r}")
                    print(f"              heard {row['heard']!r}")
            print(flush=True)

        if out:
            out.write_text(json.dumps(results, indent=2), encoding="utf-8")
            print(f"wrote {out}")
    finally:
        mo.stop()
    return results


def print_summary(results: dict[str, Any]) -> None:
    print("=" * 78)
    print(f"{'model':<38} {'WER':>6} {'exact':>7} {'RTx':>6} {'partials':>9}")
    print("-" * 78)
    ranked = sorted(results["models"].items(), key=lambda kv: kv[1]["mean_wer"])
    for key, entry in ranked:
        stream = "S" if entry["streaming"] else "-"
        print(f"{entry['label'][:37]:<38} {entry['mean_wer']:>5.0%} "
              f"{entry['exact_matches']:>3}/{len(CORPUS):<3} "
              f"{entry['realtime_factor']:>6.2f} {entry['mean_partials']:>4}  {stream}")
    print("=" * 78)
    best = ranked[0][1]
    print(f"best by WER: {best['label']}  ({best['mean_wer']:.0%})")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Benchmark STT models on fixed audio.")
    parser.add_argument("--models", nargs="+", default=["all"],
                        help="model keys, or 'all'")
    parser.add_argument("--list", action="store_true", help="list candidate models")
    parser.add_argument("--threads", type=int, default=4)
    parser.add_argument("--chunk-ms", type=int, default=100,
                        help="streaming feed size (default 100)")
    parser.add_argument("--out", type=Path, default=None, help="write results JSON")
    parser.add_argument("--quiet", action="store_true", help="no download progress")
    args = parser.parse_args(argv)

    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")

    if args.list:
        print("candidate models:")
        for key, spec in MODELS.items():
            kind = "streaming" if spec.streaming else "offline"
            print(f"  {key:<18} {kind:<10} {spec.label}")
            print(f"                     {spec.notes}")
        return 0

    keys = list(MODELS) if "all" in args.models else args.models
    unknown = [k for k in keys if k not in MODELS]
    if unknown:
        print(f"unknown model(s): {unknown}")
        return 2

    results = benchmark(keys, threads=args.threads, chunk_ms=args.chunk_ms,
                        out=args.out, quiet=args.quiet)
    print_summary(results)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
