"""Offline application contracts: authoritative facts never conflate call IDs."""

from dataclasses import FrozenInstanceError
from threading import Event, RLock, get_ident
from types import SimpleNamespace
from uuid import uuid4
import time

import pytest

from orchestrator.client import (
    ServiceClient, ServiceClientError, _Turn, _wire_error,
)
from orchestrator.client_types import TextResult, ToolOutcome


def snapshot(state: str = "succeeded", calls: list | None = None) -> dict:
    return {
        "job_id": str(uuid4()), "turn_id": str(uuid4()), "state": state,
        "result": {"text": "answer", "calls": calls or []},
    }


def record(dispatch: str | None, **changes: object) -> dict:
    return {
        "id": "model-7", "dispatch_id": dispatch, "name": "save",
        "arguments": {"value": [1]}, "result": None,
        "outcome": "unknown", "result_delivered": False, **changes,
    }


def test_results_are_deeply_immutable_and_detached() -> None:
    source = record(str(uuid4()), outcome="completed", result={"values": [1]})
    result = ServiceClient._text_result(snapshot(calls=[source]))
    source["arguments"]["value"].append(2)
    source["result"]["values"].append(2)
    assert result.calls[0].arguments["value"] == (1,)
    assert result.calls[0].result["values"] == (1,)
    with pytest.raises(FrozenInstanceError):
        result.text = "changed"
    with pytest.raises(TypeError):
        result.calls[0].arguments["value"] = ()


def test_authoritative_rejected_and_unknown_calls_merge_execution_once() -> None:
    dispatch = str(uuid4())
    evidence = ToolOutcome(None, dispatch, "save", {"value": [1]}, {"saved": True},
                           "unknown", False, True)
    final = snapshot("cancelled", [
        record(None, id="rejected", outcome="rejected"),
        record(dispatch, id=None),
    ])
    result = ServiceClient._text_result(final, local=(evidence,))
    assert result.state == "cancelled"
    assert len(result.calls) == 2
    assert result.calls[0].outcome == "rejected"
    assert not result.calls[0].executed_locally
    assert result.calls[1].model_call_id is None
    assert result.calls[1].dispatch_id == dispatch
    assert result.calls[1].result == {"saved": True}
    assert result.calls[1].executed_locally
    assert result.calls[1].outcome == "unknown"
    assert not result.calls[1].result_delivered


def test_completed_server_result_wins_over_local_evidence() -> None:
    dispatch = str(uuid4())
    local = ToolOutcome(None, dispatch, "save", {"value": [1]}, "local", "unknown", False, True)
    result = ServiceClient._text_result(snapshot(calls=[record(
        dispatch, result="server", outcome="completed", result_delivered=True,
    )]), local=(local,))
    assert result.calls[0].result == "server"
    assert result.calls[0].model_call_id == "model-7"
    assert result.calls[0].result_delivered


def test_rejected_attempt_error_can_be_delivered_to_model() -> None:
    result = ServiceClient._text_result(snapshot(calls=[record(
        None, outcome="rejected", result={"error": "INVALID_TOOL_ARGUMENTS"},
        result_delivered=True,
    )]))
    assert result.calls[0].outcome == "rejected"
    assert result.calls[0].result_delivered
    assert not result.calls[0].executed_locally


def test_tool_arguments_and_result_have_independent_byte_budgets() -> None:
    result = ServiceClient._text_result(snapshot(calls=[record(
        str(uuid4()), arguments={"value": "x" * 9000}, result={"value": "y" * 9000},
        outcome="completed", result_delivered=True,
    )]))
    assert len(result.calls[0].arguments["value"]) == 9000
    assert len(result.calls[0].result["value"]) == 9000


def test_missing_server_record_preserves_unknown_execution_without_fake_model_id() -> None:
    local = ToolOutcome(None, str(uuid4()), "save", {}, None, "unknown", False, True)
    result = ServiceClient._text_result(snapshot("cancelled"), local=(local,))
    assert result.calls == (local,)


