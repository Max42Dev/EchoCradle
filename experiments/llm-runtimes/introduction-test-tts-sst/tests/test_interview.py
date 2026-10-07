"""The interview state machine, driven by a fake orchestrator.

These tests never touch a model: the fake returns scripted replies and executes
tool calls against a real :class:`JsonConfigTool`, so the contract (tool-owned
config, malformed-reply recovery) is verified deterministically.

One model call per turn, so the fake's reply queue is consumed one entry per turn.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

_SRC = Path(__file__).resolve().parents[1] / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from config_schema import CONFIG_SCHEMA  # noqa: E402
from interview import Interview  # noqa: E402
from orchestrator import JsonConfigTool, ToolRegistry  # noqa: E402


class FakeOrchestrator:
    """Returns queued replies and executes tool calls against the registry."""

    def __init__(self, replies: list[str], tool_calls: list[list[tuple]] | None = None) -> None:
        self.replies = list(replies)
        #: Per-turn list of (tool_name, arguments) to simulate the model calling.
        self.tool_calls = list(tool_calls or [])
        self.calls: list[list[dict[str, str]]] = []

    def chat_with_tools(self, messages, registry, **kwargs):
        self.calls.append(messages)
        calls = []
        if self.tool_calls:
            from orchestrator.tools import ToolCall

            for name, arguments in self.tool_calls.pop(0):
                result = registry.invoke(name, arguments)
                calls.append(ToolCall(id="", name=name, arguments=arguments, result=result))
        reply = self.replies.pop(0) if self.replies else json.dumps({"say": "Hm."})
        return reply, calls

    def chat(self, messages, **kwargs) -> str:
        self.calls.append(messages)
        return self.replies.pop(0) if self.replies else ""

    def stream_chat_with_tools(
        self, messages, registry, *, on_text=None, should_stop=None, **kwargs
    ):
        """Emit the queued reply as deltas, honouring an interruption.

        Mirrors the real host: text arrives in chunks, ``on_text`` sees each
        one, and ``should_stop`` is polled between chunks so a barge-in cuts
        generation short.
        """
        self.calls.append(messages)
        calls = []
        if should_stop is not None and should_stop():
            return "", calls
        if self.tool_calls:
            from orchestrator.tools import ToolCall

            for name, arguments in self.tool_calls.pop(0):
                result = registry.invoke(name, arguments)
                calls.append(ToolCall(id="", name=name, arguments=arguments, result=result))
        reply = self.replies.pop(0) if self.replies else ""
        # Feed a few characters at a time so sentence boundaries appear mid-stream.
        for i in range(0, len(reply), 4):
            if should_stop is not None and should_stop():
                break
            if on_text is not None:
                on_text(reply[i : i + 4])
        return reply, calls


def _say(text: str) -> str:
    return json.dumps({"say": text})


def _make(replies: list[str], tool_calls=None, data: dict | None = None):
    tool = JsonConfigTool(CONFIG_SCHEMA, name="config", data=data)
    registry = ToolRegistry()
    tool.register_into(registry)
    fake = FakeOrchestrator(replies, tool_calls)
    return Interview(fake, tool, registry), tool, registry


def test_opening_asks_without_user_input():
    interview, _, _ = _make([_say("What should I call you?")])
    turn = interview.opening()
    assert turn.say == "What should I call you?"
    assert not turn.done


def test_opening_without_tool_calls_leaves_config_empty():
    interview, tool, _ = _make([_say("Hello.")])
    interview.opening()
    assert tool.data == {}


def test_cancelled_turn_never_executes_tools():
    interview, tool, _ = _make([], tool_calls=[[
        ("config_set", {"field": "username", "value": "Ada"}),
    ]])
    turn, interrupted = interview.respond_streaming("I'm Ada.", lambda text: None,
                                                   should_stop=lambda: True)
    assert interrupted and not turn.tool_calls and not tool.data


def test_model_records_a_value_while_conversing():
    interview, tool, _ = _make(
        [_say("Ada, is it?")],
        tool_calls=[[("config_set", {"field": "username", "value": "Ada"})]],
    )
    turn = interview.respond("I'm Ada.")
    assert tool.data["username"] == "Ada"
    assert turn.say == "Ada, is it?"
    assert len(turn.tool_calls) == 1


@pytest.mark.parametrize("streaming", [False, True])
def test_native_tools_record_values_in_both_turn_modes(streaming):
    interview, tool, _ = _make(["Ada, is it?"], tool_calls=[[
        ("config_set", {"field": "username", "value": "Ada"}),
    ]])
    if streaming:
        spoken = []
        turn, interrupted = interview.respond_streaming("I'm Ada.", spoken.append)
        assert not interrupted and spoken == ["Ada, is it?"]
    else:
        turn = interview.respond("I'm Ada.")
    assert tool.data == {"username": "Ada"}
    assert turn.tool_calls[0].result["ok"]


def test_no_native_call_does_not_fabricate_config_updates():
    interview, tool, _ = _make(["Hello."])
    interview.respond("I'm Ada.")
    assert tool.data == {}


def test_several_values_in_one_turn():
    interview, tool, _ = _make(
        [_say("Noted.")],
        tool_calls=[
            [
                ("config_set", {"field": "username", "value": "Ada"}),
                ("config_set", {"field": "style", "value": "medieval"}),
            ]
        ],
    )
    interview.respond("Ada, medieval.")
    assert tool.data == {"username": "Ada", "style": "medieval"}


def test_invalid_tool_value_is_rejected_by_the_tool():
    interview, tool, _ = _make(
        [_say("Hm.")],
        tool_calls=[[("config_set", {"field": "username", "value": ""})]],
    )
    turn = interview.respond("uh")
    assert "username" not in tool.data
    assert not turn.tool_calls[0].result["ok"]


def test_unknown_field_is_rejected():
    interview, tool, _ = _make(
        [_say("Hm.")],
        tool_calls=[[("config_set", {"field": "nope", "value": "x"})]],
    )
    interview.respond("x")
    assert "nope" not in tool.data


def test_a_recorded_ai_name_is_kept_as_the_model_gave_it():
    """Whether to object to a name is the model's choice, never the code's."""
    interview, tool, _ = _make(
        [_say("A name!")],
        tool_calls=[[("config_set", {"field": "ai_name", "value": "Bob"})]],
    )
    turn = interview.respond("I want to call you Bob")
    assert tool.data.get("ai_name") == "Bob"
    assert turn.say == "A name!"


def test_the_name_prompt_lets_the_model_object_once():
    """The snobbishness is described in character, not enforced in code."""
    from prompts import build_system_prompt

    prompt = build_system_prompt().lower()
    assert "object to it once" in prompt
    assert "if they insist" in prompt


def test_done_requires_all_required_fields():
    interview, _, _ = _make([_say("Done!")], data={"username": "Ada"})
    assert not interview.opening().done


def test_an_accepted_name_completes_the_config():
    interview, tool, _ = _make(
        [_say("A name!")],
        tool_calls=[[("config_set", {"field": "ai_name", "value": "Vex"})]],
        data={"username": "Ada", "style": "nature"},
    )
    assert interview.respond("Vex").done
    assert interview.complete


def test_plain_text_reply_is_accepted_without_retry():
    """A model that ignores the JSON envelope is still understood."""
    interview, _, _ = _make(["Just plain text."])
    turn = interview.respond("hello")
    assert turn.say == "Just plain text."
    assert not turn.repaired


def test_empty_reply_triggers_a_retry():
    interview, _, _ = _make(["", _say("Recovered.")])
    turn = interview.respond("hello")
    assert turn.say == "Recovered."
    assert turn.repaired


def test_stray_json_fragment_triggers_a_retry():
    """A JSON blob with no 'say' is not speakable, so the model is re-asked."""
    interview, _, _ = _make(['{"tool": {"name": "none"}}', _say("Recovered.")])
    turn = interview.respond("hello")
    assert turn.say == "Recovered."
    assert turn.repaired


def test_retry_failure_falls_back_to_a_scripted_line():
    interview, _, _ = _make(["", ""])
    turn = interview.respond("hello")
    assert turn.repaired
    assert turn.say


def test_json_embedded_in_prose_is_extracted():
    interview, _, _ = _make(['Sure! {"say": "Hi."}'])
    assert interview.respond("hi").say == "Hi."


def test_host_failure_produces_a_fallback_line():
    class Broken:
        def chat_with_tools(self, messages, registry, **kwargs):
            raise RuntimeError("host down")

        def chat(self, messages, **kwargs):
            raise RuntimeError("host down")

    tool = JsonConfigTool(CONFIG_SCHEMA, name="config")
    registry = ToolRegistry()
    tool.register_into(registry)
    interview = Interview(Broken(), tool, registry)
    turn = interview.opening()
    assert turn.repaired
    assert turn.say


def test_validation_errors_are_reported_to_the_model():
    """After a turn, schema problems are fed back into the next turn's messages."""
    interview, _, _ = _make([_say("Hello."), _say("Still here.")])
    turn = interview.respond("hi")
    assert turn.validation_errors, "an incomplete config must report problems"
    assert any("username" in e for e in turn.validation_errors)

    interview.respond("again")
    last = interview.orchestrator.calls[-1]
    joined = " ".join(m["content"] for m in last)
    assert "does not yet match its schema" in joined
    assert "username" in joined


