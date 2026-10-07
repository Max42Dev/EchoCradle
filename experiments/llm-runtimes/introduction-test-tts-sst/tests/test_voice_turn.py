"""Concurrency/control regressions without microphone, speakers, or live models."""

from __future__ import annotations

import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import main as app
from interview import TurnResult


class Interview:
    def __init__(self) -> None:
        self.history: list[dict[str, str]] = []
        self.turns = 0

    def opening_streaming(self, emit, stop):
        self.turns += 1
        emit("First sentence here.")
        emit("Second unheard sentence.")
        self.history.append({"role": "assistant", "content": "Generated full reply."})
        return TurnResult(say="Generated full reply.", config={}, done=False), stop()


class Player:
    def __init__(self) -> None:
        self.aborted = threading.Event()
        self.played = threading.Event()
        self.error = None

    def begin_turn(self):
        pass

    @property
    def playing(self):
        return self.played.is_set() and not self.aborted.is_set()

    def play(self, samples, rate, *, text=""):
        self.played.set()
        return True

    def abort(self):
        self.aborted.set()

    def delivery(self):
        if not self.played.is_set():
            return []
        return [{"text": "First sentence here.", "total_samples": 100,
                 "heard_samples": 50}]


def test_interrupt_during_native_tts_returns_without_waiting_for_tts(tmp_path):
    entered = threading.Event()
    release = threading.Event()

    def synthesize(text):
        entered.set()
        assert release.wait(3)
        return np.zeros(100), 16000

    player = Player()
    interview = Interview()
    recorder = SimpleNamespace(speech_detected=entered.is_set, available=True)
    mo = SimpleNamespace(tts=SimpleNamespace(synthesize_samples=synthesize))
    with ThreadPoolExecutor(max_workers=1) as executor:
        args = SimpleNamespace(tts=True, play=True, tts_executor=executor)
        before = time.monotonic()
        try:
            turn, interrupted, _ = app._run_turn(
                interview, mo, player, recorder, tmp_path, 0, args, opening=True
            )
            assert time.monotonic() - before < 0.5
            assert interrupted and player.aborted.is_set()
            assert not player.played.is_set()
            assert turn.say == ""
            assert not interview.history
            assert "before intelligible reply" in interview.interruption_context
        finally:
            release.set()
    assert not player.played.is_set()  # late native result cannot restart output


def test_late_interrupt_rewrites_history_to_playback_estimate(tmp_path):
    player = Player()
    interview = Interview()
    recorder = SimpleNamespace(speech_detected=player.played.is_set, available=True)
    mo = SimpleNamespace(tts=SimpleNamespace(
        synthesize_samples=lambda text: (np.zeros(100), 16000)
    ))
    with ThreadPoolExecutor(max_workers=1) as executor:
        args = SimpleNamespace(tts=True, play=True, tts_executor=executor)
        turn, interrupted, _ = app._run_turn(
            interview, mo, player, recorder, tmp_path, 0, args, opening=True
        )
    assert interrupted
    assert turn.say == "First…"
    assert interview.history[-1]["content"] == "First…"
    assert "estimated" in interview.interruption_context
    assert "unheard" not in interview.history[-1]["content"]
    assert (tmp_path / "delivery.jsonl").is_file()


def test_sentence_callback_never_waits_for_slow_tts(tmp_path):
    entered = threading.Event()
    release = threading.Event()
    drained = threading.Event()

    class BurstInterview(Interview):
        def opening_streaming(self, emit, stop):
            self.turns += 1
            emit("First sentence here.")
            assert entered.wait(1)
            for index in range(100):
                emit(f"Sentence {index}.")
            drained.set()
            self.history.append({"role": "assistant", "content": "Generated full reply."})
            return TurnResult(say="Generated full reply.", config={}, done=False), stop()

    def synthesize(text):
        entered.set()
        assert release.wait(3)
        return np.zeros(100), 16000

    player = Player()
    recorder = SimpleNamespace(speech_detected=drained.is_set, available=True)
    mo = SimpleNamespace(tts=SimpleNamespace(synthesize_samples=synthesize))
    with ThreadPoolExecutor(max_workers=1) as executor:
        args = SimpleNamespace(tts=True, play=False, tts_executor=executor)
        try:
            _, interrupted, next_line = app._run_turn(
                BurstInterview(), mo, player, recorder, tmp_path, 0, args, opening=True,
            )
            assert drained.is_set() and interrupted
            assert next_line == 101
        finally:
            release.set()


@pytest.mark.parametrize("text", ["Quit.", "quit!", " EXIT ", "Quit"])
def test_recognizer_punctuation_does_not_prevent_stop(text):
    assert app._is_stop_command(text)


@pytest.mark.parametrize("text", ["don't quit", "exit the room", "", "quitter"])
def test_stop_requires_an_explicit_command(text):
    assert not app._is_stop_command(text)


def test_interview_defaults_delegate_models_and_voice_to_service():
    args = app.build_parser().parse_args(["--mic"])
    assert (args.text_model, args.tts_model, args.stt_model, args.speaker_id) == (
        None, None, None, None
    )
    assert args.service_url is None
    attached = app.build_parser().parse_args(["--service-url", "http://127.0.0.1:5010"])
    assert attached.service_url == "http://127.0.0.1:5010"