"""Deterministic VAD replay and worker tests: no model download or live audio."""

from __future__ import annotations

import sys
import threading
import types
from pathlib import Path
from typing import Any

import numpy as np
import pytest

from orchestrator.hosts.speech import ContinuousRecorder
from orchestrator.vad import SileroVad

RATE = 16000
BLOCK = 1600


class FakeVad:
    def __init__(self, decisions: list[bool]) -> None:
        self.decisions = iter(decisions)
        self.blocks: list[Any] = []
        self.threads: list[int] = []
        self.resets = 0

    def process(self, block: Any) -> bool:
        self.blocks.append(block)
        self.threads.append(threading.get_ident())
        return next(self.decisions)

    def reset(self) -> None:
        self.resets += 1


def audio(level: float) -> Any:
    return np.full(BLOCK, level, dtype=np.float32)


def recorder_with_fake_device(
    monkeypatch: Any, vad: Any, *, native_rate: int = RATE, **options: Any,
) -> tuple[ContinuousRecorder, dict[str, Any]]:
    state: dict[str, Any] = {}
    module = types.ModuleType("sounddevice")

    class Stream:
        def __init__(self, *, callback: Any, **kwargs: Any) -> None:
            state["callback"] = callback

        def start(self) -> None:
            pass

        def stop(self) -> None:
            state["stopped"] = True

        def close(self) -> None:
            state["closed"] = True

    module.InputStream = Stream
    module.query_devices = lambda *args: {"default_samplerate": native_rate}
    monkeypatch.setitem(sys.modules, "sounddevice", module)
    recorder = ContinuousRecorder(vad=vad, **options)
    assert recorder.start()
    return recorder, state


def deliver(recorder: ContinuousRecorder, state: dict[str, Any], block: Any) -> None:
    """Pace fake callbacks by processed sample counts, not sleeps/polling."""
    with recorder._condition:
        before = recorder._buffered_samples
        # All tests retain either <500ms idle audio or a confirmed/candidate onset.
        state["callback"](block.reshape(-1, 1), len(block), None, None)
        assert recorder._condition.wait_for(
            lambda: recorder._buffered_samples > before or recorder.error is not None,
            timeout=2,
        )


def test_loud_noise_does_not_trigger_but_quiet_speech_does(monkeypatch: Any) -> None:
    vad = FakeVad([False, False, True, True])
    recorder, state = recorder_with_fake_device(monkeypatch, vad, threshold=0.1)
    try:
        deliver(recorder, state, audio(0.2))
        deliver(recorder, state, audio(0.2))
        assert not recorder.speech_detected()
        deliver(recorder, state, audio(0.002))
        assert not recorder.speech_detected()
        deliver(recorder, state, audio(0.002))
        assert recorder.speech_detected()
        assert all(thread != threading.get_ident() for thread in vad.threads)
        assert recorder.next_block(timeout=0) is vad.blocks[0]
        assert recorder._last_block_speech is False
    finally:
        worker = recorder._worker
        recorder.stop()
    assert not worker.is_alive()
    assert state["stopped"] and state["closed"]


@pytest.mark.parametrize("level", [0.002, 0.02, 0.2])
def test_candidate_preroll_and_endpoint_share_one_classification(
    monkeypatch: Any, level: float,
) -> None:
    # Amplitude variants deliberately invert energy decisions at the endpoint.
    blocks = [audio(0)] * 5 + [audio(level)] * 3 + [audio(0.3)] * 2
    vad = FakeVad([False] * 5 + [True] * 3 + [False] * 2)
    recorder, state = recorder_with_fake_device(monkeypatch, vad, start_blocks=3)
    try:
        for block in blocks:
            deliver(recorder, state, block)
        assert recorder.speech_detected()
        captured = recorder.record_utterance(silence_s=0.2, no_speech_s=0)
        np.testing.assert_array_equal(captured, np.concatenate(blocks))
        assert len(vad.blocks) == len(blocks)
        assert not recorder.speech_detected()
    finally:
        recorder.stop()