def test_no_validation_note_when_config_is_valid():
    interview, _, _ = _make(
        [_say("ok")],
        data={"username": "Ada", "style": "nature", "ai_name": "Vex"},
    )
    turn = interview.opening()
    assert turn.validation_errors == []
    joined = " ".join(m["content"] for m in interview.orchestrator.calls[-1])
    assert "does not yet match its schema" not in joined


def test_validation_errors_clear_once_complete():
    interview, _, _ = _make(
        [_say("ok"), _say("ok"), _say("ok")],
        tool_calls=[
            [("config_set", {"field": "username", "value": "Ada"})],
            [("config_set", {"field": "style", "value": "nature"})],
            [("config_set", {"field": "ai_name", "value": "Vex"})],
        ],
    )
    interview.respond("Ada")
    assert interview.validation_errors
    interview.respond("nature")
    assert interview.validation_errors
    interview.respond("Vex")
    assert interview.validation_errors == []


def test_declined_optional_field_is_not_recorded():
    interview, tool, _ = _make(
        [_say("Very well.")],
        tool_calls=[[("config_set", {"field": "story", "value": "no story"})]],
    )
    interview.respond("no story")
    assert "story" not in tool.data


def test_declined_value_does_not_override_a_real_one():
    interview, tool, _ = _make(
        [_say("ok")],
        tool_calls=[[("config_set", {"field": "username", "value": "none"})]],
    )
    interview.respond("none")
    assert "username" not in tool.data


