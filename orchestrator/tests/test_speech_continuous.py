"""Tests for :class:`ContinuousRecorder`, the always-open microphone.

Driven with a fake ``sounddevice`` so no hardware is needed. Idle pre-roll is
bounded; confirmed speech is retained until its consumer acknowledges it.
"""

from __future__ import annotations

import sys
import types

import numpy as np
import pytest

from orchestrator.hosts.speech import ContinuousRecorder

RATE = 16000
BLOCK = 1600  # 100 ms


class _FakeInputStream:
    """Delivers a scripted sequence of blocks through the driver callback.

    The recorder uses a callback stream (a blocking ``read`` returns zeros on
    the virtual devices of a remote-desktop session), so the fake drives the
    callback rather than implementing ``read``.
    """

    def __init__(self, script, state, *, callback=None, **_kwargs):
        self._script = script
        self._state = state
        self._callback = callback

    def start(self):
        self._state["started"] = True
        if self._callback is not None:
            for block in self._script:
                self._callback(block.reshape(-1, 1), len(block), None, None)

    def stop(self):
        self._state["stopped"] = True

    def close(self):
        self._state["closed"] = True


def _install_fake_sounddevice(monkeypatch, script):
    state = {"started": False, "stopped": False, "closed": False}
    module = types.ModuleType("sounddevice")

    def _input_stream(*, blocksize, callback=None, **_kwargs):
        state["callback"] = callback
        state["options"] = {"blocksize": blocksize, **_kwargs}
        return _FakeInputStream(script, state, callback=callback)

    module.InputStream = _input_stream
    monkeypatch.setitem(sys.modules, "sounddevice", module)
    return state


def _silence():
    return np.zeros(BLOCK, dtype=np.float32)


def _speech(level=0.1):
    return np.full(BLOCK, level, dtype=np.float32)


def test_start_opens_the_device_once(monkeypatch):
    state = _install_fake_sounddevice(monkeypatch, [_silence()])
    recorder = ContinuousRecorder(sample_rate=RATE, block_ms=100)

    assert recorder.start()
    assert state["started"]
    assert recorder.sample_rate == recorder.capture_sample_rate == RATE
    assert state["options"]["samplerate"] == RATE

    recorder.stop()
    assert state["stopped"] and state["closed"]


def test_streams_every_block_including_silence(monkeypatch):
    # No segmentation: silence is streamed too, so the recogniser sees the
    # run-up to speech and can endpoint on its own.
    script = [_silence(), _speech(), _silence()]
    _install_fake_sounddevice(monkeypatch, script)
    recorder = ContinuousRecorder(sample_rate=RATE, block_ms=100)

    recorder.start()
    blocks = [recorder.next_block(timeout=2.0) for _ in range(3)]
    recorder.stop()

    assert all(b is not None for b in blocks)
    assert len(blocks[0]) == BLOCK


def test_next_block_returns_none_before_start(monkeypatch):
    _install_fake_sounddevice(monkeypatch, [_silence()])
    recorder = ContinuousRecorder(sample_rate=RATE, block_ms=100)

    # Never started: there is no queue, so this must not raise.
    assert recorder.next_block(timeout=0.1) is None


def test_disabled_recorder_does_not_start(monkeypatch):
    _install_fake_sounddevice(monkeypatch, [_speech()])
    recorder = ContinuousRecorder(enabled=False)

    assert not recorder.start()
    assert recorder.next_block(timeout=0.1) is None


def test_missing_sounddevice_degrades(monkeypatch):
    monkeypatch.setitem(sys.modules, "sounddevice", None)
    recorder = ContinuousRecorder(sample_rate=RATE)

    assert not recorder.start()
    assert not recorder.available


def test_pick_input_device_chooses_the_loudest(monkeypatch):
    # A remote-desktop session exposes the same mic under several backends and
    # the default can be silent, so the picker must choose the one with signal.
    module = types.ModuleType("sounddevice")
    module.query_devices = lambda: [
        {"name": "silent", "max_input_channels": 1},
        {"name": "output only", "max_input_channels": 0},
        {"name": "loud", "max_input_channels": 1},
    ]

    def _input_stream(*, device, callback=None, **_kwargs):
        level = {0: 0.0001, 2: 0.2}.get(device, 0.0)
        block = np.full(BLOCK, level, dtype=np.float32)

        class _Stream:
            def start(self):
                if callback is not None:
                    callback(block.reshape(-1, 1), len(block), None, None)

            def stop(self):
                pass

            def close(self):
                pass

        return _Stream()

    module.InputStream = _input_stream
    monkeypatch.setitem(sys.modules, "sounddevice", module)

    from orchestrator.hosts.speech import pick_input_device

    assert pick_input_device(sample_rate=RATE, seconds=0.0) == 2


