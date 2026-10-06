"""The LLM -> TTS bridge: incremental sentence splitting."""

from __future__ import annotations

from orchestrator.streaming import SentenceSplitter, stream_sentences


def test_emits_sentence_as_soon_as_boundary_arrives():
    splitter = SentenceSplitter(min_chars=8)
    assert splitter.feed("Hello there") == []
    assert splitter.feed(" friend.") == ["Hello there friend."]


def test_does_not_cut_on_short_abbreviation():
    splitter = SentenceSplitter(min_chars=8)
    assert splitter.feed("Mr. Smith") == []
    assert splitter.feed(" arrived today.") == ["Mr. Smith arrived today."]


def test_flush_returns_trailing_text():
    splitter = SentenceSplitter(min_chars=8)
    splitter.feed("A complete sentence. And a trailing fragment")
    assert splitter.flush() == ["And a trailing fragment"]


def test_multiple_sentences_in_one_chunk():
    splitter = SentenceSplitter(min_chars=4)
    out = splitter.feed("One two. Three four. Five six.")
    assert out == ["One two.", "Three four.", "Five six."]


def test_stream_sentences_over_chunks():
    chunks = ["The forge ", "is cold. ", "Nobody ", "has come."]
    assert list(stream_sentences(chunks, min_chars=4)) == [
        "The forge is cold.",
        "Nobody has come.",
    ]


def test_empty_input_yields_nothing():
    assert list(stream_sentences([], min_chars=4)) == []


def test_min_chars_must_be_positive():
    import pytest

    with pytest.raises(ValueError):
        SentenceSplitter(min_chars=0)
