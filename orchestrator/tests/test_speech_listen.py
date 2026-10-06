"""Tests for :meth:`SttHost.listen_stream`, the live-microphone recogniser loop.

Driven with a fake recogniser so no model or hardware is needed. The behaviour
under test is the silence guard: the recogniser endpoints on silence as well as
on speech, so an endpoint can fire before the player has said anything. Ending
the utterance then would return an empty string and the caller would move on.
"""

from __future__ import annotations

import numpy as np

from orchestrator.hosts.speech import SttHost

RATE = 16000
BLOCK = 1600  # 100 ms


class _FakeStream:
    def __init__(self, recognizer):
        self._recognizer = recognizer
        self.blocks = 0

    def accept_waveform(self, rate, samples):
        self.blocks += 1

    def input_finished(self):
        pass


class _FakeRecognizer:
    """Endpoints after every block; only produces text once speech is seen."""

    def __init__(self, *, speech_after: int):
        self._speech_after = speech_after
        self._stream = None

    def create_stream(self):
        self._stream = _FakeStream(self)
        return self._stream

    def is_ready(self, stream):
        return False

    def decode_stream(self, stream):
        pass

    def get_result(self, stream):
        return "hello" if stream.blocks > self._speech_after else ""

    def reset(self, stream):
        pass

    def is_endpoint(self, stream):
        # Endpoints on the very first block, before any speech: the case that
        # used to end the utterance with an empty transcript.
        return True


def _host(recognizer) -> SttHost:
    host = SttHost()
    host._recognizer = recognizer
    host._streaming = True
    return host


def test_endpoint_on_silence_does_not_end_the_utterance():
    # The first two blocks endpoint with no text; the third has text. The loop
    # must ignore the empty endpoints and keep consuming until there is text.
    recognizer = _FakeRecognizer(speech_after=2)
    host = _host(recognizer)

    blocks = (np.zeros(BLOCK, dtype=np.float32) for _ in range(5))
    text = host.listen_stream(blocks, rate=RATE)

    assert text == "hello"
    # It consumed past the empty endpoints rather than stopping at the first.
    assert recognizer._stream.blocks >= 3


def test_returns_empty_when_nothing_is_ever_said():
    recognizer = _FakeRecognizer(speech_after=10_000)
    host = _host(recognizer)

    blocks = (np.zeros(BLOCK, dtype=np.float32) for _ in range(3))
    text = host.listen_stream(blocks, rate=RATE)

    assert text == ""
