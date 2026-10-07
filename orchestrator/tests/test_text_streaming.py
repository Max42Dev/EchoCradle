"""Tests for :meth:`TextHost.stream_chat_with_tools`.

Scripted chunks test tool/text accumulation; local socket pairs exercise real
HTTPResponse reads and cancellation cleanup without a model or a server.
"""

from __future__ import annotations

import http.client
import json
import socket
import threading
import time
import types
from collections.abc import Iterator
from unittest.mock import patch

import pytest

from orchestrator.hosts.base import HostError
from orchestrator.hosts.text import TextHost
from orchestrator.tools import ToolRegistry, ToolSpec


class _EchoTool:
    """A minimal Tool: echoes its ``value`` argument."""

    spec = ToolSpec(
        name="echo",
        description="echo a value",
        parameters={
            "type": "object",
            "properties": {"value": {"type": "string"}},
        },
    )

    def invoke(self, arguments: dict) -> dict:
        return {"echoed": arguments.get("value", "")}


def _chunk(*, content: str | None = None, tool_calls: list[dict] | None = None) -> dict:
    delta: dict = {}
    if content is not None:
        delta["content"] = content
    if tool_calls is not None:
        delta["tool_calls"] = tool_calls
    return {"choices": [{"delta": delta}]}


def _host_with_rounds(rounds: list[list[dict]]) -> TextHost:
    """A host whose SSE stream yields each round's chunks in turn."""
    host = TextHost(base_url="http://fake")
    # A stub model so _payload can name it; no server is ever contacted.
    host._model = types.SimpleNamespace(
        descriptor=types.SimpleNamespace(id="fake", params={})
    )
    queue = list(rounds)

    def _sse_chunks(_payload: dict, should_stop=None) -> Iterator[dict]:
        yield from queue.pop(0)

    host._sse_chunks = _sse_chunks  # type: ignore[method-assign]
    return host


def _registry() -> ToolRegistry:
    registry = ToolRegistry()
    registry.register(_EchoTool())
    return registry


def test_horizon_template_options_apply_without_mutating_catalog() -> None:
    from orchestrator.catalog import Catalog

    model = Catalog.default().get("k2-horizon-7b-q4km")
    host = TextHost(base_url="http://fake")
    host._model = types.SimpleNamespace(descriptor=model)
    payload = host._payload([], max_tokens=32, temperature=0, stream=True)
    assert payload["chat_template_kwargs"] == {
        "enable_thinking": False,
        "tool_presentation_format": "json",
        "tool_call_format": "json",
    }
    assert "enable_thinking" not in model.params["chat_template_kwargs"]
    assert payload["stream"] is True


def test_streams_text_and_reports_each_delta():
    host = _host_with_rounds([[_chunk(content="Hello "), _chunk(content="world")]])
    seen: list[str] = []

    text, calls = host.stream_chat_with_tools(
        [{"role": "user", "content": "hi"}],
        _registry(),
        on_text=seen.append,
    )

    assert text == "Hello world"
    assert seen == ["Hello ", "world"]
    assert calls == []


def test_accumulates_tool_call_arguments_across_deltas():
    # llama.cpp streams function.arguments as a partial string, so the pieces
    # must be concatenated before the call is executed.
    rounds = [
        [
            _chunk(
                tool_calls=[
                    {
                        "index": 0,
                        "id": "call_1",
                        "function": {"name": "echo", "arguments": '{"val'},
                    }
                ]
            ),
            _chunk(
                tool_calls=[
                    {"index": 0, "function": {"arguments": 'ue": "hi"}'}}
                ]
            ),
        ],
        [_chunk(content="Done.")],
    ]
    host = _host_with_rounds(rounds)

    text, calls = host.stream_chat_with_tools(
        [{"role": "user", "content": "hi"}], _registry()
    )

    assert text == "Done."
    assert len(calls) == 1
    assert calls[0].name == "echo"
    assert calls[0].arguments == {"value": "hi"}
    assert calls[0].result == {"echoed": "hi"}


