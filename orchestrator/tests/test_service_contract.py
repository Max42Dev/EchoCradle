"""Offline REST, output and tool contracts; no lifespan/model loading."""

import asyncio
import json
from types import SimpleNamespace
from uuid import uuid4

import httpx
import pytest
from starlette.exceptions import HTTPException

from orchestrator.service import (
    ServiceConfig, _Connection, _RemoteRegistry, _Runtime, _Session, create_app,
)
from orchestrator.service_protocol import (
    JSON_LIMIT, JobSpec, ServiceError, decode, decode_value, encode, session_spec,
)
from orchestrator.tools import ToolCall, ToolSpec


DECLARATION = ToolSpec("lookup", "Local lookup", {
    "type": "object", "required": ["key"],
    "properties": {"key": {"type": "string"}}, "additionalProperties": False,
}).to_openai()


def runtime(config: ServiceConfig | None = None) -> _Runtime:
    service = _Runtime(config or ServiceConfig(), None, None)
    service.state = "ready"
    service.diagnostics["actual"]["text"] = {"model_id": "offline"}
    return service


def dispose(service: _Runtime) -> None:
    for executor in service.executors.values():
        executor.shutdown(wait=True)


@pytest.mark.parametrize("raw", ['["valid"]', '"valid"', '42', 'true', 'null'])
def test_json_value_roots_do_not_relax_envelopes(raw: str) -> None:
    assert decode_value(raw) == json.loads(raw)
    with pytest.raises(ServiceError, match="object"):
        decode(raw)


@pytest.mark.parametrize("raw", ['{"x":1,"x":2}', '1e999', 'NaN', '[[' * 20 + '0' + ']]' * 20])
def test_value_decoder_remains_strict(raw: str) -> None:
    with pytest.raises(ServiceError):
        decode_value(raw)


@pytest.mark.parametrize("schema,output", [
    ({"type": "array", "items": {"type": "string"}}, '["valid"]'),
    ({"type": "string"}, '"valid"'), ({"type": "integer"}, '42'),
    ({"type": "boolean"}, 'true'), ({"type": "null"}, 'null'),
])
def test_inference_accepts_admitted_schema_roots(schema: dict, output: str) -> None:
    service = runtime()
    service.facade = SimpleNamespace(text=SimpleNamespace(
        _post=lambda *args: {"tokens": [1]}, stream_chat=lambda *args, **kwargs: iter([output]),
    ))
    job = service.submit(JobSpec.parse({"kind": "text", "text": "test", "json_schema": schema}))
    try:
        assert service.infer(job) == {"text": output, "calls": []}
    finally:
        dispose(service)


@pytest.mark.parametrize("temperature", [10**400, -(10**400), float("inf"), float("nan"), True])
def test_numeric_validation_is_public(temperature: object) -> None:
    with pytest.raises(ServiceError) as caught:
        JobSpec.parse({"kind": "text", "text": "test", "temperature": temperature})
    assert caught.value.code == "INVALID_SPEC"


def test_removed_options_and_schema_tools_rejected_before_socket_check() -> None:
    with pytest.raises(ServiceError) as caught:
        JobSpec.parse({"kind": "dialogue", "text": "test"})
    assert caught.value.code == "UNSUPPORTED_KIND"
    with pytest.raises(ServiceError):
        session_spec({"delivery_mode": "text"})
    service = runtime()
    sid = str(uuid4())
    service.sessions[sid] = _Session(sid, [DECLARATION])
    try:
        assert "dialogue" not in service.capabilities()["kinds"]
        with pytest.raises(ServiceError) as caught:
            service.submit(JobSpec.parse({
                "kind": "text", "text": "test", "session_id": sid,
                "json_schema": {"type": "object"},
            }))
        assert caught.value.code == "INVALID_SPEC"
    finally:
        dispose(service)


