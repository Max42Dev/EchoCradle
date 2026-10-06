"""Tests for the live-microphone recorder's endpointing logic.

These drive :meth:`AudioRecorder.record_utterance` with a **fake
``sounddevice``** so the behaviour is verified without real hardware. That
matters because CI and dev containers have no microphone: the logic still has
to be provably correct, and the AWS virtual loopback device on a VM is too
intermittent to test against reliably.
"""

from __future__ import annotations

import sys
import types

import numpy as np
import pytest

from orchestrator.hosts.speech import AudioRecorder

RATE = 16000
BLOCK = 1600  # 100 ms


class _FakeInputStream:
    """Yields a scripted sequence of blocks, then repeats the last one."""

    def __init__(self, script, state, **_kwargs):
        self._script = script
        self._state = state
        self._index = 0

    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        return False

    def read(self, frames):
        index = min(self._index, len(self._script) - 1)
        self._index += 1
        self._state["blocks_read"] += 1
        mono = self._script[index]
        return mono.reshape(-1, 1), False


def _install_fake_sounddevice(monkeypatch, script):
    """Route ``import sounddevice`` inside the recorder to our fake."""
    state = {"blocks_read": 0}
    module = types.ModuleType("sounddevice")

    def _input_stream(*, blocksize, **_kwargs):
        return _FakeInputStream(script, state)

    module.InputStream = _input_stream
    module._state = state  # exposed so tests can inspect how much was read
    monkeypatch.setitem(sys.modules, "sounddevice", module)
    return state


def _silence():
    return np.zeros(BLOCK, dtype=np.float32)


def _speech(level=0.1):
    return np.full(BLOCK, level, dtype=np.float32)


def test_nothing_above_threshold_returns_none(monkeypatch):
    _install_fake_sounddevice(monkeypatch, [_silence()])
    recorder = AudioRecorder(sample_rate=RATE, block_ms=100)

    assert recorder.record_utterance() is None


def test_speech_then_silence_captures_one_utterance(monkeypatch):
    # Two loud blocks open the utterance, then enough quiet blocks to stop.
    script = [_speech(), _speech()] + [_silence()] * 20
    _install_fake_sounddevice(monkeypatch, script)
    recorder = AudioRecorder(sample_rate=RATE, block_ms=100, silence_s=1.2)

    captured = recorder.record_utterance()

    assert captured is not None
    # Streaming starts at the first block, so the onset is never dropped.
    assert len(captured) == BLOCK * 2 + BLOCK * 12


def test_on_block_receives_blocks_before_speech_may_start(monkeypatch):
    # The authority the recogniser sees must include the run-up to speech.
    script = [_silence(), _speech(), _speech()] + [_silence()] * 20
    _install_fake_sounddevice(monkeypatch, script)
    recorder = AudioRecorder(sample_rate=RATE, block_ms=100, silence_s=1.2)
    seen: list[int] = []

    recorder.record_utterance(on_block=lambda block: seen.append(len(block)))

    # One leading silent block was streamed before the utterance began.
    assert len(seen) >= 3


def test_single_loud_block_does_not_open_utterance(monkeypatch):
    # One click, then silence: not enough to count as speech starting.
    script = [_speech()] + [_silence()] * 50
    _install_fake_sounddevice(monkeypatch, script)
    recorder = AudioRecorder(sample_rate=RATE, block_ms=100)

    assert recorder.record_utterance() is None


def test_no_speech_gives_up_before_max_duration(monkeypatch):
    # Silence should return promptly instead of waiting out the full budget.
    _install_fake_sounddevice(monkeypatch, [_silence()])
    recorder = AudioRecorder(
        sample_rate=RATE, block_ms=100, no_speech_s=1.0, max_utterance_s=30.0
    )
    state = sys.modules["sounddevice"]._state  # type: ignore[attr-defined]

    assert recorder.record_utterance() is None
    assert state["blocks_read"] <= 20


def test_continuous_speech_stops_at_max_duration(monkeypatch):
    _install_fake_sounddevice(monkeypatch, [_speech()])
    recorder = AudioRecorder(
        sample_rate=RATE, block_ms=100, max_utterance_s=1.0, silence_s=5.0
    )

    captured = recorder.record_utterance()

    assert captured is not None
    # 1.0 s cap at 100 ms blocks: at most ~10 blocks, never unbounded.
    assert len(captured) <= BLOCK * 11


def test_on_block_called_while_recording(monkeypatch):
    script = [_speech(), _speech()] + [_silence()] * 20
    _install_fake_sounddevice(monkeypatch, script)
    recorder = AudioRecorder(sample_rate=RATE, block_ms=100, silence_s=1.2)
    seen: list[int] = []

    recorder.record_utterance(on_block=lambda block: seen.append(len(block)))

    # Streaming depends on blocks arriving during capture, not at the end.
    assert len(seen) >= 2
    assert all(count == BLOCK for count in seen)


def test_on_ready_fires_before_any_block_is_delivered(monkeypatch):
    # The cue to start speaking must come only once the device is capturing.
    script = [_speech(), _speech()] + [_silence()] * 20
    _install_fake_sounddevice(monkeypatch, script)
    recorder = AudioRecorder(sample_rate=RATE, block_ms=100, silence_s=1.2)
    order: list[str] = []

    recorder.record_utterance(
        on_ready=lambda: order.append("ready"),
        on_block=lambda _block: order.append("block"),
    )

    assert order[0] == "ready"
    assert "block" in order


def test_on_ready_is_optional(monkeypatch):
    script = [_speech(), _speech()] + [_silence()] * 20
    _install_fake_sounddevice(monkeypatch, script)
    recorder = AudioRecorder(sample_rate=RATE, block_ms=100, silence_s=1.2)

    assert recorder.record_utterance() is not None


def test_disabled_recorder_returns_none_without_touching_hardware(monkeypatch):
    _install_fake_sounddevice(monkeypatch, [_speech()])
    recorder = AudioRecorder(enabled=False)

    assert recorder.record_utterance() is None


def test_missing_sounddevice_degrades_to_unavailable(monkeypatch):
    monkeypatch.setitem(sys.modules, "sounddevice", None)
    recorder = AudioRecorder(sample_rate=RATE)

    assert recorder.record_utterance() is None
    assert not recorder.available