def test_pick_input_device_returns_none_without_sounddevice(monkeypatch):
    monkeypatch.setitem(sys.modules, "sounddevice", None)

    from orchestrator.hosts.speech import pick_input_device

    assert pick_input_device() is None


def test_speech_detected_after_a_loud_block(monkeypatch):
    # Barge-in relies on this: a couple of loud blocks latch detection on.
    _install_fake_sounddevice(monkeypatch, [_silence(), _speech(), _speech()])
    recorder = ContinuousRecorder(sample_rate=RATE, block_ms=100, threshold=0.01)

    recorder.start()

    assert recorder.speech_detected()


def test_speech_not_detected_on_silence(monkeypatch):
    _install_fake_sounddevice(monkeypatch, [_silence(), _silence()])
    recorder = ContinuousRecorder(sample_rate=RATE, block_ms=100, threshold=0.01)

    recorder.start()

    assert not recorder.speech_detected()


def test_reset_preserves_unconsumed_speech_and_detection(monkeypatch):
    _install_fake_sounddevice(monkeypatch, [_speech(), _speech()])
    recorder = ContinuousRecorder(sample_rate=RATE, block_ms=100, threshold=0.01)
    recorder.start()
    assert recorder.speech_detected()

    recorder.reset()

    assert recorder.speech_detected()
    np.testing.assert_array_equal(recorder.next_block(timeout=0), _speech())
    np.testing.assert_array_equal(recorder.next_block(timeout=0), _speech())
    recorder.acknowledge()
    assert not recorder.speech_detected()
    assert recorder.next_block(timeout=0) is None


def test_idle_capture_keeps_only_300ms_of_pre_roll(monkeypatch):
    _install_fake_sounddevice(monkeypatch, [_silence()] * 100)
    recorder = ContinuousRecorder(sample_rate=RATE, block_ms=100)
    assert recorder.start()

    blocks = []
    while (block := recorder.next_block(timeout=0)) is not None:
        blocks.append(block)

    assert sum(map(len, blocks)) == 3 * BLOCK
    assert not recorder.speech_detected()
    recorder.stop()


def test_callback_retains_candidates_and_entire_utterance(monkeypatch):
    onset = [_speech(0.02), _speech(0.03), _speech(0.04), _speech(0.05)]
    _install_fake_sounddevice(monkeypatch, [_silence()] * 100 + onset + [_silence()] * 10)
    recorder = ContinuousRecorder(sample_rate=RATE, block_ms=100, start_blocks=3)
    assert recorder.start()
    assert recorder.speech_detected()

    captured = recorder.record_utterance(silence_s=1.0, no_speech_s=0.01)

    np.testing.assert_array_equal(captured, np.concatenate([_silence()] * 3 + onset
                                                         + [_silence()] * 10))
    assert not recorder.speech_detected()
    recorder.stop()


def test_single_callback_noise_spike_does_not_latch(monkeypatch):
    _install_fake_sounddevice(monkeypatch, [_silence()] * 100 + [_speech()] + [_silence()] * 10)
    recorder = ContinuousRecorder(sample_rate=RATE, block_ms=100)
    assert recorder.start()
    assert not recorder.speech_detected()
    assert recorder._buffered_samples == 3 * BLOCK
    recorder.stop()


def test_acknowledgement_preserves_next_queued_turn_without_reset(monkeypatch):
    first = [_speech(0.02)] * 2 + [_silence()] * 2
    second = [_speech(0.03)] * 3 + [_silence()] * 2
    _install_fake_sounddevice(monkeypatch, first + second)
    recorder = ContinuousRecorder(sample_rate=RATE, block_ms=100)
    assert recorder.start()

    np.testing.assert_array_equal(recorder.record_utterance(silence_s=0.2),
                                  np.concatenate(first))
    assert recorder.speech_detected()
    np.testing.assert_array_equal(recorder.record_utterance(silence_s=0.2),
                                  np.concatenate(second))
    assert not recorder.speech_detected()
    recorder.stop()


def test_calibration_clears_old_and_in_flight_detection(monkeypatch):
    state = _install_fake_sounddevice(monkeypatch, [_speech()] * 2)
    recorder = ContinuousRecorder(sample_rate=RATE, block_ms=100)
    assert recorder.start()
    assert recorder.speech_detected()
    original_next_block = recorder.next_block
    injected = False

    def next_block(timeout=None):
        nonlocal injected
        if not injected:
            injected = True
            for _ in range(2):
                state["callback"](_speech().reshape(-1, 1), BLOCK, None, None)
            assert not recorder.speech_detected()
        return original_next_block(timeout=timeout)

    monkeypatch.setattr(recorder, "next_block", next_block)
    assert recorder.calibrate(seconds=0.01) >= 0.01
    recorder.reset()
    assert not recorder.speech_detected()
    assert original_next_block(timeout=0) is None
    for _ in range(2):
        loud = _speech(recorder.threshold * 2)
        state["callback"](loud.reshape(-1, 1), BLOCK, None, None)
    assert recorder.speech_detected()
    recorder.stop()


