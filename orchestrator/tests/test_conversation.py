"""The Conversation wrapper: history bookkeeping over a stateless host."""

from __future__ import annotations

from orchestrator.conversation import Conversation
from orchestrator.tools import ToolRegistry


class FakeOrchestrator:
    """Records the messages it is given and returns a scripted reply."""

    def __init__(self, replies: list[str] | None = None) -> None:
        self.replies = list(replies or [])
        self.seen: list[list[dict[str, str]]] = []

    def chat(self, messages, **kwargs):
        self.seen.append(messages)
        return self.replies.pop(0) if self.replies else "ok"

    def chat_with_tools(self, messages, registry, **kwargs):
        self.seen.append(messages)
        return (self.replies.pop(0) if self.replies else "ok"), []


def test_starts_with_only_the_system_prompt():
    convo = Conversation(FakeOrchestrator(), "SYS")
    assert convo.messages == [{"role": "system", "content": "SYS"}]
    assert convo.history == []


def test_say_records_both_turns():
    convo = Conversation(FakeOrchestrator(["hi back"]), "SYS")
    reply = convo.say("hello")
    assert reply == "hi back"
    assert convo.history == [
        {"role": "user", "content": "hello"},
        {"role": "assistant", "content": "hi back"},
    ]


def test_history_is_replayed_on_the_next_call():
    fake = FakeOrchestrator(["one", "two"])
    convo = Conversation(fake, "SYS")
    convo.say("first")
    convo.say("second")
    # The second call must contain the first exchange.
    second = fake.seen[1]
    assert second[0] == {"role": "system", "content": "SYS"}
    assert {"role": "user", "content": "first"} in second
    assert {"role": "assistant", "content": "one"} in second
    assert second[-1] == {"role": "user", "content": "second"}


def test_note_is_sent_once_then_discarded():
    fake = FakeOrchestrator(["a", "b"])
    convo = Conversation(fake, "SYS")
    convo.note("remember this")
    convo.say("first")
    convo.say("second")
    assert any(m["content"] == "remember this" for m in fake.seen[0])
    assert not any(m["content"] == "remember this" for m in fake.seen[1])
    # And it never entered the persistent history.
    assert all(m["content"] != "remember this" for m in convo.history)


def test_say_with_tools_records_the_reply():
    convo = Conversation(FakeOrchestrator(["tooled"]), "SYS")
    reply, calls = convo.say_with_tools("do it", ToolRegistry())
    assert reply == "tooled"
    assert calls == []
    assert convo.history[-1] == {"role": "assistant", "content": "tooled"}


def test_opening_sends_a_stage_direction():
    fake = FakeOrchestrator(["welcome"])
    convo = Conversation(fake, "SYS")
    assert convo.opening("(the player arrives)") == "welcome"
    assert fake.seen[0][-1] == {"role": "user", "content": "(the player arrives)"}