def test_config_is_normalised():
    interview, _, _ = _make(
        [_say("ok"), _say("ok"), _say("ok"), _say("ok")],
        tool_calls=[
            [("config_set", {"field": "username", "value": "  Ada "})],
            [("config_set", {"field": "style", "value": "nature"})],
            [("config_set", {"field": "ai_name", "value": "Vex"})],
            [("config_set", {"field": "ai_name", "value": "Vex"})],
        ],
    )
    interview.respond("Ada")
    interview.respond("nature")
    interview.respond("Vex")
    interview.respond("I insist on Vex.")
    assert interview.config() == {"username": "Ada", "style": "nature", "ai_name": "Vex"}


def test_streaming_speaks_each_sentence_as_it_arrives():
    interview, _, _ = _make(["Hello there. How are you?"])
    spoken: list[str] = []

    turn, interrupted = interview.opening_streaming(spoken.append)

    assert spoken == ["Hello there.", "How are you?"]
    assert not interrupted
    assert turn.say == "Hello there. How are you?"


def test_interruption_truncates_the_history_to_what_was_spoken():
    # The model wants to say three sentences; the player cuts in after the
    # first. The history must keep only the first, so the model's next turn
    # sees what the player actually heard.
    interview, _, _ = _make(["First sentence. Second sentence. Third sentence."])
    spoken: list[str] = []

    def should_stop() -> bool:
        return len(spoken) >= 1

    turn, interrupted = interview.opening_streaming(spoken.append, should_stop)

    assert interrupted
    assert spoken == ["First sentence."]
    assert turn.say == "First sentence."
    assert interview.history[-1] == {"role": "assistant", "content": "First sentence."}


def test_interrupted_turn_keeps_the_user_message_for_the_next_turn():
    full = "First sentence here. Second sentence here. Third sentence here."
    interview, _, _ = _make([full, "Understood."])
    spoken: list[str] = []

    def should_stop() -> bool:
        return len(spoken) >= 1

    interview.respond_streaming("my name is Max", spoken.append, should_stop)
    # The player's words are in the history, followed by the truncated reply.
    assert interview.history[-2] == {"role": "user", "content": "my name is Max"}
    assert interview.history[-1]["content"] == "First sentence here."
    assert interview.history[-1]["content"] != full