@pytest.mark.parametrize("native_rate", [44100, 48000])
def test_native_capture_resamples_100ms_blocks_at_model_rate(monkeypatch, native_rate):
    native_block = native_rate // 10
    script = [np.full(native_block, 0.02, dtype=np.float32)] * 20
    state = _install_fake_sounddevice(monkeypatch, script)
    sys.modules["sounddevice"].query_devices = lambda device, kind: {
        "default_samplerate": native_rate,
    }
    recorder = ContinuousRecorder(device=3)
    assert recorder.start()
    assert recorder.sample_rate == RATE
    assert recorder.capture_sample_rate == native_rate
    assert state["options"]["samplerate"] == native_rate
    assert state["options"]["blocksize"] == native_block
    blocks = [recorder.next_block(timeout=0) for _ in script]
    assert all(len(block) == BLOCK and block.dtype == np.float32 for block in blocks)
    np.testing.assert_allclose(np.concatenate(blocks), 0.02)
    recorder.stop()


@pytest.mark.parametrize("native_rate", [8000, 44100, 48000])
def test_resampling_fractional_positions_survive_irregular_callbacks(monkeypatch, native_rate):
    # A ramp proves both fractional phase and interpolation across boundaries.
    waveform = np.linspace(0.02, 0.2, native_rate, dtype=np.float32)
    sizes = [1, 17, 503, 1, 1033, 7]
    script = []
    offset = 0
    while offset < len(waveform):
        size = sizes[len(script) % len(sizes)]
        script.append(waveform[offset:offset + size])
        offset += size
    _install_fake_sounddevice(monkeypatch, script)
    sys.modules["sounddevice"].query_devices = lambda device, kind: {
        "default_samplerate": native_rate,
    }
    recorder = ContinuousRecorder(start_blocks=1)
    assert recorder.start()
    blocks = []
    while (block := recorder.next_block(timeout=0)) is not None:
        blocks.append(block)
    count = (len(waveform) - 1) * RATE // native_rate + 1
    expected = np.interp(np.arange(count) * native_rate / RATE,
                         np.arange(len(waveform)), waveform).astype(np.float32)
    captured = np.concatenate(blocks)
    assert len(captured) == count
    np.testing.assert_allclose(captured, expected, rtol=0, atol=2e-8)
    recorder.stop()


@pytest.mark.parametrize("native_rate", [44100, 48000])
def test_native_capture_preserves_preroll_onset_and_endpoint(monkeypatch, native_rate):
    native_block = native_rate // 10
    native_silence = np.zeros(native_block, dtype=np.float32)
    onset = [np.full(native_block, level, dtype=np.float32)
             for level in (0.02, 0.03, 0.04)]
    _install_fake_sounddevice(monkeypatch, [native_silence] * 100 + onset
                             + [native_silence] * 2)
    sys.modules["sounddevice"].query_devices = lambda device, kind: {
        "default_samplerate": native_rate,
    }
    recorder = ContinuousRecorder(start_blocks=3)
    assert recorder.start()
    assert recorder.speech_detected()
    captured = recorder.record_utterance(silence_s=0.2)
    expected = np.concatenate([_silence()] * 3
                              + [_speech(level) for level in (0.02, 0.03, 0.04)]
                              + [_silence()] * 2)
    np.testing.assert_array_equal(captured, expected)
    assert not recorder.speech_detected()
    recorder.stop()


@pytest.mark.parametrize("metadata", [None, {}, {"default_samplerate": 0},
                                     {"default_samplerate": float("nan")}])
def test_invalid_native_metadata_falls_back_to_model_rate(monkeypatch, metadata):
    state = _install_fake_sounddevice(monkeypatch, [_speech()] * 2)

    def query_devices(device, kind):
        if metadata is None:
            raise RuntimeError("metadata unavailable")
        return metadata

    sys.modules["sounddevice"].query_devices = query_devices
    recorder = ContinuousRecorder()
    assert recorder.start()
    assert recorder.capture_sample_rate == RATE
    assert state["options"]["samplerate"] == RATE
    recorder.stop()


def test_native_callback_copies_buffer_reports_status_and_restarts_phase(monkeypatch):
    state = _install_fake_sounddevice(monkeypatch, [])
    sys.modules["sounddevice"].query_devices = lambda device, kind: {
        "default_samplerate": 44100,
    }
    recorder = ContinuousRecorder(start_blocks=1)
    for _ in range(2):
        assert recorder.start()
        assert recorder.capture_status is None
        buffer = np.full((4410, 1), 0.02, dtype=np.float32)
        state["callback"](buffer, len(buffer), None, "input overflow")
        buffer[:] = 0
        block = recorder.next_block(timeout=0)
        assert len(block) == BLOCK
        np.testing.assert_allclose(block, 0.02)
        assert recorder.capture_status == "input overflow"
        state["callback"](buffer[:0], 0, None, None)
        assert recorder.next_block(timeout=0) is None
        recorder.stop()


