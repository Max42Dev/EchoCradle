"""Incremental sentence splitter: the LLM -> TTS bridge.

The orchestrator receives an LLM response as a *token stream*. To start speaking
before the response is finished, we must cut that stream into speakable units as
soon as a boundary appears -- not after the whole reply arrives.

This module is pure logic with no dependencies, so it is fully testable and is
the piece that actually delivers "speech starts while the text is still being
generated".

Design notes
------------
* Boundaries are sentence terminators (``. ! ? ...``) plus newlines.
* A boundary only fires once at least ``min_chars`` characters have accumulated,
  so abbreviations and short fragments ("Mr.", "3.14") do not cause a cut.
* ``flush()`` emits whatever is left when the stream ends.
* The splitter is a state machine fed one chunk at a time, so it works with any
  tokenizer granularity (whole tokens, sub-words, or characters).

Run the self-test:

    python sentence_splitter.py --demo
"""

from __future__ import annotations

import argparse
import re
from collections.abc import Iterable, Iterator

# Sentence terminators. A run of them (e.g. "?!", "...") counts as one boundary.
_TERMINATORS = ".!?\n"
# Characters that may legitimately follow a terminator without ending a sentence.
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
        """Index just past a sentence boundary, or None if there is none yet."""
        text = self._buffer
        for i, ch in enumerate(text):
            if ch not in _TERMINATORS:
                continue
            # Consume a run of terminators and any trailing closing quotes.
            end = i + 1
            while end < len(text) and text[end] in _TERMINATORS:
                end += 1
            while end < len(text) and text[end] in _CLOSERS:
                end += 1
            # Do not cut mid-stream on a boundary that might be an abbreviation:
            # require enough text, and (unless final) a following space/newline.
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


def _demo() -> None:
    """Simulate an LLM token stream and show when each sentence is emitted."""
    reply = (
        "The blacksmith eyes you warily. He has not slept in three days, "
        "and the forge fire behind him is dying. \"What do you want?\" he asks. "
        "His hand rests on a hammer that is not for show."
    )
    # Crude tokenisation: split on spaces, keep the spaces, to mimic a tokenizer.
    tokens = re.findall(r"\S+\s*", reply)

    print("Simulated LLM token stream -> sentence emissions")
    print("-" * 60)
    splitter = SentenceSplitter(min_chars=8)
    emitted = 0
    for n, token in enumerate(tokens, start=1):
        for sentence in splitter.feed(token):
            emitted += 1
            print(f"  token {n:>3}  ->  SPEAK: {sentence!r}")
    for sentence in splitter.flush():
        emitted += 1
        print(f"  end      ->  SPEAK: {sentence!r}")
    print("-" * 60)
    print(f"{emitted} sentences emitted from {len(tokens)} tokens")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--demo", action="store_true", help="run the self-test")
    parser.add_argument("--min-chars", type=int, default=8,
                        help="minimum characters before a boundary may fire")
    args = parser.parse_args()

    if args.demo:
        _demo()
    else:
        parser.print_help()


if __name__ == "__main__":
    main()
