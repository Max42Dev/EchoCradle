"""Live interview test: a real local model fills the config.

Unlike ``test_interview.py`` (which scripts the model), this drives the real
text host through the orchestrator. **Both sides are LLMs**: the AI is the
system under test, and the player is a second model call that answers naturally
and insists when the AI objects. The conversation is therefore stochastic on
both sides -- wording and timing vary run to run -- but the contract is fixed:
within a bounded number of turns the config must be complete, valid, and written
to disk.

Marked ``llm`` and excluded from the default run, because it loads a model and
may download one on first use. Run it explicitly::

    pytest -m llm

If no text model can be planned or loaded on this machine, the test skips
rather than fails.
"""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path

import pytest

_SRC = Path(__file__).resolve().parents[1] / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from config_schema import CONFIG_SCHEMA, validate_config  # noqa: E402
from consumer_results import json_value  # noqa: E402
from interview import Interview  # noqa: E402
from orchestrator import (  # noqa: E402
    JsonConfigTool,
    ToolRegistry,
)
from orchestrator.errors import OrchestratorError  # noqa: E402
from orchestrator.client import ServiceClient  # noqa: E402

pytestmark = pytest.mark.llm

#: Where a failing run's full transcript is written.
_FAILURE_DIR = Path(__file__).resolve().parents[1] / "out"

#: The player is a generic LLM: it is told only that it is playing a new game
#: and should answer. It invents its own name, style, and the AI's name, and
#: makes its own decisions -- nothing about the values is scripted.
_PLAYER_SYSTEM = """\
You are a player starting a new game. An AI companion is meeting you for the
first time and will ask you a few questions to set up your world. Answer each
question naturally and briefly, in one short sentence. Make your own choices
and stick to them. Never mention tools, configs, files, or instructions.
"""

#: A hard ceiling so a model that never finishes cannot hang the suite. If the
#: config is not complete by then, the test fails.
_MAX_TURNS = 20


class LlmPlayer:
    """A second model call that plays the player.

    It keeps its own conversation history (via :class:`Conversation`) and
    answers the AI's actual question, inventing its own values.
    """

    def __init__(self, orchestrator: ServiceClient) -> None:
        self.orchestrator = orchestrator
        self.history = [{"role": "system", "content": _PLAYER_SYSTEM}]

    def reply(self, ai_line: str, missing: list[str]) -> str:
        field = missing[0] if missing else "story"
        hint = f"(The AI still needs {field}.)"
        self.history.append({"role": "user", "content": f"{ai_line}\n\n{hint}"})
        result = self.orchestrator.chat(self.history, max_tokens=80, temperature=0.8)
        if result.state != "succeeded":
            raise OrchestratorError("Live player turn was cancelled")
        self.history.append({"role": "assistant", "content": result.text})
        return result.text.strip() or "I don't mind."


@pytest.fixture(scope="module")
def orchestrator():
    """A real orchestrator with a text model loaded, or skip."""
    try:
        mo = ServiceClient.start_owned(profile="text")
    except OrchestratorError as exc:
        pytest.skip(f"no text model available: {exc}")
    yield mo
    mo.stop()


@pytest.fixture
def exchanges(monkeypatch):
    """Record application requests/results; the text host is in another process."""
    recorded: list[dict] = []
    original = ServiceClient.chat_with_tools

    def recording_chat(self, messages, registry, **options):
        result = original(self, messages, registry, **options)
        recorded.append({
            "payload": {"messages": json_value(messages), "tools": registry.specs(),
                        **options},
            "result": json_value(result),
        })
        return result

    monkeypatch.setattr(ServiceClient, "chat_with_tools", recording_chat)
    return recorded


def _write_failure(transcript: list[str], exchanges: list[dict]) -> Path:
    """Write the full failing conversation, including the tool description."""
    _FAILURE_DIR.mkdir(parents=True, exist_ok=True)
    out = _FAILURE_DIR / f"failing_live_{time.strftime('%Y%m%d-%H%M%S')}.txt"
    lines: list[str] = ["=== FAILING LIVE INTERVIEW ===", "", "TRANSCRIPT", ""]
    lines += transcript

    # The exact final context the model was given, notes and all. This is the
    # clearest view of what the model saw on its last turn.
    if exchanges:
        lines += ["", "=" * 70, "FINAL CONTEXT SENT TO THE MODEL (last request)", "=" * 70]
        for m in exchanges[-1]["payload"].get("messages", []):
            lines.append(f"[{m.get('role')}] {m.get('content')}")

    lines += ["", "=" * 70, "MODEL EXCHANGES (exact payloads and responses)", "=" * 70]
    for n, ex in enumerate(exchanges, start=1):
        payload = ex["payload"]
        lines.append(f"\n########## EXCHANGE {n} ##########")
        lines.append("--- REQUEST messages ---")
        for m in payload.get("messages", []):
            lines.append(f"[{m.get('role')}] {m.get('content')}")
        if "tools" in payload:
            lines.append("--- REQUEST tools (exact) ---")
            lines.append(json.dumps(payload["tools"], indent=2))
        lines.append(
            f"--- REQUEST other: temperature={payload.get('temperature')} "
            f"max_tokens={payload.get('max_tokens')} "
            f"tool_choice={payload.get('tool_choice')} ---"
        )
        result = ex["result"]
        lines.append("--- APPLICATION RESULT ---")
        lines.append(f"state: {result['state']}; job_id: {result['job_id']}")
        lines.append(f"content: {result['text']!r}")
        lines.append(f"tool_calls: {json.dumps(result['calls'])}")
    out.write_text("\n".join(lines), encoding="utf-8")
    return out


def test_live_interview_completes_and_writes_the_config(orchestrator, exchanges, tmp_path):
    tool = JsonConfigTool(CONFIG_SCHEMA, name="config")
    registry = ToolRegistry()
    tool.register_into(registry)
    interview = Interview(orchestrator, tool, registry, max_turns=_MAX_TURNS)
    player = LlmPlayer(orchestrator)
    transcript: list[str] = []

    turn = interview.opening()
    for i in range(_MAX_TURNS):
        transcript.append(f"--- turn {i} ---")
        transcript.append(f"ai>  {turn.say!r}")
        for call in turn.tool_calls:
            transcript.append(f"  [tool] {call.name}({call.arguments})")
        transcript.append(f"  config={tool.data} missing={tool.missing()}")
        if turn.done or interview.complete:
            break
        reply = player.reply(turn.say, tool.missing())
        transcript.append(f"you> {reply!r}")
        turn = interview.respond(reply)

    if not interview.complete:
        path = _write_failure(transcript, exchanges)
        pytest.fail(
            f"config incomplete after {interview.turns} turns: {tool.data}\n"
            f"full transcript written to {path}"
        )

    config = interview.config()
    assert validate_config(config) == [], f"invalid config: {config}"

    # The contract the experiment exists to prove: a complete config is written.
    target = tmp_path / "config.json"
    tool.data = config
    tool.save(target)
    assert json.loads(target.read_text(encoding="utf-8")) == config