@pytest.mark.parametrize("changes", [
    {"outcome": "success"}, {"result_delivered": 1}, {"id": 1},
    {"dispatch_id": "model-id"}, {"outcome": "unknown", "result_delivered": True},
    {"arguments": []},
])
def test_malformed_outcome_fails_closed(changes: dict) -> None:
    with pytest.raises(ServiceClientError) as error:
        ServiceClient._text_result(snapshot(calls=[record(None, **changes)]))
    assert error.value.code == "PROTOCOL_ERROR"


def test_duplicate_dispatch_is_not_doubled() -> None:
    dispatch = str(uuid4())
    with pytest.raises(ServiceClientError, match="Duplicate"):
        ServiceClient._text_result(snapshot(calls=[record(dispatch), record(dispatch)]))


def test_failed_job_has_typed_partial_and_sanitized_metadata() -> None:
    final = snapshot("failed")
    final["error"] = {"code": "TOOL_TIMEOUT", "message": "SECRET", "retryable": False,
                      "job_id": final["job_id"]}
    with pytest.raises(ServiceClientError) as error:
        ServiceClient._text_result(final)
    assert error.value.job_id == final["job_id"]
    assert error.value.partial_result.state == "failed"
    assert error.value.partial_result.text == "answer"
    assert error.value.phase == "inference"
    assert "SECRET" not in str(error.value)


@pytest.mark.parametrize("changes", [
    {"retryable": "yes"}, {"code": "secret\ntext"}, {"job_id": "bad"},
])
def test_error_metadata_is_validated(changes: dict) -> None:
    with pytest.raises(ServiceClientError, match="PROTOCOL_ERROR"):
        _wire_error({"code": "RESOURCE_BUSY", "retryable": True, "job_id": None, **changes})


def bare_client() -> ServiceClient:
    client = object.__new__(ServiceClient)
    client._closing = Event()
    client._lock = RLock()
    client._jobs = set()
    client._sockets = set()
    return client


def test_chat_forwards_any_root_schema_and_returns_cancelled_result() -> None:
    client = bare_client()
    captured = []
    final = snapshot("cancelled")
    client._submit = lambda spec, **kwargs: captured.append(spec) or final
    client._poll = lambda job, **kwargs: job
    event = Event()
    result = client.chat([{"role": "user", "content": "array?"}],
                         json_schema={"type": "array"}, cancellation=event)
    assert isinstance(result, TextResult)
    assert result.state == "cancelled"
    assert captured[0]["json_schema"] == {"type": "array"}


def test_sessions_send_only_tools_and_tool_turns_use_text(monkeypatch) -> None:
    client = bare_client()
    requests = []
    session = str(uuid4())
    client._request = lambda method, path, **kw: requests.append((method, path, kw)) or {
        "session_id": session,
    }
    monkeypatch.setattr(_Turn, "run", lambda self, *args, **kw: TextResult(
        "succeeded", "ok", str(uuid4()), self.turn,
    ))
    monkeypatch.setattr(_Turn, "close", lambda self: None)
    registry = SimpleNamespace(specs=lambda: [], invoke=lambda *args: None)
    assert client.stream_chat_with_tools([], registry).state == "succeeded"
    assert requests[0][2]["payload"] == {"tools": []}
    assert client.stream_chat([]).state == "succeeded"
    assert requests[2][2]["payload"] == {"tools": []}


def make_turn(registry: object, cancellation: Event | None = None) -> _Turn:
    client = bare_client()
    client._snapshot = lambda *args, **kwargs: {"state": "waiting_tool"}
    turn = _Turn(client, str(uuid4()), str(uuid4()), registry, ["save"], None,
                 cancellation, time.monotonic() + 1)
    turn.job = str(uuid4())
    return turn


