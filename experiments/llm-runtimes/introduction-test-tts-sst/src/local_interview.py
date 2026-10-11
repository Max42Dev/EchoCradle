"""Explicit application boundary for in-process diagnostic experiments.

ModelOrchestrator keeps its native tuple/predicate API. Service consumers never
use this adapter or detect a legacy API dynamically.
"""

import threading
from typing import Callable
from uuid import uuid4

from orchestrator import ModelOrchestrator
from orchestrator.client_types import Message, TextResult, ToolOutcome, ToolRegistry


class LocalInterviewText:
    def __init__(self, orchestrator: ModelOrchestrator) -> None:
        self.orchestrator = orchestrator

    @staticmethod
    def _result(text: str, calls: list, cancelled: bool = False) -> TextResult:
        return TextResult(
            "cancelled" if cancelled else "succeeded", text, str(uuid4()), None,
            tuple(ToolOutcome(
                call.id or None, None, call.name, call.arguments, call.result,
                "unknown" if cancelled else "completed", not cancelled, True,
            ) for call in calls),
        )

    def chat_with_tools(
        self, messages: list[Message], registry: ToolRegistry, *,
        max_tokens: int = 512, temperature: float = 0.7,
    ) -> TextResult:
        text, calls = self.orchestrator.chat_with_tools(
            messages, registry, max_tokens=max_tokens, temperature=temperature,
        )
        return self._result(text, calls)

    def stream_chat_with_tools(
        self, messages: list[Message], registry: ToolRegistry, *,
        max_tokens: int = 512, temperature: float = 0.7,
        on_text: Callable[[str], None] | None = None,
        cancellation: threading.Event | None = None,
    ) -> TextResult:
        text, calls = self.orchestrator.stream_chat_with_tools(
            messages, registry, max_tokens=max_tokens, temperature=temperature,
            on_text=on_text, should_stop=cancellation.is_set if cancellation else None,
        )
        return self._result(text, calls, cancellation is not None and cancellation.is_set())