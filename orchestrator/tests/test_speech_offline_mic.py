"""Tests for the offline microphone path: capture, then decode in one pass.

An offline recogniser (SenseVoice) cannot decode incrementally, so the recorder
captures the whole utterance first — energy-endpointed — and it is decoded once.
These tests drive that with a fake recorder and a fake recogniser.
"""

from __future__ import annotations

import numpy as np

from orchestrator.hosts.speech import ContinuousRecorder, SttHost

RATE = 16000
BLOCK = 1600  # 100 ms


class _FakeRecorder(ContinuousRecorder):
    """A real recorder whose ``next_block`` is driven from a script.

    ``next_block`` also runs the same speech-detection the audio callback does,
    so barge-in behaves as it would on a live device.
    """

    def __init__(self, blocks):
        super().__init__(sample_rate=RATE, block_ms=100, threshold=0.01)
        self._script = list(blocks)

    def next_block(self, timeout=None):
        if not self._script:
            return None
        block = self._script.pop(0)
        level = float(np.sqrt(np.mean(block**2))) if len(block) else 0.0
        if level >= self.threshold:
            self._loud_run += 1
            if self._loud_run >= self.start_blocks:
                self._detected = True
        else:
            self._loud_run = 0
        return block


def _silence():
    return np.zeros(BLOCK, dtype=np.float32)


def _speech(level=0.2):
    return np.full(BLOCK, level, dtype=np.float32)


def _scripted(recorder, blocks):
    """Drive ``next_block`` from a script, then return None forever."""
    queue = list(blocks)

    def next_block(timeout=None):
        return queue.pop(0) if queue else None

    recorder.next_block = next_block  # type: ignore[method-assign]
    return recorder


def test_record_utterance_waits_for_speech_then_stops_on_silence():
    # silence, speech, speech, then enough silence to end the utterance.
    blocks = [_silence(), _speech(), _speech()] + [_silence()] * 12
    recorder = _scripted(
        ContinuousRecorder(sample_rate=RATE, block_ms=100, threshold=0.01), blocks
    )

    captured = recorder.record_utterance(silence_s=1.0, no_speech_s=8.0)

    assert captured is not None
    # It kept the leading silence (the run-up) and the speech.
    assert len(captured) >= 3 * BLOCK


def test_record_utterance_returns_none_when_nothing_is_said():
    recorder = _scripted(
        ContinuousRecorder(sample_rate=RATE, block_ms=100, threshold=0.01),
        [_silence()] * 100,
    )
    assert recorder.record_utterance(no_speech_s=0.3) is None


def test_listen_uses_offline_decode_for_a_non_streaming_model():
    host = SttHost()
    host._streaming = False
    host._recognizer = object()  # only checked for None
    decoded: list = []

    def fake_offline(samples, rate):
        decoded.append((len(samples), rate))
        return "hello there"

    host._transcribe_offline = fake_offline  # type: ignore[method-assign]
    recorder = _FakeRecorder([_silence(), _speech(), _speech()] + [_silence()] * 12)

    text = host.listen(recorder)

    assert text == "hello there"
    assert decoded and decoded[0][1] == RATE


def test_listen_returns_empty_when_nothing_is_said():
    host = SttHost()
    host._streaming = False
    host._recognizer = object()
    host._transcribe_offline = lambda samples, rate: "should not run"  # type: ignore[method-assign]
    recorder = _FakeRecorder([_silence()] * 100)

    assert host.listen(recorder) == ""


def test_sensevoice_keeps_utterance_boundary_context():
    from types import SimpleNamespace

    waveform = np.concatenate([_silence()] * 3 + [_speech()] * 2 + [_silence()] * 10)
    accepted = []
    stream = SimpleNamespace(
        accept_waveform=lambda rate, audio: accepted.append(audio.copy()),
        result=SimpleNamespace(text="Quit."),
    )
    host = SttHost()
    host._recognizer = SimpleNamespace(
        create_stream=lambda: stream, decode_stream=lambda stream: None
    )
    host._normalise_level = False  # SenseVoice
    observed = []
    host.on_decode_audio = lambda audio, rate: observed.append((audio.copy(), rate))
    assert host._transcribe_offline(waveform, RATE) == "Quit."
    assert len(accepted[0]) == len(waveform)
    assert np.all(accepted[0][:3 * BLOCK] == 0)
    np.testing.assert_array_equal(observed[0][0], accepted[0])
    assert observed[0][1] == RATE


def test_whisper_still_trims_and_adds_onset_padding():
    from types import SimpleNamespace

    waveform = np.concatenate([_silence()] * 10 + [_speech()] * 2 + [_silence()] * 20)
    accepted = []
    stream = SimpleNamespace(
        accept_waveform=lambda rate, audio: accepted.append(audio.copy()),
        result=SimpleNamespace(text="hello"),
    )
    host = SttHost()
    host._recognizer = SimpleNamespace(
        create_stream=lambda: stream, decode_stream=lambda stream: None
    )
    host._normalise_level = True  # Whisper
    assert host._transcribe_offline(waveform, RATE) == "hello"
    assert len(accepted[0]) < len(waveform)
    assert np.all(accepted[0][:int(0.2 * RATE)] == 0)


