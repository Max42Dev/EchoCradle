"""Tests for silence trimming ahead of offline recognition.

Whisper loops on padding: given a waveform with trailing silence it can emit
"I would I would I would" indefinitely. Trimming is therefore functional, not
cosmetic, and these tests pin down its behaviour without loading any model.
"""

from __future__ import annotations

import numpy as np
import pytest

from orchestrator.hosts.speech import trim_silence

RATE = 16000


def _silence(seconds: float) -> np.ndarray:
    return np.zeros(int(RATE * seconds), dtype=np.float32)


def _speech(seconds: float, level: float = 0.3) -> np.ndarray:
    return np.full(int(RATE * seconds), level, dtype=np.float32)


def test_leading_and_trailing_silence_are_removed():
    audio = np.concatenate([_silence(1.0), _speech(1.0), _silence(1.0)])

    trimmed = trim_silence(audio, rate=RATE)

    # Speech is 1 s; keep only a small pad either side.
    assert 1.0 * RATE <= len(trimmed) <= 1.5 * RATE


def test_all_silence_is_returned_unchanged():
    # Nothing to trim towards; returning the input avoids an empty decode.
    audio = _silence(2.0)

    assert len(trim_silence(audio, rate=RATE)) == len(audio)


def test_speech_starting_at_zero_is_not_clipped_away():
    audio = _speech(1.0)

    trimmed = trim_silence(audio, rate=RATE)

    assert len(trimmed) >= len(audio) - 1


def test_very_short_audio_is_passed_through():
    audio = _speech(0.005)

    assert len(trim_silence(audio, rate=RATE)) == len(audio)


def test_quiet_speech_above_the_threshold_is_kept():
    # Redirected microphone audio is quiet; the threshold must not discard it.
    audio = np.concatenate([_silence(0.5), _speech(1.0, level=0.006), _silence(0.5)])

    trimmed = trim_silence(audio, rate=RATE)

    assert len(trimmed) < len(audio)
    assert len(trimmed) >= RATE


def test_audio_below_the_threshold_is_untouched():
    # An inaudible signal must not be trimmed to nothing.
    audio = np.concatenate([_silence(0.5), _speech(1.0, level=0.0001), _silence(0.5)])

    assert len(trim_silence(audio, rate=RATE)) == len(audio)


def test_empty_input_is_handled():
    assert trim_silence(np.zeros(0, dtype=np.float32), rate=RATE).size == 0


@pytest.mark.parametrize("rate", [8000, 16000, 24000, 48000])
def test_works_at_different_sample_rates(rate: int):
    audio = np.concatenate([
        np.zeros(rate // 2, dtype=np.float32),
        np.full(rate, 0.3, dtype=np.float32),
        np.zeros(rate // 2, dtype=np.float32),
    ])

    trimmed = trim_silence(audio, rate=rate)

    assert rate <= len(trimmed) <= rate * 1.5