@pytest.mark.parametrize("quiet_level, expected", [(0.0, 0.001), (0.0002, 0.001),
                                                 (0.0004, 0.0016)])
def test_calibration_defaults_allow_quiet_remote_speech(monkeypatch, quiet_level, expected):
    state = _install_fake_sounddevice(monkeypatch, [_speech(quiet_level)] * 3)
    recorder = ContinuousRecorder()
    assert recorder.start()
    assert recorder.calibrate(seconds=0.001) == pytest.approx(expected)
    assert not recorder.speech_detected()
    for _ in range(2):
        state["callback"](_speech(0.002).reshape(-1, 1), BLOCK, None, None)
    assert recorder.speech_detected()
    recorder.stop()


@pytest.mark.parametrize("wasapi_peak, expected", [(0.013, 1), (0.0, 0), (1e-8, 0),
                                                (float("nan"), 0), (None, 0)])
def test_picker_prefers_working_wasapi_with_native_probe_and_fallback(
    monkeypatch, wasapi_peak, expected,
):
    from orchestrator.hosts import speech

    module = types.ModuleType("sounddevice")
    module.query_devices = lambda: [
        {"max_input_channels": 1, "hostapi": 0, "default_samplerate": 44100},
        {"max_input_channels": 1, "hostapi": 1, "default_samplerate": 48000},
        {"max_input_channels": 0, "hostapi": 1},
    ]
    module.query_hostapis = lambda: [{"name": "MME"}, {"name": "Windows WASAPI"}]
    monkeypatch.setitem(sys.modules, "sounddevice", module)
    probes = []

    def probe(index: int, rate: int, seconds: float) -> float | None:
        probes.append((index, rate, seconds))
        return [0.026, wasapi_peak][index]

    monkeypatch.setattr(speech, "_probe_input", probe)
    assert speech.pick_input_device(seconds=0.0) == expected
    assert probes == [(0, 44100, 0.0), (1, 48000, 0.0)]


def test_picker_verifies_real_probe_silence_failure_and_quiet_levels(monkeypatch):
    from orchestrator.hosts.speech import pick_input_device

    module = types.ModuleType("sounddevice")
    module.query_devices = lambda: [
        {"max_input_channels": 1, "hostapi": host, "default_samplerate": 48000}
        for host in (0, 1, 1, 1)
    ]
    module.query_hostapis = lambda: [{"name": "MME"}, {"name": "Windows WASAPI"}]

    def input_stream(*, device, samplerate, blocksize, callback, **kwargs):
        assert samplerate == 48000
        assert blocksize == 4800
        if device == 2:
            raise RuntimeError("cannot open WASAPI")
        level = {0: 0.026, 1: 0.0, 3: 0.001}[device]
        return _FakeInputStream([np.full(blocksize, level, dtype=np.float32)], {},
                                callback=callback)

    module.InputStream = input_stream
    monkeypatch.setitem(sys.modules, "sounddevice", module)
    assert pick_input_device(seconds=0.0) == 3


def test_picker_returns_none_when_all_inputs_are_silent(monkeypatch):
    from orchestrator.hosts import speech

    module = types.ModuleType("sounddevice")
    module.query_devices = lambda: [{"max_input_channels": 1}]
    monkeypatch.setitem(sys.modules, "sounddevice", module)
    monkeypatch.setattr(speech, "_probe_input", lambda *args: 0.0)
    assert speech.pick_input_device(seconds=0.0) is None


def test_native_metadata_query_driver_failure_falls_back(monkeypatch):
    state = _install_fake_sounddevice(monkeypatch, [])

    def query_devices(device: int | None, kind: str) -> dict:
        raise OSError("PortAudio metadata unavailable")

    sys.modules["sounddevice"].query_devices = query_devices
    recorder = ContinuousRecorder()
    assert recorder.start()
    assert state["options"]["samplerate"] == RATE
    assert recorder.capture_sample_rate == RATE
    recorder.stop()


def test_picker_handles_device_enumeration_failure(monkeypatch):
    from orchestrator.hosts.speech import pick_input_device

    module = types.ModuleType("sounddevice")

    def query_devices() -> list:
        raise OSError("PortAudio unavailable")

    module.query_devices = query_devices
    monkeypatch.setitem(sys.modules, "sounddevice", module)
    assert pick_input_device(seconds=0.0) is None