def test_tool_runs_on_caller_and_cancellation_retains_execution_not_delivery() -> None:
    event = Event()
    caller = get_ident()
    threads = []

    def invoke(name: str, arguments: dict) -> dict:
        threads.append(get_ident())
        event.set()
        turn.revoked.set()  # Watcher may revoke during an in-progress callback.
        return {"saved": True}

    turn = make_turn(SimpleNamespace(invoke=invoke), event)
    sent = []
    turn.send = lambda *args, **kw: sent.append(args)
    dispatch = str(uuid4())
    turn.pending[dispatch] = time.monotonic() + 1
    turn._invoke({"call_id": dispatch, "name": "save", "arguments": {}})
    assert threads == [caller]
    assert sent == []
    assert turn.calls[0].executed_locally
    assert turn.calls[0].model_call_id is None
    assert turn.calls[0].outcome == "unknown"


def test_tool_callback_exception_retains_possible_side_effect() -> None:
    def invoke(*args: object) -> None:
        raise RuntimeError("SECRET")

    turn = make_turn(SimpleNamespace(invoke=invoke))
    dispatch = str(uuid4())
    turn.pending[dispatch] = time.monotonic() + 1
    with pytest.raises(ServiceClientError) as error:
        turn._invoke({"call_id": dispatch, "name": "save", "arguments": {}})
    assert error.value.code == "CALLBACK_FAILED"
    assert "SECRET" not in str(error.value)
    assert turn.calls[0].executed_locally


def test_factory_ownership_is_explicit_and_spawn_has_no_download_flag(monkeypatch) -> None:
    captured = []
    monkeypatch.setattr(ServiceClient, "__init__", lambda self, **kw: captured.append(kw))
    ServiceClient.start_owned(profile="text")
    ServiceClient.attach("http://127.0.0.1:1234", token="token")
    assert "url" not in captured[0]
    assert captured[1]["url"] == "http://127.0.0.1:1234"
    import inspect
    assert "download" not in inspect.getsource(ServiceClient._spawn)


@pytest.mark.parametrize("failure", [False, True])
def test_turn_text_callbacks_are_caller_thread_and_fail_consistently(monkeypatch, failure) -> None:
    import orchestrator.client as module

    caller = get_ident()
    captured = []
    turn = make_turn(None)
    turn.client._url = "http://127.0.0.1:1234"
    turn.client._token = "test-token"
    turn.client._instance = str(uuid4())
    turn.client._thread = lambda *args: SimpleNamespace(join=lambda **kw: None)

    class Socket:
        def send(self, data: str) -> None:
            pass

        def close(self) -> None:
            pass

    monkeypatch.setattr(module, "connect", lambda *args, **kw: Socket())
    turn.hello.set()
    final = snapshot()
    final["job_id"], final["turn_id"] = turn.job, turn.turn
    turn.terminal = final
    turn.queue.put(("text.delta", {"text": "delta"}))
    submitted = []
    turn.client._submit = lambda spec, **kw: submitted.append(spec) or final

    def callback(text: str) -> None:
        captured.append((get_ident(), text))
        if failure:
            raise ValueError("SECRET")

    turn.on_text = callback
    if failure:
        with pytest.raises(ServiceClientError) as error:
            turn.run([], 512, 0.7)
        assert error.value.code == "CALLBACK_FAILED"
        assert error.value.phase == "callback"
        assert "SECRET" not in str(error.value)
    else:
        result = turn.run([], 512, 0.7)
        assert result.text == "answer"  # final snapshot, not concatenated deltas
        assert result.state == "succeeded"
    assert captured == [(caller, "delta")]
    assert submitted[0]["kind"] == "text"


def test_arbitrary_stop_predicate_is_not_accepted() -> None:
    with pytest.raises(ServiceClientError, match="threading.Event"):
        bare_client().chat([], cancellation=lambda: False)


def test_declarations_exception_is_sanitized_before_session_creation() -> None:
    def specs():
        raise RuntimeError("SECRET")

    with pytest.raises(ServiceClientError) as error:
        bare_client().stream_chat_with_tools([], SimpleNamespace(specs=specs))
    assert error.value.code == "CALLBACK_FAILED"
    assert "SECRET" not in str(error.value)