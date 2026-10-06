"""Tests for :class:`AudioPlayer`.

Driven with a **fake ``sounddevice``** so no audio hardware is needed.
"""

from __future__ import annotations

import sys
import types

import numpy as np
import pytest

from orchestrator.hosts.speech import AudioPlayer

RATE = 24000


class _FakeOutputStream:
    """Records lifecycle calls so tests can assert on them."""

    def __init__(self, state, **kwargs):
        self._state = state
        self._open = False
        self.callback = kwargs["callback"]
        state["opened"] += 1
        state["streams"].append(self)

    def start(self):
        self._open = True

    def pump(self, frames, *, latency=0.0):
        output = np.zeros((frames, 1), dtype=np.float32)
        timing = types.SimpleNamespace(currentTime=0.0, outputBufferDacTime=latency)
        self.callback(output, frames, timing, None)
        self._state["written"] += frames
        return output

    def stop(self):
        self._open = False

    def abort(self):
        self._open = False
        self._state["aborted"] += 1

    def close(self):
        self._open = False
        self._state["closed"] += 1


def _install_fake_sounddevice(monkeypatch):
    state = {"opened": 0, "written": 0, "aborted": 0, "closed": 0, "streams": []}
    module = types.ModuleType("sounddevice")

    def _output_stream(**kwargs):
        return _FakeOutputStream(state, **kwargs)

    module.OutputStream = _output_stream
    monkeypatch.setitem(sys.modules, "sounddevice", module)
    return state


def test_play_opens_a_stream_and_writes(monkeypatch):
    state = _install_fake_sounddevice(monkeypatch)
    player = AudioPlayer()

    assert player.play(np.zeros(100, dtype=np.float32), RATE)
    assert state["opened"] == 1
    assert state["written"] == 0  # play queues without blocking/writing.
    state["streams"][0].pump(100)
    assert state["written"] == 100


def test_close_releases_the_stream(monkeypatch):
    state = _install_fake_sounddevice(monkeypatch)
    player = AudioPlayer()
    player.play(np.zeros(100, dtype=np.float32), RATE)

    player.close()

    assert state["closed"] == 1
    assert state["aborted"] == 1


def test_play_after_close_reopens_a_fresh_stream(monkeypatch):
    state = _install_fake_sounddevice(monkeypatch)
    player = AudioPlayer()

    player.play(np.zeros(100, dtype=np.float32), RATE)
    player.close()
    assert player.play(np.zeros(100, dtype=np.float32), RATE)
    assert state["opened"] == 2
    assert player.available


def test_abort_closes_the_stream_so_queued_audio_is_dropped(monkeypatch):
    # Barge-in: the rest of the line must not be heard, so the stream is closed
    # rather than paused (a paused stream still holds the queued audio).
    state = _install_fake_sounddevice(monkeypatch)
    player = AudioPlayer()
    player.play(np.zeros(RATE, dtype=np.float32), RATE)

    player.abort()

    assert state["closed"] == 1
    assert not player.playing


def test_playing_is_false_before_any_audio(monkeypatch):
    _install_fake_sounddevice(monkeypatch)
    player = AudioPlayer()

    assert not player.playing


def test_wait_returns_true_when_should_stop_fires(monkeypatch):
    _install_fake_sounddevice(monkeypatch)
    player = AudioPlayer()
    # A long buffer so the wait would otherwise block for a while.
    player.play(np.zeros(RATE * 5, dtype=np.float32), RATE)

    assert player.wait(should_stop=lambda: True) is True


def test_wait_returns_false_when_nothing_stops_it(monkeypatch):
    _install_fake_sounddevice(monkeypatch)
    player = AudioPlayer()
    player.play(np.zeros(10, dtype=np.float32), RATE)
    # Drive the callback rather than a blocking write.
    player._stream.pump(10)

    assert player.wait(should_stop=lambda: False) is False


def test_partial_delivery_excludes_driver_buffer_and_survives_abort(monkeypatch):
    clock = [100.0]
    monkeypatch.setattr("time.monotonic", lambda: clock[0])
    state = _install_fake_sounddevice(monkeypatch)
    player = AudioPlayer()
    player.play(np.ones(RATE), RATE, text="A complete sentence.")
    state["streams"][0].pump(RATE // 2, latency=0.1)
    assert player.delivery()[0]["heard_samples"] == 0
    clock[0] += 0.35
    heard = player.delivery()[0]["heard_samples"]
    assert heard == pytest.approx(RATE // 4, abs=1)
    player.abort()
    clock[0] += 10
    assert player.delivery()[0]["heard_samples"] == heard
    assert state["aborted"] == 1


def test_underrun_silence_does_not_count_as_delivered_text(monkeypatch):
    state = _install_fake_sounddevice(monkeypatch)
    player = AudioPlayer()
    player.play(np.ones(10), RATE, text="Hello.")
    output = state["streams"][0].pump(100)
    assert np.all(output[:10] == 1)
    assert np.all(output[10:] == 0)
    assert player.delivery()[0]["total_samples"] == 10