def test_acknowledge_scans_flags_without_decoding_next_turn(monkeypatch: Any) -> None:
    blocks = [audio(0.002)] * 2 + [audio(0.2)] * 2
    vad = FakeVad([True, True, False, False] * 2)
    recorder, state = recorder_with_fake_device(monkeypatch, vad)
    try:
        for block in blocks * 2:
            deliver(recorder, state, block)
        first = recorder.record_utterance(silence_s=0.2)
        assert recorder.speech_detected()
        recorder.reset()  # Does not reset a stateful decoder between turns.
        assert vad.resets == 1
        second = recorder.record_utterance(silence_s=0.2)
        np.testing.assert_array_equal(first, np.concatenate(blocks))
        np.testing.assert_array_equal(second, first)
        assert len(vad.blocks) == 8
        assert not recorder.speech_detected()
    finally:
        recorder.stop()


def test_idle_vad_capture_retains_half_second_then_candidate_onset(monkeypatch: Any) -> None:
    vad = FakeVad([False] * 20 + [True, True])
    recorder, state = recorder_with_fake_device(monkeypatch, vad)
    try:
        for index in range(22):
            block = audio(0.002 if index >= 20 else 0)
            with recorder._condition:
                state["callback"](block.reshape(-1, 1), len(block), None, None)
                assert recorder._condition.wait_for(
                    lambda: bool(recorder._queue) and recorder._queue[-1][0] is vad.blocks[-1]
                    and len(vad.blocks) == index + 1,
                    timeout=2,
                )
            if index == 19:
                assert recorder._buffered_samples == RATE // 2
            if index == 20:
                assert not recorder.speech_detected()
                assert recorder._buffered_samples == RATE // 2 + BLOCK
        assert recorder.speech_detected()
        assert recorder._buffered_samples == RATE // 2 + 2 * BLOCK
    finally:
        recorder.stop()