def test_should_stop_abandons_the_stream_and_keeps_text_so_far():
    host = _host_with_rounds(
        [[_chunk(content="First. "), _chunk(content="Second. "), _chunk(content="Third.")]]
    )
    seen: list[str] = []

    def should_stop() -> bool:
        return len(seen) >= 1

    text, _calls = host.stream_chat_with_tools(
        [{"role": "user", "content": "hi"}],
        _registry(),
        on_text=seen.append,
        should_stop=should_stop,
    )

    # Only the first delta was consumed before the stop was honoured.
    assert text == "First. "
    assert seen == ["First. "]


def _tool_chunk(count: int = 1) -> dict:
    return _chunk(tool_calls=[
        {
            "index": index,
            "id": f"call_{index}",
            "function": {"name": "echo", "arguments": '{"value": "hi"}'},
        }
        for index in range(count)
    ])


def test_stop_before_request_does_not_open_stream() -> None:
    host = _host_with_rounds([])
    with patch.object(host, "_sse_chunks", side_effect=AssertionError("request")):
        assert host.stream_chat_with_tools([], _registry(), should_stop=lambda: True) == ("", [])


def test_stop_at_eof_skips_pending_tool_calls() -> None:
    host = _host_with_rounds([])
    stop = threading.Event()

    def chunks(_payload: dict, should_stop=None) -> Iterator[dict]:
        yield _chunk(
            content="Partial", tool_calls=_tool_chunk()["choices"][0]["delta"]["tool_calls"]
        )
        stop.set()

    host._sse_chunks = chunks
    registry = _registry()
    with patch.object(registry, "invoke", side_effect=AssertionError("tool invoked")):
        assert host.stream_chat_with_tools([], registry, should_stop=stop.is_set) == ("Partial", [])


@pytest.mark.parametrize("max_rounds", [1, 2])
def test_stop_during_tool_skips_remaining_tools_and_followup(max_rounds: int) -> None:
    host = _host_with_rounds([[_chunk(content="Working"), _tool_chunk(2)]])
    registry = _registry()
    stop = threading.Event()

    def invoke(_name: str, _arguments: dict) -> dict:
        stop.set()
        return {"done": True}

    with patch.object(registry, "invoke", side_effect=invoke) as invoked:
        text, calls = host.stream_chat_with_tools(
            [], registry, should_stop=stop.is_set, max_rounds=max_rounds
        )
    assert text == "Working"
    assert len(calls) == 1
    assert calls[0].result == {"done": True}
    assert invoked.call_count == 1


@pytest.mark.parametrize("max_rounds", [0, 1])
@pytest.mark.parametrize("error", [
    OSError("closed"), HostError("closed"),
    http.client.IncompleteRead(b"partial"), ValueError("closed"),
])
def test_cancellation_exception_returns_partial_text(error: Exception, max_rounds: int) -> None:
    host = _host_with_rounds([])
    stop = threading.Event()
    closed = threading.Event()

    def chunks(_payload: dict, should_stop=None) -> Iterator[dict]:
        try:
            yield _chunk(content="Partial")
            stop.set()
            raise error
        finally:
            closed.set()

    host._sse_chunks = chunks
    assert host.stream_chat_with_tools(
        [], _registry(), should_stop=stop.is_set, max_rounds=max_rounds
    ) == ("Partial", [])
    assert closed.is_set()


def test_stream_is_explicitly_closed_when_callback_stops() -> None:
    host = _host_with_rounds([])
    stop = threading.Event()
    closed = threading.Event()

    def chunks(_payload: dict, should_stop=None) -> Iterator[dict]:
        try:
            yield _chunk(content="Partial")
            yield _chunk(content="Ignored")
        finally:
            closed.set()

    host._sse_chunks = chunks
    assert host.stream_chat_with_tools(
        [], _registry(), should_stop=stop.is_set, on_text=lambda _: stop.set()
    ) == ("Partial", [])
    assert closed.is_set()


@pytest.mark.parametrize("max_rounds", [0, 1])
def test_non_cancelled_transport_failure_is_not_hidden(max_rounds: int) -> None:
    host = _host_with_rounds([])

    def chunks(_payload: dict, should_stop=None) -> Iterator[dict]:
        yield _chunk(content="Partial")
        raise HostError("transport failed")

    host._sse_chunks = chunks
    with pytest.raises(HostError, match="transport failed"):
        host.stream_chat_with_tools(
            [], _registry(), should_stop=lambda: False, max_rounds=max_rounds
        )