def test_cancelled_before_first_sentence_does_not_emit_fallback():
    interview, _, _ = _make(["A sentence that must never be spoken."])
    emitted: list[str] = []
    turn, interrupted = interview.opening_streaming(emitted.append, lambda: True)
    assert interrupted
    assert turn.say == ""
    assert not emitted


def test_cancelled_fragment_is_not_flushed_to_speech():
    interview, _, _ = _make(["First sentence. An unfinished continuation"])
    emitted: list[str] = []
    turn, interrupted = interview.opening_streaming(
        emitted.append, lambda: bool(emitted)
    )
    assert interrupted
    assert emitted == ["First sentence."]
    assert turn.say == "First sentence."


def test_streaming_never_speaks_a_meta_sentence():
    interview, _, _ = _make(["Hello there, traveller. I must call the config tool now."])
    spoken: list[str] = []

    interview.opening_streaming(spoken.append)

    assert spoken == ["Hello there, traveller."]


def test_prompt_keeps_spoken_history_and_private_interruption_separate() -> None:
    from prompts import build_turn_messages

    marker = "[Player interrupted; partial-sentence wording is approximate.]"
    history = [
        {"role": "assistant", "content": "Welcome to"},
        {"role": "system", "content": marker},
        {"role": "user", "content": "Actually, call me Elowen."},
    ]
    original = [dict(message) for message in history]
    slots = {"username": "Rowan"}
    messages = build_turn_messages(history, slots)

    assert messages[1:4] == history
    assert history == original
    assert slots == {"username": "Rowan"}
    assert all(marker not in m["content"] for m in messages if m["role"] == "assistant")
    assert messages[-1]["role"] == "system"
    assert '"username": "Rowan"' in messages[-1]["content"]


def test_opening_context_is_system_only_not_fabricated_player_dialogue() -> None:
    from prompts import build_turn_messages

    messages = build_turn_messages([], {})
    assert all(message["role"] == "system" for message in messages)
    assert any("Greet them and begin" in message["content"] for message in messages)


def test_prompt_instructs_transcript_resolution_and_correction_without_bookkeeping() -> None:
    from prompts import build_system_prompt

    prompt = build_system_prompt()
    assert "misspellings" in prompt
    assert "spelled letter by letter" in prompt
    assert "one name, not two" in prompt
    assert "direct correction replaces that field" in prompt
    assert "including fields already filled" in prompt
    assert "Record confirmed values" in prompt
    assert "never repeat bookkeeping" in prompt
    assert "JSON" not in prompt
    assert "interruption notes are not dialogue" in prompt
    assert len(prompt.split()) < 350


def test_interruption_metadata_is_private_not_assistant_dialogue():
    from prompts import build_turn_messages

    history = [{"role": "assistant", "content": "Hello…"},
               {"role": "user", "content": "My name is Ada."}]
    messages = build_turn_messages(history, {}, interruption_context="Delivery was interrupted.")
    assert history[0]["content"] == "Hello…"
    notes = [message for message in messages if "Delivery was interrupted." in message["content"]]
    assert len(notes) == 1
    assert notes[0]["role"] == "system"


def test_validation_and_stall_notes_stay_private_and_do_not_invent_values() -> None:
    from prompts import build_turn_messages

    history = [{"role": "user", "content": "I'm not sure yet."}]
    messages = build_turn_messages(
        history, {}, validation_errors=["username is missing"], stalled_field="username"
    )
    assert messages[1] == history[0]
    assert all(message["role"] == "system" for message in messages[2:])
    assert "do not invent a value" in messages[-1]["content"]
    assert "If the player gave or confirmed" in messages[-1]["content"]


@pytest.mark.parametrize("streaming", [False, True])
def test_correction_is_owned_by_model_tool_call_not_transcript_rules(streaming: bool) -> None:
    interview, tool, _ = _make(
        ["Elowen, then."],
        tool_calls=[[('config_set', {"field": "username", "value": "Elowen"})]],
        data={"username": "Rowan"},
    )
    if streaming:
        result, _ = interview.respond_streaming("E L O W E N Elowen", lambda _: None)
    else:
        result = interview.respond("E L O W E N Elowen")
    assert tool.data["username"] == "Elowen"
    assert result.say == "Elowen, then."