def test_http_errors_and_canonical_snapshots() -> None:
    async def run() -> None:
        app = create_app(ServiceConfig(port=8765), token="offline-secret")
        service = app.state.service
        service.state = "ready"
        service.loop = asyncio.get_running_loop()
        service.diagnostics["actual"]["text"] = {"model_id": "offline"}

        @app.get("/framework-error")
        async def framework_error() -> None:
            raise HTTPException(429, "secret path", headers={"Retry-After": "3"})

        @app.get("/unexpected-error")
        async def unexpected_error() -> None:
            raise RuntimeError("offline-secret private prompt C:/private")

        transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
        try:
            async with httpx.AsyncClient(
                transport=transport, base_url="http://127.0.0.1:8765",
                headers={"Authorization": "Bearer offline-secret"},
            ) as client:
                for method, path, status, code in [
                    ("GET", "/missing", 404, "NOT_FOUND"),
                    ("POST", "/v1/health/live", 405, "METHOD_NOT_ALLOWED"),
                    ("GET", "/framework-error", 429, "HTTP_ERROR"),
                    ("GET", "/unexpected-error", 500, "INTERNAL_ERROR"),
                ]:
                    response = await client.request(method, path)
                    assert response.status_code == status
                    error = response.json()["error"]
                    assert error["code"] == code and error["job_id"] is None
                    assert error["retryable"] == (status == 429)
                    assert "secret" not in response.text and "private" not in response.text
                    if status == 405:
                        assert response.headers["allow"] == "GET"
                    if status == 429:
                        assert response.headers["retry-after"] == "3"
                response = await client.post("/v1/jobs", json={
                    "kind": "text", "text": "test", "temperature": 10**400,
                })
                assert response.status_code == 422
                assert response.json()["error"]["code"] == "INVALID_SPEC"
                submitted = (await client.post("/v1/jobs", json={
                    "kind": "text", "text": "test", "idempotency_key": "one",
                })).json()
                job_id = submitted["job_id"]
                assert submitted == (await client.get(f"/v1/jobs/{job_id}")).json()
                assert submitted["v"] == submitted["protocol_version"] == 1
                assert submitted["selected_model"] == {"model_id": "offline"}
                cancelled = (await client.post(f"/v1/jobs/{job_id}/cancel")).json()
                assert cancelled == (await client.get(f"/v1/jobs/{job_id}")).json()
                assert cancelled == (await client.post(f"/v1/jobs/{job_id}/cancel")).json()
                replay = (await client.post("/v1/jobs", json={
                    "kind": "text", "text": "test", "idempotency_key": "one",
                })).json()
                assert replay == cancelled
                session = (await client.post("/v1/sessions", json={})).json()
                assert "delivery_mode" not in session
        finally:
            dispose(service)

    asyncio.run(run())


def test_complete_snapshot_and_event_budgets() -> None:
    service = runtime()
    sid = str(uuid4())
    session = _Session(sid, [])
    service.sessions[sid] = session
    job = service.submit(JobSpec.parse({"kind": "text", "text": "test", "session_id": sid}))
    try:
        result = {"text": "🙂" * 16_384}
        with pytest.raises(ServiceError):
            service.validate_success(job, result)
        # Find a REST-valid result whose state event overflows the same budget.
        overhead = len(encode(service.job_snapshot(
            job, result={"text": ""}, state="succeeded", host_pending=False,
            revision=job.revision + 2,
        )))
        result = {"text": "x" * (JSON_LIMIT - overhead)}
        snapshot = service.job_snapshot(job, result=result)
        assert len(encode(snapshot)) <= JSON_LIMIT
        with pytest.raises(ServiceError):
            service.validate_success(job, result)
    finally:
        dispose(service)


def test_tool_records_preserve_real_host_order_and_model_ids() -> None:
    service = runtime()
    sid = str(uuid4())
    service.sessions[sid] = _Session(sid, [DECLARATION])
    from orchestrator.service import _Job

    job = _Job(str(uuid4()), JobSpec.parse({
        "kind": "text", "text": "test", "session_id": sid,
    }), "offline", float("inf"))

    def chat(messages: list, registry: _RemoteRegistry, **kwargs: object) -> tuple:
        result = registry.invoke("lookup", {"key": 42})
        calls = [ToolCall("model-id", "lookup", {"key": 42}, result)]
        registry.results_delivered(calls)
        return "test", calls

    service.facade = SimpleNamespace(text=SimpleNamespace(
        _post=lambda *args: {"tokens": [1]}, stream_chat_with_tools=chat,
    ))
    try:
        result = service.infer(job)
        assert set(result) == {"text", "calls"}
        assert result["calls"] == [{
            "id": "model-id", "name": "lookup", "arguments": {"key": 42},
            "result": {"ok": False, "error": "INVALID_TOOL_ARGUMENTS"},
            "dispatch_id": None, "outcome": "rejected", "result_delivered": True,
        }]
    finally:
        dispose(service)


