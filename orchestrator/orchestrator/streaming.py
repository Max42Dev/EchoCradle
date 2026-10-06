"""Incremental sentence splitting: the LLM -> TTS bridge.

Promoted from experiment 0109, where it was identified as the piece that makes
"speak while the LLM is still writing" work. It is **pure logic, not a model**:
feed it text chunks as they stream out of the text host and it returns complete
sentences the moment a boundary appears.

Boundaries are sentence terminators (``. ! ? ...``) plus newlines. A boundary
only fires once ``min_chars`` have accumulated, so abbreviations and decimals
("Mr.", "3.14") do not cause a premature cut.
"""

from __future__ import annotations

from collections.abc import Iterable, Iterator

_TERMINATORS = ".!?\n"
_CLOSERS = "\"')]}»”’"


class SentenceSplitter:
    """Feed text chunks in, get complete sentences out, incrementally."""

    def __init__(self, min_chars: int = 8) -> None:
        if min_chars < 1:
            raise ValueError("min_chars must be >= 1")
        self._min_chars = min_chars
        self._buffer = ""

    def feed(self, chunk: str) -> list[str]:
        """Add a chunk and return any sentences that are now complete."""
        self._buffer += chunk
        return self._drain(final=False)

    def flush(self) -> list[str]:
        """Return the remaining text as a final sentence (may be empty)."""
        return self._drain(final=True)

    def _drain(self, *, final: bool) -> list[str]:
        out: list[str] = []
        while True:
            cut = self._find_boundary(final=final)
            if cut is None:
                break
            sentence = self._buffer[:cut].strip()
            self._buffer = self._buffer[cut:]
            if sentence:
                out.append(sentence)
        return out

    def _find_boundary(self, *, final: bool) -> int | None:
        text = self._buffer
        for i, ch in enumerate(text):
            if ch not in _TERMINATORS:
                continue
            end = i + 1
            while end < len(text) and text[end] in _TERMINATORS:
                end += 1
            while end < len(text) and text[end] in _CLOSERS:
                end += 1
            if end < self._min_chars:
                continue
            if not final and end < len(text) and not text[end].isspace():
                continue
            return end
        if final and text.strip():
            return len(text)
        return None


def stream_sentences(chunks: Iterable[str], min_chars: int = 8) -> Iterator[str]:
    """Convenience generator over an iterable of chunks."""
    splitter = SentenceSplitter(min_chars=min_chars)
    for chunk in chunks:
        yield from splitter.feed(chunk)
    yield from splitter.flush()
