"""The interview: a relaxed conversation that records values on the side.

One model call per turn. The model talks *and* calls the config tools as it goes,
which is the natural design and the one that works (5/5 complete configs on
Granite 4.2-8B — see `compare_toolcalling.py`).

The config document is owned by a real
:class:`~orchestrator.tools.JsonConfigTool`, and the tool calls the model makes
are executed through the same :class:`~orchestrator.tools.ToolRegistry` the
orchestrator exposes.

The class owns the *contract* around the model:

* a malformed reply is retried once, then replaced with a scripted line so the
  conversation can never hard-fail,
* the document is only written to disk once it validates.

Character behaviour -- how snobbish it is about the name it is given, whether it
objects at all -- lives in the prompt, not here. The model sets the config
however it likes; code only checks the result against the schema.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from typing import Any

from config_schema import is_complete, missing_fields, normalize
from prompts import SPOKEN_RETRY_PROMPT, build_turn_messages

log = logging.getLogger("interview")

#: Only identified stage directions / internal asides are removed. Ordinary
#: parenthetical speech and bracketed names must remain intact.
_ASIDE = re.compile(r"\([^()]*\)|\[[^\[\]]*\]", re.DOTALL)
_STAGE_DIRECTION = re.compile(
    r"^(?:\*?(?:sighs?|smiles?|laughs?|chuckles?|pauses?|nods?|whispers?|"
    r"clears? (?:my|their|his|her|the) throat|stage direction)\b|"
    r"(?:with|in) (?:a |an )?(?:dramatic|theatrical|sly|soft|hushed)\b)",
    re.IGNORECASE,
)

#: Phrases that mean the model is talking about its own machinery rather than to
#: the player. A reply containing one is discarded and re-asked, because it must
#: never be spoken aloud.
_META_MARKERS = (
    "config_set",
    "tool call",
    "tool_calls",
    "the tools",
    "my instructions",
    "per instructions",
    "the config file",
    "the configuration file",
    "json object",
    "{\"say\"",
    "internal note",
    "private context",
    "system prompt",
    "system message",
    "developer message",
    "conversation history",
    "chat history",
    "history marker",
    "player interrupted",
    "partial-sentence wording",
    "wording is approximate",
    "recorded=",
    "still_needed=",
    "ask_them_next=",
    "config tool",
    "config schema",
    "configuration schema",
    "json schema",
    "schema validation",
    "validation errors",
    "does not yet match its schema",
    "i shall record",
    "i will record",
    "i'll record",
    "let me record",
    "record it faithfully",
    "no further objection",
    "formally recorded",
    "the required fields",
    "required fields",
    "validation call",
)
_META_PATTERNS = (
    re.compile(
        r"\b(?:i|we)(?: (?:must|need to|should|will|shall)|['’]ll) "
        r"(?:set|update|replace|save|store|record) "
        r"(?:the |their |your |a )?(?:username|ai_name|style field|story field|"
        r"config(?:uration)?|field|value)\b",
        re.IGNORECASE,
    ),
    re.compile(
        r"\bif (?:they|the player|the user)\b.*\b(?:set|record|store|call)\b",
        re.IGNORECASE | re.DOTALL,
    ),
    re.compile(r"\b(?:according to|per|follow|following) (?:my |the )?instructions\b", re.I),
    re.compile(r"\b(?:the|my|our) schema\b|\b(?:username|ai_name)\s*=", re.I),
)

#: Words that mean the player declined to give a value. Recording "no story"
#: as the story would be wrong, so a declined optional field is left unset.
_NEGATIONS = frozenset(
    {
        "no",
        "nope",
        "none",
        "nothing",
        "no story",
        "no thanks",
        "no thank you",
        "skip",
        "skip it",
        "pass",
        "not now",
        "no idea",
        "n/a",
        "na",
        "-",
    }
)

#: Used when the model cannot produce a usable reply.
_FALLBACK_LINES = {
    "username": "Let's start over simply: what should I call you?",
    "style": "And what kind of world is this? Give me a word or two.",
    "ai_name": "Then tell me what to call myself.",
    "story": "Anything else worth knowing? Say no and we are done.",
}


@dataclass
class TurnResult:
    """One exchange's outcome."""

    say: str
    config: dict[str, Any]
    done: bool
    raw: str = ""
    repaired: bool = False
    tool_calls: list[Any] = field(default_factory=list)
    validation_errors: list[str] = field(default_factory=list)