@pytest.mark.parametrize("native_rate", [44100, 48000])
def test_worker_receives_copied_model_rate_pcm(monkeypatch: Any, native_rate: int) -> None:
    vad = FakeVad([True])
    recorder, state = recorder_with_fake_device(
        monkeypatch, vad, native_rate=native_rate, start_blocks=1,
    )
    try:
        buffer = np.full((native_rate // 10, 1), 0.002, dtype=np.float32)
        state["callback"](buffer, len(buffer), None, None)
        buffer[:] = 0
        block = recorder.next_block(timeout=2)
        assert len(block) == BLOCK
        assert block is vad.blocks[0]
        np.testing.assert_allclose(block, 0.002)
    finally:
        recorder.stop()


def test_vad_failure_is_visible_and_wakes_consumer(monkeypatch: Any) -> None:
    class BrokenVad:
        def process(self, block: Any) -> bool:
            raise RuntimeError("inference failed")

    recorder, state = recorder_with_fake_device(monkeypatch, BrokenVad())
    try:
        state["callback"](audio(0.2).reshape(-1, 1), BLOCK, None, None)
        assert recorder.next_block(timeout=2) is None
        assert not recorder.available
        assert "inference failed" in recorder.error
        assert recorder.capture_status == recorder.error
        assert not recorder.speech_detected()
    finally:
        recorder.stop()


def test_bounded_worker_queue_overflow_is_visible(monkeypatch: Any) -> None:
    vad = FakeVad([])
    recorder, state = recorder_with_fake_device(monkeypatch, vad)
    try:
        block = np.zeros(recorder._worker_queue_samples + 1, dtype=np.float32)
        state["callback"](block.reshape(-1, 1), len(block), None, None)
        assert not recorder.available
        assert "overflow" in recorder.error
        assert recorder.next_block(timeout=2) is None
        assert not vad.blocks
    finally:
        recorder.stop()


def test_calibration_reset_waits_for_worker_and_flushes_metadata(monkeypatch: Any) -> None:
    entered = threading.Event()
    release = threading.Event()
    reset_done = threading.Event()

    class BlockingVad(FakeVad):
        def process(self, block: Any) -> bool:
            entered.set()
            assert release.wait(2)
            return super().process(block)

        def reset(self) -> None:
            super().reset()
            if self.resets > 1:
                reset_done.set()

    vad = BlockingVad([True, True])
    recorder, state = recorder_with_fake_device(monkeypatch, vad, start_blocks=1)
    calibration = threading.Thread(target=lambda: recorder.calibrate(seconds=0))
    try:
        state["callback"](audio(0.002).reshape(-1, 1), BLOCK, None, None)
        assert entered.wait(2)
        calibration.start()
        assert not reset_done.is_set()
        release.set()
        calibration.join(2)
        assert not calibration.is_alive()
        assert reset_done.is_set()
        assert not recorder.speech_detected()
        assert recorder.next_block(timeout=0) is None
        deliver(recorder, state, audio(0.002))
        assert recorder.speech_detected()
    finally:
        release.set()
        calibration.join(2)
        recorder.stop()


def fake_sherpa(monkeypatch: Any) -> dict[str, Any]:
    state: dict[str, Any] = {"windows": [], "segments": 0, "resets": 0}
    module = types.ModuleType("sherpa_onnx")

    class Config:
        def __init__(self, **options: Any) -> None:
            self.__dict__.update(options)
            state["config"] = self

        def validate(self) -> bool:
            return True

    class Detector:
        def __init__(self, config: Any, **options: Any) -> None:
            state["detector_options"] = options

        def accept_waveform(self, samples: Any) -> None:
            state["windows"].append(samples.copy())
            state["segments"] += 1

        def is_speech_detected(self) -> bool:
            return bool(state["windows"] and np.max(np.abs(state["windows"][-1])) > 0.01)

        def empty(self) -> bool:
            return state["segments"] == 0

        def pop(self) -> None:
            state["segments"] -= 1

        def reset(self) -> None:
            state["resets"] += 1
            state["segments"] = 0

    module.VadModelConfig = Config
    module.SileroVadModelConfig = lambda **kwargs: types.SimpleNamespace(**kwargs)
    module.VoiceActivityDetector = Detector
    monkeypatch.setitem(sys.modules, "sherpa_onnx", module)
    return state


def test_helper_configuration_windowing_gain_and_segment_cleanup(
    monkeypatch: Any, tmp_path: Path,
) -> None:
    state = fake_sherpa(monkeypatch)
    model = tmp_path / "silero.onnx"
    model.touch()
    vad = SileroVad(model)
    config = state["config"]
    assert config.sample_rate == RATE and config.provider == "cpu"
    assert config.silero_vad.window_size == 512
    assert config.silero_vad.threshold == 0.5
    assert config.silero_vad.min_speech_duration == 0.16
    assert config.silero_vad.min_silence_duration == 0.35
    quiet_speech = np.full(1600, 0.002, dtype=np.float32)
    assert vad.process(quiet_speech)
    assert len(state["windows"]) == 3
    assert len(vad._pending) == 64
    assert state["segments"] == 0
    np.testing.assert_allclose(state["windows"][0], 0.1)
    np.testing.assert_allclose(quiet_speech, 0.002)
    vad.reset()
    assert len(vad._pending) == 0
    assert state["resets"] == 1


@pytest.mark.parametrize("level", [0.00001, 0.002, 0.2, 0.9])
def test_gain_replay_variants_are_bounded(
    monkeypatch: Any, tmp_path: Path, level: float,
) -> None:
    state = fake_sherpa(monkeypatch)
    model = tmp_path / "silero.onnx"
    model.touch()
    vad = SileroVad(model)
    vad.process(np.full(512, level, dtype=np.float32))
    peak = float(np.max(np.abs(state["windows"][0])))
    assert peak <= 0.5 + 1e-6
    assert peak <= level * 100 + 1e-6
    assert peak == pytest.approx(min(0.1, level * 100))


def test_measured_ambient_is_not_amplified(monkeypatch: Any, tmp_path: Path) -> None:
    state = fake_sherpa(monkeypatch)
    model = tmp_path / "silero.onnx"
    model.touch()
    vad = SileroVad(model)
    vad.set_noise_floor(0.0002)
    ambient = np.full(512, 0.0002, dtype=np.float32)
    assert not vad.process(ambient)
    np.testing.assert_array_equal(state["windows"][0], ambient)
    assert vad.process(np.full(512, 0.002, dtype=np.float32))


def test_model_rate_and_missing_model_are_validated(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="16000"):
        SileroVad(tmp_path / "missing", sample_rate=8000)
    with pytest.raises(FileNotFoundError):
        SileroVad(tmp_path / "missing")
    with pytest.raises(ValueError, match="16000"):
        ContinuousRecorder(vad=FakeVad([]), sample_rate=48000)