@pytest.mark.parametrize("max_rounds", [0, 1])
@pytest.mark.parametrize("chunked", [False, True])
@pytest.mark.parametrize("partial", [False, True])
def test_cancellation_interrupts_stalled_http_read(
    max_rounds: int, chunked: bool, partial: bool
) -> None:
    # A real CPython HTTPResponse over a local socket pair exercises buffered
    # readline and chunked IncompleteRead; no server/model or network is used.
    reader, writer = socket.socketpair()
    reader.settimeout(5)
    header = b"Transfer-Encoding: chunked\r\n" if chunked else b""
    writer.sendall(b"HTTP/1.1 200 OK\r\n" + header + b"\r\n")
    response = http.client.HTTPResponse(reader)
    response.begin()
    host = _host_with_rounds([])
    host._sse_chunks = types.MethodType(TextHost._sse_chunks, host)
    stop = threading.Event()
    received = threading.Event()
    watching = threading.Event()
    watchers: set[threading.Thread] = set()
    result: list[tuple] = []
    errors: list[BaseException] = []

    def should_stop() -> bool:
        thread = threading.current_thread()
        if thread.name == "text-sse-cancel":
            watchers.add(thread)
            watching.set()
        return stop.is_set()

    def run() -> None:
        try:
            result.append(host.stream_chat_with_tools(
                [], _registry(), should_stop=should_stop, max_rounds=max_rounds,
                on_text=lambda _: received.set(),
            ))
        except BaseException as exc:
            errors.append(exc)

    if partial:
        data = f"data: {json.dumps(_chunk(content='Partial'))}\n\n".encode()
        wire_data = f"{len(data):x}\r\n".encode() + data + b"\r\n" if chunked else data
        writer.sendall(wire_data)
    worker = threading.Thread(target=run, name="test-text-reader", daemon=True)
    try:
        with patch("urllib.request.urlopen", return_value=response) as opened:
            worker.start()
            assert watching.wait(1), "cancellation watcher did not start"
            if partial:
                assert received.wait(1), "initial delta was not delivered"
            started = time.monotonic()
            stop.set()
            worker.join(1)
            assert not worker.is_alive(), "cancelled read remained blocked"
            assert time.monotonic() - started < 1
            assert errors == []
            assert result == [("Partial" if partial else "", [])]
            assert opened.call_count == 1
            assert response.isclosed()
            assert watchers and all(not watcher.is_alive() for watcher in watchers)
    finally:
        stop.set()
        writer.close()
        worker.join(2)
        response.close()
        reader.close()


def test_transport_pre_cancel_does_not_open_request() -> None:
    host = _host_with_rounds([])
    with patch("urllib.request.urlopen", side_effect=AssertionError("request")):
        assert list(TextHost._sse_chunks(host, {}, should_stop=lambda: True)) == []


@pytest.mark.parametrize("ending", ["done", "eof", "error", "close"])
def test_transport_watcher_is_joined_on_every_exit(ending: str) -> None:
    reader, writer = socket.socketpair()
    reader.settimeout(2)
    data = f"data: {json.dumps(_chunk(content='Hello'))}\n\n".encode()
    if ending == "done":
        data += b"data: [DONE]\n\n"
    writer.sendall(b"HTTP/1.1 200 OK\r\n\r\n" + data)
    if ending == "eof":
        writer.shutdown(socket.SHUT_WR)
    response = http.client.HTTPResponse(reader)
    response.begin()
    host = _host_with_rounds([])
    watchers: set[threading.Thread] = set()
    watching = threading.Event()

    def should_stop() -> bool:
        thread = threading.current_thread()
        if thread.name == "text-sse-cancel":
            watchers.add(thread)
            watching.set()
        return False

    try:
        with patch("urllib.request.urlopen", return_value=response):
            stream = TextHost._sse_chunks(host, {}, should_stop=should_stop)
            assert next(stream) == _chunk(content="Hello")
            assert watching.wait(1)
            if ending == "error":
                with patch.object(response, "readline", side_effect=OSError("read failed")):
                    with pytest.raises(HostError, match="read failed"):
                        list(stream)
            elif ending == "close":
                stream.close()
            else:
                assert list(stream) == []
        assert response.isclosed()
        assert all(not watcher.is_alive() for watcher in watchers)
    finally:
        writer.close()
        response.close()
        reader.close()