@dataclass
class Interview:
    """Drives the conversation until the config is complete."""

    orchestrator: Any
    config_tool: Any
    registry: Any
    max_turns: int = 12
    history: list[dict[str, str]] = field(default_factory=list)
    turns: int = 0
    #: Schema problems found after the previous turn, reported to the model.
    validation_errors: list[str] = field(default_factory=list)
    #: The field the interview is stuck on, and for how many turns.
    _stall_field: str | None = None
    _stall_count: int = 0
    #: Delivery bookkeeping is private context, never assistant dialogue.
    interruption_context: str | None = None

    # -- public API --------------------------------------------------------

    def opening(self) -> TurnResult:
        """Produce the AI's first line without any user input yet."""
        return self._turn()

    def opening_streaming(
        self,
        on_sentence: Any,
        should_stop: Any = None,
    ) -> tuple[TurnResult, bool]:
        """Produce the AI's first line, speaking it as it is generated."""
        return self._turn_streaming(on_sentence, should_stop)

    def respond(self, user_text: str) -> TurnResult:
        """Handle one user utterance and return the AI's reply."""
        self.history.append({"role": "user", "content": user_text})
        return self._turn()

    def respond_streaming(
        self,
        user_text: str,
        on_sentence: Any,
        should_stop: Any = None,
    ) -> tuple[TurnResult, bool]:
        """Handle one user utterance, speaking the reply as it is generated.

        ``on_sentence(sentence)`` is called for each complete sentence the
        moment the model writes it, so speech starts long before the reply is
        finished. ``should_stop()`` is polled between deltas; when it returns
        True the model is cut off and only the sentences actually spoken are
        kept in the history — so the model's next turn sees exactly what the
        player heard, not what it had intended to say.

        Returns ``(result, interrupted)``.
        """
        self.history.append({"role": "user", "content": user_text})
        return self._turn_streaming(on_sentence, should_stop)

    @property
    def slots(self) -> dict[str, Any]:
        """The config document as it currently stands."""
        return dict(self.config_tool.data)

    @property
    def complete(self) -> bool:
        return is_complete(self.config_tool.data)

    def config(self) -> dict[str, Any]:
        """The finished config, normalised and ready to write."""
        return normalize(self.config_tool.data)

    # -- internals ---------------------------------------------------------

    def _prepare_messages(self) -> list[dict[str, str]]:
        """Track a stall and assemble the message list for one turn."""
        missing_now = self.config_tool.missing()
        if missing_now and missing_now[0] == self._stall_field:
            self._stall_count += 1
        else:
            self._stall_field = missing_now[0] if missing_now else None
            self._stall_count = 0
        return build_turn_messages(
            self.history,
            self.config_tool.data,
            interruption_context=self.interruption_context,
            validation_errors=self.validation_errors,
            stalled_field=self._stall_field if self._stall_count >= 3 else None,
        )

    def _turn(self) -> TurnResult:
        self.turns += 1
        messages = self._prepare_messages()
        say, calls, repaired = self._ask(messages)

        # A declined optional field must stay unset, not be stored as the literal
        # word the player used to decline it ("no story" is not a story).
        self._drop_declined_values()

        if not say:
            say = self._fallback_line()
            repaired = True

        self.history.append({"role": "assistant", "content": say})

        # Check the config against its schema and remember the problems, so the
        # next turn can report them to the model and let it correct itself.
        self.validation_errors = self.config_tool.errors()

        return TurnResult(
            say=say,
            config=dict(self.config_tool.data),
            done=self.complete,
            repaired=repaired,
            tool_calls=calls,
            validation_errors=list(self.validation_errors),
        )

    def _turn_streaming(
        self,
        on_sentence: Any,
        should_stop: Any,
    ) -> tuple[TurnResult, bool]:
        """One turn whose reply is spoken sentence by sentence as it arrives."""
        from orchestrator.streaming import SentenceSplitter  # noqa: PLC0415

        self.turns += 1
        messages = self._prepare_messages()
        splitter = SentenceSplitter()
        spoken: list[str] = []
        aside: list[str] = []
        closers: list[str] = []

        def emit(sentence: str) -> None:
            if should_stop is not None and should_stop():
                return
            # A sentence that leaks the machinery is never spoken, and never
            # enters the history — the player did not hear it.
            sentence = self._spoken_line(sentence)
            if not sentence:
                return
            spoken.append(sentence)
            on_sentence(sentence)

        def on_text(chunk: str) -> None:
            # Buffer asides across deltas AND sentence boundaries: otherwise a
            # period inside an internal aside can leak its trailing sentences.
            clean: list[str] = []
            for char in chunk:
                if char in "([":
                    closers.append(")" if char == "(" else "]")
                    aside.append(char)
                elif closers:
                    aside.append(char)
                    if char == closers[-1]:
                        closers.pop()
                        if not closers:
                            clean.append(self._strip_asides("".join(aside)))
                            aside.clear()
                else:
                    clean.append(char)
            for sentence in splitter.feed("".join(clean)):
                emit(sentence)

        try:
            _text, calls = self.orchestrator.stream_chat_with_tools(
                messages, self.registry, max_tokens=400, temperature=0.4,
                on_text=on_text,
                should_stop=should_stop,
            )
        except Exception as exc:  # noqa: BLE001 - surfaced as a fallback line
            log.warning("text host failed: %s", exc)
            calls = []

        interrupted = bool(should_stop()) if should_stop is not None else False
        if not interrupted:
            for sentence in splitter.flush():
                emit(sentence)

        self._drop_declined_values()

        say = " ".join(spoken).strip()
        repaired = False
        if not say and not interrupted:
            say = self._fallback_line()
            repaired = True
            on_sentence(say)

        # Only what was actually spoken goes into the history. If the player
        # interrupted, the model's next turn must see the truncated line, not
        # the full reply it had planned.
        self.history.append({"role": "assistant", "content": say})

        self.validation_errors = self.config_tool.errors()

        return (
            TurnResult(
                say=say,
                config=dict(self.config_tool.data),
                done=self.complete,
                repaired=repaired,
                tool_calls=calls,
                validation_errors=list(self.validation_errors),
            ),
            interrupted,
        )

    def _drop_declined_values(self) -> None:
        """Remove fields the player declined, so they are omitted rather than stored."""
        for name in list(self.config_tool.data):
            value = self.config_tool.data[name]
            if isinstance(value, str) and value.strip().lower() in _NEGATIONS:
                log.info("dropping declined value for %s: %r", name, value)
                self.config_tool.data.pop(name, None)

    def _ask(self, messages: list[dict[str, str]]) -> tuple[str, list[Any], bool]:
        """Native tools remain available on every attempt; the model chooses calls."""
        calls: list[Any] = []
        for attempt in range(2):
            try:
                text, requested = self.orchestrator.chat_with_tools(
                    messages, self.registry, max_tokens=400, temperature=0.4,
                )
                calls.extend(requested)
                say = self._spoken_line(text)
                if say:
                    return say, calls, bool(attempt)
                messages = messages + [{"role": "system", "content": SPOKEN_RETRY_PROMPT}]
            except Exception as exc:  # noqa: BLE001 - bounded retry, then fallback
                log.warning("text host failed: %s", exc)
        return "", calls, True

    @classmethod
    def _spoken_line(cls, raw: str) -> str:
        """Extract what the AI says, rejecting anything that leaks the machinery."""
        if not raw or not raw.strip():
            return ""
        text = raw.strip()

        # A JSON envelope is no longer requested, but tolerate one if it appears.
        parsed = cls._parse(text)
        if parsed is not None and isinstance(parsed.get("say"), str):
            text = parsed["say"].strip()

        text = re.sub(r"[ \t]{2,}", " ", cls._strip_asides(text)).strip()

        if not text or text.startswith(("{", '["', "[{")):
            return ""
        if cls._looks_like_meta(text):
            log.warning("discarding a reply that leaked the machinery: %r", text[:120])
            return ""
        return text

    @classmethod
    def _strip_asides(cls, text: str) -> str:
        """Remove recognized directions, not arbitrary conversational asides."""
        def replace(match: re.Match[str]) -> str:
            content = match.group()[1:-1].strip()
            if cls._looks_like_meta(content) or _STAGE_DIRECTION.match(content):
                return " "
            return match.group()

        return _ASIDE.sub(replace, text)

    @staticmethod
    def _looks_like_meta(text: str) -> bool:
        """True when the reply is about the model's own instructions or tools."""
        lowered = text.lower()
        return any(marker in lowered for marker in _META_MARKERS) or any(
            pattern.search(text) for pattern in _META_PATTERNS
        )

    @staticmethod
    def _parse(raw: str) -> dict[str, Any] | None:
        """Parse the model's reply, tolerating prose around the JSON."""
        if not raw:
            return None
        text = raw.strip()
        if text.startswith("```"):
            text = text.strip("`")
            text = text[text.find("{") :]
        try:
            data = json.loads(text)
        except json.JSONDecodeError:
            start, end = text.find("{"), text.rfind("}")
            if start == -1 or end <= start:
                return None
            try:
                data = json.loads(text[start : end + 1])
            except json.JSONDecodeError:
                return None
        return data if isinstance(data, dict) else None

    def _fallback_line(self) -> str:
        missing = missing_fields(self.config_tool.data)
        key = missing[0] if missing else "story"
        return _FALLBACK_LINES.get(key, "Say that again, plainly.")