@pytest.mark.parametrize("ending", ["result", "disconnect", "cancel_after_result", "emit_failure"])
def test_remote_dispatch_is_distinct_and_unknown_until_result(ending: str) -> None:
    async def run() -> None:
        service = runtime()
        service.loop = asyncio.get_running_loop()
        sid = str(uuid4())
        session = _Session(sid, [DECLARATION])
        service.sessions[sid] = session
        connection = _Connection(service, session, None)
        connection.accepted = True
        session.connection = connection
        job = service.submit(JobSpec.parse({"kind": "text", "text": "test", "session_id": sid}))
        record = {"dispatch_id": None, "outcome": "rejected", "result_delivered": False}
        if ending == "emit_failure":
            async def fail_emit(*args: object) -> None:
                raise ServiceError("SOCKET_LOST", "offline emission failure", 409)
            connection.emit = fail_emit
        task = asyncio.create_task(service.remote_tool(job, "lookup", {"key": "a"}, record))
        try:
            if ending == "emit_failure":
                with pytest.raises(ServiceError):
                    await task
                assert record["dispatch_id"] is not None
                assert record["outcome"] == "unknown" and not record["result_delivered"]
                assert not connection.pending
                return
            _, state, _, _ = await connection.queue.get()
            assert state["v"] == 1
            _, payload, _, _ = await connection.queue.get()
            assert payload["call_id"] == record["dispatch_id"]
            assert "model_call_id" not in payload
            assert record["outcome"] == "unknown"
            pending = connection.pending[payload["call_id"]]
            if ending == "disconnect":
                service.disconnect(session, connection)
                with pytest.raises(ServiceError) as caught:
                    await task
                assert caught.value.code == "SOCKET_LOST"
                assert record["outcome"] == "unknown" and not record["result_delivered"]
            else:
                pending.future.set_result({"ok": True})
                pending.record.update(outcome="completed", result={"ok": True})
                if ending == "cancel_after_result":
                    service.cancel_job(job)
                    with pytest.raises(ServiceError) as caught:
                        await task
                    assert caught.value.code == "CANCELLED"
                    assert record["outcome"] == "completed" and not record["result_delivered"]
                else:
                    assert await task == {"ok": True}
                    assert record["outcome"] == "completed" and not record["result_delivered"]
            assert not connection.pending
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            dispose(service)

    asyncio.run(run())


def test_oversized_event_is_rejected_before_enqueue() -> None:
    async def run() -> None:
        service = runtime()
        service.loop = asyncio.get_running_loop()
        session = _Session(str(uuid4()), [])
        connection = _Connection(service, session, None)
        connection.accepted = True
        try:
            payload = {"text": "x" * (JSON_LIMIT - 20)}
            encode(payload)
            with pytest.raises(ServiceError) as caught:
                await connection.emit("text.delta", payload)
            assert caught.value.code == "PAYLOAD_TOO_LARGE"
            assert connection.queue.empty() and connection.queued_bytes == 0
        finally:
            dispose(service)

    asyncio.run(run())


@pytest.mark.parametrize("ending", ["response", "cancel", "failure"])
def test_real_host_reports_delivery_only_on_continuation_response(ending: str) -> None:
    from orchestrator.hosts.base import HostError
    from orchestrator.hosts.text import TextHost
    from orchestrator.service import _Job

    service = runtime()
    sid = str(uuid4())
    service.sessions[sid] = _Session(sid, [DECLARATION])
    job = _Job(str(uuid4()), JobSpec.parse({
        "kind": "text", "text": "test", "session_id": sid,
    }), "offline", float("inf"))
    host = TextHost(base_url="http://offline")
    host._model = SimpleNamespace(descriptor=SimpleNamespace(id="offline", params={}))
    host._post = lambda *args: {"tokens": [1]}
    requests = []

    def chunks(payload: dict, **kwargs: object):
        requests.append(payload)
        if len(requests) == 1:
            yield {"choices": [{"delta": {"tool_calls": [{
                "index": 0, "id": "model-id",
                "function": {"name": "lookup", "arguments": '{"key":42}'},
            }]}}]}
        elif ending == "failure":
            raise HostError("offline failure")
        else:
            yield {"choices": [{"delta": {"content": "answer"}}]}

    host._sse_chunks = chunks
    service.facade = SimpleNamespace(text=host)
    original = _RemoteRegistry.invoke

    def invoke(registry: _RemoteRegistry, name: str, arguments: dict):
        result = original(registry, name, arguments)
        if ending == "cancel":
            service.cancel_job(job)
        return result

    from unittest.mock import patch

    try:
        with patch.object(_RemoteRegistry, "invoke", invoke):
            if ending == "failure":
                with pytest.raises(HostError):
                    service.infer(job)
            else:
                service.infer(job)
        call = job.result["calls"][0]
        assert call["outcome"] == "rejected"
        assert call["result_delivered"] is (ending == "response")
        assert len(requests) == (1 if ending == "cancel" else 2)
        if ending == "response":
            assert requests[1]["messages"][-1]["role"] == "tool"
    finally:
        dispose(service)