def test_barge_in_then_offline_transcription_end_to_end():
    """The exact live combination: barge-in fires, then the offline model decodes.

    Barge-in is energy-based and instant; the offline model then waits for the
    player to pause and decodes the whole utterance. This drives both against
    one recorder, the way ``main.py`` does.
    """
    # The player speaks over the AI: two loud blocks latch barge-in, then they
    # keep talking and finally pause, which is what ends the utterance.
    recorder = _FakeRecorder([_speech()] * 4 + [_silence()] * 12)

    # Barge-in fires on the first two blocks, before any transcription.
    assert recorder.next_block() is not None
    assert recorder.next_block() is not None
    assert recorder.speech_detected()

    host = SttHost()
    host._streaming = False
    host._recognizer = object()
    host._transcribe_offline = lambda samples, rate: "my name is max"  # type: ignore[method-assign]

    # The offline model then captures the rest and decodes the whole utterance.
    text = host.listen(recorder)

    assert text == "my name is max"


def test_barge_in_does_not_fire_on_silence():
    recorder = _FakeRecorder([_silence()] * 20)
    for _ in range(20):
        recorder.next_block()

    assert not recorder.speech_detected()


def test_ten_seconds_stale_silence_does_not_drop_queued_speech():
    onset = [_speech(0.02), _speech(0.03), _speech(0.04), _speech(0.05)]
    blocks = [_silence()] * 100 + onset + [_silence()] * 10
    recorder = _scripted(ContinuousRecorder(sample_rate=RATE, block_ms=100), blocks)

    captured = recorder.record_utterance(no_speech_s=0.3, max_s=2.0)

    np.testing.assert_array_equal(captured, np.concatenate([_silence()] * 3 + onset
                                                         + [_silence()] * 10))


def test_single_noise_spike_is_not_an_utterance():
    blocks = [_silence()] * 10 + [_speech()] + [_silence()] * 12
    recorder = _scripted(ContinuousRecorder(sample_rate=RATE, block_ms=100), blocks)

    assert recorder.record_utterance(no_speech_s=0.3) is None


def test_stale_scan_time_is_not_charged_to_wait_budget(monkeypatch):
    import time

    clock = [0.0]
    monkeypatch.setattr(time, "monotonic", lambda: clock[0])
    recorder = ContinuousRecorder(sample_rate=RATE, block_ms=100)
    stale = [_silence()] * 100
    live = [_speech()] * 2 + [_silence()] * 2
    waits = []

    def next_block(timeout=None):
        if stale:
            clock[0] += 0.1  # Deliberately expensive backlog processing.
            return stale.pop(0)
        if timeout == 0:
            return None
        waits.append(timeout)
        clock[0] += 0.1
        return live.pop(0) if live else None

    monkeypatch.setattr(recorder, "next_block", next_block)
    captured = recorder.record_utterance(no_speech_s=0.3, silence_s=0.2)

    assert captured is not None
    assert abs(waits[0] - 0.3) < 1e-9
    assert abs(waits[1] - 0.2) < 1e-9


def test_confirmation_retains_all_candidate_blocks_and_rejects_spikes():
    onset = [_speech(0.02), _speech(0.03), _speech(0.04)]
    blocks = [_speech()] + [_silence()] * 5 + onset + [_silence()] * 2
    recorder = _scripted(
        ContinuousRecorder(sample_rate=RATE, block_ms=100, start_blocks=3), blocks
    )

    captured = recorder.record_utterance(silence_s=0.2)

    np.testing.assert_array_equal(captured, np.concatenate([_silence()] * 3 + onset
                                                         + [_silence()] * 2))


def test_quiet_endpoint_uses_sample_duration_not_block_count():
    half_quiet = np.zeros(BLOCK // 2, dtype=np.float32)
    blocks = [_speech()] * 2 + [half_quiet] * 4 + [_speech()] * 2
    recorder = _scripted(ContinuousRecorder(sample_rate=RATE, block_ms=100), blocks)

    captured = recorder.record_utterance(silence_s=0.2)

    np.testing.assert_array_equal(captured, np.concatenate(blocks[:6]))
    np.testing.assert_array_equal(recorder.next_block(), _speech())


def test_max_duration_starts_at_onset_not_stale_silence():
    blocks = [_silence()] * 100 + [_speech()] * 8
    recorder = _scripted(ContinuousRecorder(sample_rate=RATE, block_ms=100), blocks)

    captured = recorder.record_utterance(max_s=0.3, no_speech_s=0.01)

    np.testing.assert_array_equal(captured, np.concatenate([_silence()] * 3 + [_speech()] * 3))
    np.testing.assert_array_equal(recorder.next_block(), _speech())


def test_offline_listen_forwards_each_consumed_block_before_decode():
    host = SttHost()
    host._streaming = False
    host._recognizer = object()
    blocks = [_silence(), _speech(), _speech()] + [_silence()] * 10
    recorder = _scripted(ContinuousRecorder(sample_rate=RATE, block_ms=100), blocks)
    seen = []

    def decode(samples, rate):
        assert len(seen) == len(blocks)
        return "hello"

    host._transcribe_offline = decode  # type: ignore[method-assign]
    assert host.listen(recorder, on_block=seen.append) == "hello"
    assert all(actual is expected for actual, expected in zip(seen, blocks, strict=True))


def test_streaming_listen_forwards_blocks_before_recognizer_consumes_them():
    host = SttHost()
    host._streaming = True
    host._recognizer = object()
    blocks = [_silence(), _speech(), _speech()]
    recorder = _scripted(ContinuousRecorder(sample_rate=RATE, block_ms=100), blocks)
    seen = []
    partial = object()

    def listen_stream(source, *, rate, on_partial):
        assert rate == RATE
        assert on_partial is partial
        for index, block in enumerate(source):
            assert seen[index] is block
        return "hello"

    host.listen_stream = listen_stream  # type: ignore[method-assign]
    assert host.listen(recorder, on_partial=partial, on_block=seen.append) == "hello"
    assert len(seen) == len(blocks)