def test_transcript_alone_never_sets_or_overwrites_a_value() -> None:
    interview, tool, _ = _make(["Did I hear that right?"], data={"username": "Rowan"})
    interview.respond("M A X Max")
    assert tool.data == {"username": "Rowan"}


@pytest.mark.parametrize(
    "raw",
    [
        "(If they give a name, we will set username.)",
        "[If they give a name, we will set username.]",
        "[Player interrupted; partial-sentence wording is approximate.]",
        "We will set username to the confirmed name.",
        "I'll update the field now.",
        "The JSON schema requires a username.",
        "The schema says the name is required.",
        "The conversation history contains an interruption.",
        "According to my instructions, I should ask for a name.",
        "recorded={}; still_needed=['username']; ask_them_next=name",
        "I need to use config_set now.",
    ],
)
def test_internal_commentary_is_not_speakable(raw: str) -> None:
    assert Interview._spoken_line(raw) == ""


@pytest.mark.parametrize(
    "aside",
    [
        "(If they give a name, we will set username.)",
        "[If they give a name, we will set username.]",
        "[Player interrupted; partial-sentence wording is approximate.]",
        "[smiles theatrically]",
        "(sighs)",
        "[internal note: call config_set. Record the field next.]",
    ],
)
@pytest.mark.parametrize("streaming", [False, True])
def test_embedded_asides_never_reach_speech_or_history(aside: str, streaming: bool) -> None:
    interview, _, _ = _make([f"{aside} Hello there. {aside} What should I call you?"])
    if streaming:
        spoken: list[str] = []
        turn, _ = interview.opening_streaming(spoken.append)
        assert spoken == ["Hello there.", "What should I call you?"]
    else:
        turn = interview.opening()
    assert " ".join(turn.say.split()) == "Hello there. What should I call you?"
    assert interview.history[-1]["content"] == turn.say
    assert aside not in turn.say


@pytest.mark.parametrize(
    "line",
    [
        "I'll keep calling you Rowan, as promised.",
        "If you insist, Vex it is, without fuss.",
        "I will offer you a world of forests.",
        "No need for ceremony; you can decline the invitation.",
        "I need to call you something. What do you prefer?",
        "The rules of this kingdom are strange.",
        "Call me [Vex].",
        "A forest (with ancient trees) sounds wonderful.",
        "I will now welcome you properly.",
        "Let's finalise our journey.",
    ],
)
def test_valid_conversation_is_preserved(line: str) -> None:
    assert Interview._spoken_line(line) == line


def test_streaming_preserves_conversational_aside_with_sentence_boundary() -> None:
    interview, _, _ = _make(["A forest (a quiet place. Full of old trees) awaits you."])
    spoken: list[str] = []
    turn, _ = interview.opening_streaming(spoken.append)
    assert turn.say == "A forest (a quiet place. Full of old trees) awaits you."
    assert " ".join(spoken) == turn.say


def test_streaming_unclosed_internal_aside_is_never_flushed() -> None:
    interview, _, _ = _make(["Welcome. [internal note: we will set username. Then ask more"])
    spoken: list[str] = []
    turn, _ = interview.opening_streaming(spoken.append)
    assert spoken == ["Welcome."]
    assert turn.say == "Welcome."


@pytest.mark.parametrize("streaming", [False, True])
def test_removed_direction_does_not_join_spoken_words(streaming: bool) -> None:
    interview, _, _ = _make(["Hello[smiles]traveller."])
    if streaming:
        turn, _ = interview.opening_streaming(lambda _: None)
    else:
        turn = interview.opening()
    assert turn.say == "Hello traveller."


def test_meta_retry_retains_model_tool_value_and_speaks_only_recovery() -> None:
    interview, tool, _ = _make(
        ["We will set username now.", "Elowen, a fine name."],
        tool_calls=[[('config_set', {"field": "username", "value": "Elowen"})]],
    )
    turn = interview.respond("Elowen")
    assert turn.repaired
    assert turn.say == "Elowen, a fine name."
    assert tool.data["username"] == "Elowen"
    assert len(turn.tool_calls) == 1
    assert interview.history[-1]["content"] == turn.say
