"""Tests for audio normalisation.

Redirected microphones (NICE DCV) deliver speech around -40 dBFS. The same
recording transcribes to garbage at its native level and to near-correct text
once amplified, so normalisation is the single biggest accuracy win on this
hardware.
"""

from __future__ import annotations

import numpy as np

from orchestrator.hosts.speech import _RunningAgc, normalise_audio


def test_normalise_scales_peak_to_target():
    quiet = np.full(100, 0.01, dtype=np.float32)

    out = normalise_audio(quiet, target_peak=0.9)

    assert abs(float(np.max(np.abs(out))) - 0.9) < 1e-5


def test_normalise_leaves_loud_audio_alone():
    loud = np.full(100, 0.9, dtype=np.float32)

    out = normalise_audio(loud, target_peak=0.9)

    assert abs(float(np.max(np.abs(out))) - 0.9) < 1e-5


def test_normalise_caps_the_gain():
    # A near-silent recording must not be amplified into pure noise.
    tiny = np.full(100, 1e-6, dtype=np.float32)

    out = normalise_audio(tiny, target_peak=0.9, max_gain=100.0)

    assert float(np.max(np.abs(out))) <= 1e-4 + 1e-9


def test_normalise_handles_silence():
    silence = np.zeros(100, dtype=np.float32)

    out = normalise_audio(silence)

    assert float(np.max(np.abs(out))) == 0.0


def test_running_agc_tracks_the_loudest_block_so_far():
    agc = _RunningAgc(target_peak=0.9)

    first = agc.apply(np.full(10, 0.01, dtype=np.float32))
    assert abs(float(np.max(np.abs(first))) - 0.9) < 1e-5

    # A louder block lowers the gain, so the output stays near the target.
    second = agc.apply(np.full(10, 0.05, dtype=np.float32))
    assert float(np.max(np.abs(second))) <= 0.9 + 1e-5


def test_running_agc_handles_silence():
    agc = _RunningAgc()

    out = agc.apply(np.zeros(10, dtype=np.float32))

    assert float(np.max(np.abs(out))) == 0.0
