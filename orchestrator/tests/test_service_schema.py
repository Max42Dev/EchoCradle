"""Response-schema wire contract and server forwarding, without native models."""

from types import SimpleNamespace
from uuid import uuid4

import pytest

from orchestrator.client import ServiceClient
from orchestrator.service import _Runtime
from orchestrator.service_protocol import JobSpec, ServiceError


SCHEMA = {
    "type": "object", "required": ["name"], "additionalProperties": False,
    "properties": {"name": {"type": "string"}},
}


def test_client_forwards_schema() -> None:
    client = object.__new__(ServiceClient)
    submitted = []
    final = {"job_id": str(uuid4()), "turn_id": None, "state": "succeeded",
             "result": {"text": '{"name":"Ada"}', "calls": []}}
    client._submit = lambda spec, **kwargs: submitted.append(spec) or final
    client._poll = lambda job, **kwargs: final
    result = client.chat([{"role": "user", "content": "Name?"}], json_schema=SCHEMA)
    assert result.state == "succeeded" and result.text == '{"name":"Ada"}'
    assert submitted[0]["json_schema"] == SCHEMA


@pytest.mark.parametrize("options", [
    {"kind": "dialogue", "json_schema": SCHEMA},
    {"kind": "text", "json_schema": []},
    {"kind": "text", "json_schema": {"type": "invalid"}},
    {"kind": "text", "json_schema": {"$ref": "https://example.com/schema"}},
])
def test_invalid_schema_specs_are_rejected(options: dict) -> None:
    with pytest.raises(ServiceError):
        JobSpec.parse({"text": "Name?", **options})


@pytest.mark.parametrize("output,valid", [
    ('{"name":"Ada"}', True), ('{"name":42}', False),
    ('{"name":"Ada","extra":true}', False), ('not JSON', False),
])
def test_server_constrains_and_validates_output(output: str, valid: bool) -> None:
    captured = []

    def chat(messages: list, **kwargs: object):
        captured.append(kwargs)
        yield output

    runtime = object.__new__(_Runtime)
    runtime.sessions = {}
    runtime.facade = SimpleNamespace(text=SimpleNamespace(
        stream_chat=chat, _post=lambda *args: {"tokens": [1]},
    ))
    job = SimpleNamespace(
        spec=JobSpec.parse({"kind": "text", "text": "Name?", "json_schema": SCHEMA}),
        should_stop=lambda: False,
    )
    if valid:
        assert runtime.infer(job) == {"text": output, "calls": []}
    else:
        with pytest.raises(ServiceError):
            runtime.infer(job)
    assert captured[0]["json_schema"] == SCHEMA