"""Native declarations are transported; tool implementations remain local."""

from types import SimpleNamespace
from uuid import uuid4

import pytest

from orchestrator.service import _RemoteRegistry, _Session
from orchestrator.service_protocol import ServiceError, session_spec
from orchestrator.tools import ToolSpec


DECLARATION = ToolSpec("lookup_weather", "Look up local weather for a town.", {
    "type": "object", "required": ["town"], "additionalProperties": False,
    "properties": {"town": {"type": "string"}},
}).to_openai()


def test_session_accepts_custom_tool_description() -> None:
    declarations, mode = session_spec({"tools": [DECLARATION]})
    assert declarations == [DECLARATION] and mode == "text"


@pytest.mark.parametrize("tools", [
    [DECLARATION, DECLARATION], [{"type": "code", "function": {}}],
    [ToolSpec("bad name", "", {"type": "object"}).to_openai()],
    [ToolSpec("bad_schema", "", {"type": "object", "$ref": "file:///secret"}).to_openai()],
])
def test_invalid_declarations_rejected(tools: list) -> None:
    with pytest.raises(ServiceError):
        session_spec({"tools": tools})


def test_remote_registry_passes_full_description_and_rejects_bad_args() -> None:
    identifier = str(uuid4())
    runtime = SimpleNamespace(sessions={identifier: _Session(identifier, [DECLARATION], "text")})
    job = SimpleNamespace(spec=SimpleNamespace(session_id=identifier), should_stop=lambda: False)
    registry = _RemoteRegistry(runtime, job)
    assert registry.specs() == [DECLARATION]
    assert registry.invoke("lookup_weather", {"town": 42})["error"] == "INVALID_TOOL_ARGUMENTS"
    assert registry.invoke("missing", {})["error"] == "TOOL_NOT_PERMITTED"
    copy = registry.specs()
    copy[0]["function"]["description"] = "changed"
    assert registry.specs() == [DECLARATION]