"""Diagnostic recorder tests without opening audio hardware."""

from __future__ import annotations

import sys
import wave
from pathlib import Path
from types import SimpleNamespace

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from diagnostic_run import MicrophoneCapture


class Capture:
    def __init__(self, directory: Path) -> None:
        self.directory = directory
        self.events = []

    def event(self, kind, **data):
        self.events.append((kind, data))


def test_writer_preserves_pcm_and_reports_driver_overflow(tmp_path):
    capture = Capture(tmp_path)
    writer = MicrophoneCapture(capture, 16000)
    audio = np.full((1600, 1), 0.25, dtype=np.float32)
    writer.enqueue(audio, 1600, SimpleNamespace(inputBufferAdcTime=2.0, currentTime=2.1),
                   SimpleNamespace(input_overflow=True))
    audio.fill(0)  # diagnostic buffer owns a copy
    writer.close()
    with wave.open(str(tmp_path / "microphone.wav"), "rb") as wav:
        samples = np.frombuffer(wav.readframes(wav.getnframes()), dtype="<i2")
    assert len(samples) == 1600
    assert np.all(samples == int(0.25 * 32767))
    block = next(data for kind, data in capture.events if kind == "microphone_block")
    assert block["input_overflow"]
    assert block["sample_start"] == 0
    assert block["wav_sample_end"] == 1600
    summary = capture.events[-1][1]
    assert summary["input_overflow_blocks"] == 1
    assert summary["dropped_samples"] == 0


def test_oversize_blocks_are_reported_not_buffered(tmp_path):
    capture = Capture(tmp_path)
    writer = MicrophoneCapture(capture, 16000, max_block_bytes=10)
    writer.enqueue(np.zeros((1600, 1), dtype=np.float32), 1600, None, None)
    writer.close()
    summary = capture.events[-1][1]
    assert summary["dropped_blocks"] == 1
    assert summary["dropped_samples"] == 1600
    assert not writer._worker.is_alive()