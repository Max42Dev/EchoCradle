"""A stateful conversation over a stateless text host.

:class:`~orchestrator.orchestrator.ModelOrchestrator` is deliberately stateless:
every ``chat`` call is independent, and the caller must supply the whole message
list. That is the right primitive, but it means every caller re-implements the
same bookkeeping -- keep a system prompt, append the user's turn, append the
assistant's reply, replay it all next time. This class owns that bookkeeping so
callers do not have to.

A conversation is:

* a **system prompt** (fixed for its lifetime),
* an alternating list of **user** and **assistant** messages,
* optional **transient notes** -- system messages injected for the *next* call
  only (state reminders, validation errors) and then discarded, so they never
  accumulate in the history.

Typical use::

    convo = Conversation(mo, "You are a helpful assistant.")
    reply = convo.say("Hello!")                 # user turn -> assistant reply
    reply, calls = convo.say_with_tools("Hi", registry)
"""

from __future__ import annotations

from typing import Any

from orchestrator.tools import ToolRegistry


class Conversation:
    """A message history plus the calls that advance it."""

    def __init__(
        self,
        orchestrator: Any,
        system_prompt: str,
        *,
        max_tokens: int = 512,
        temperature: float = 0.7,
    ) -> None:
        self.orchestrator = orchestrator
        self.system_prompt = system_prompt
        self.max_tokens = max_tokens
        self.temperature = temperature
        self.messages: list[dict[str, str]] = [
            {"role": "system", "content": system_prompt}
        ]
        self._notes: list[str] = []

    # -- history -----------------------------------------------------------

    @property
    def history(self) -> list[dict[str, str]]:
        """The persistent messages, excluding the system prompt and notes."""
        return self.messages[1:]

    def note(self, text: str) -> None:
        """Queue a system note for the next call only (not kept in history)."""
        self._notes.append(text)

    def add_user(self, text: str) -> None:
        self.messages.append({"role": "user", "content": text})

    def add_assistant(self, text: str) -> None:
        self.messages.append({"role": "assistant", "content": text})

    def _outgoing(self) -> list[dict[str, str]]:
        """The messages to send: history plus any queued notes."""
        outgoing = list(self.messages)
        outgoing.extend({"role": "system", "content": n} for n in self._notes)
        return outgoing

    def _clear_notes(self) -> None:
        self._notes.clear()

    # -- turns -------------------------------------------------------------

    def say(self, text: str, **kwargs: Any) -> str:
        """Send a user turn, record the reply, and return it."""
        self.add_user(text)
        reply = self.orchestrator.chat(
            self._outgoing(),
            max_tokens=kwargs.get("max_tokens", self.max_tokens),
            temperature=kwargs.get("temperature", self.temperature),
        )
        self._clear_notes()
        self.add_assistant(reply)
        return reply

    def say_with_tools(
        self, text: str, registry: ToolRegistry, **kwargs: Any
    ) -> tuple[str, list[Any]]:
        """Send a user turn with tools, record the reply, and return it."""
        self.add_user(text)
        reply, calls = self.orchestrator.chat_with_tools(
            self._outgoing(),
            registry,
            max_tokens=kwargs.get("max_tokens", self.max_tokens),
            temperature=kwargs.get("temperature", self.temperature),
        )
        self._clear_notes()
        self.add_assistant(reply)
        return reply, calls

    def opening(self, prompt: str, **kwargs: Any) -> str:
        """Produce the first assistant turn without a user message.

        ``prompt`` is a stage direction (e.g. "The player has just arrived")
        sent as the user turn so the model has something to answer.
        """
        return self.say(prompt, **kwargs)
