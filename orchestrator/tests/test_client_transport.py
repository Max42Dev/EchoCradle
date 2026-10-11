"""Fault injection with HTTPX mock transport; no listeners or native models."""

import hashlib
import threading
import time
from uuid import uuid4

import httpx
import numpy as np
import pytest

from orchestrator.client import ServiceClient, ServiceClientError, _Turn
from orchestrator.service_protocol import PROTOCOL_VERSION


def client_for(handler) -> ServiceClient:
    client = object.__new__(ServiceClient)
    client._closing = threading.Event()
    client._lock = threading.RLock()
    client._jobs = set()
    client._sockets = set()
    client._instance = str(uuid4())
    client._http = httpx.Client(base_url="http://127.0.0.1", transport=httpx.MockTransport(handler))
    return client


def test_nonjson_http_error_preserves_status_not_response_secrets() -> None:
    client = client_for(lambda request: httpx.Response(502, text="SECRET"))
    with pytest.raises(ServiceClientError) as error:
        client._request("GET", "/v1/jobs")
    assert error.value.code == "HTTP_ERROR"
    assert error.value.status == 502
    assert error.value.retryable
    assert "SECRET" not in str(error.value)
    client._http.close()


def test_validated_wire_http_error_preserves_correlation() -> None:
    identifier = str(uuid4())
    client = client_for(lambda request: httpx.Response(429, json={"error": {
        "code": "RESOURCE_BUSY", "retryable": True, "job_id": identifier, "message": "SECRET",
    }}))
    with pytest.raises(ServiceClientError) as error:
        client._request("POST", "/v1/jobs")
    assert error.value.code == "RESOURCE_BUSY"
    assert error.value.job_id == identifier
    assert error.value.status == 429
    assert error.value.phase == "request"
    client._http.close()


@pytest.mark.parametrize("body", [{"error": {"code": "X", "retryable": 1}}, {"bad": True}])
def test_framework_and_malformed_error_fallbacks(body: dict) -> None:
    client = client_for(lambda request: httpx.Response(404, json=body))
    with pytest.raises(ServiceClientError) as error:
        client._request("GET", "/missing")
    assert error.value.code == ("PROTOCOL_ERROR" if "error" in body else "HTTP_ERROR")
    client._http.close()


def test_total_budget_bounds_blocked_headers() -> None:
    release = threading.Event()
    finished = threading.Event()

    def handler(request):
        release.wait(1)
        finished.set()
        return httpx.Response(200, json={})

    client = client_for(handler)
    start = time.monotonic()
    try:
        with pytest.raises(ServiceClientError, match="DEADLINE_EXCEEDED"):
            client._request("GET", "/stall", deadline=start + 0.03)
        assert time.monotonic() - start < 0.3
    finally:
        release.set()
        assert finished.wait(1)
        client._http.close()


def test_total_budget_bounds_blocked_body_and_passes_remaining_timeout() -> None:
    release = threading.Event()
    finished = threading.Event()
    timeouts = []

    class Body(httpx.SyncByteStream):
        def __iter__(self):
            yield b'{"a":'
            release.wait(1)
            finished.set()
            yield b'1}'

    def handler(request):
        timeouts.append(request.extensions["timeout"])
        return httpx.Response(200, stream=Body())

    client = client_for(handler)
    start = time.monotonic()
    try:
        with pytest.raises(ServiceClientError, match="DEADLINE_EXCEEDED"):
            client._request("GET", "/stall", deadline=start + 0.03)
        assert time.monotonic() - start < 0.3
        assert 0 < timeouts[0]["read"] <= 0.03
    finally:
        release.set()
        assert finished.wait(1)
        client._http.close()


def test_response_byte_limit_and_transport_sanitization() -> None:
    client = client_for(lambda request: httpx.Response(200, content=b"12345"))
    with pytest.raises(ServiceClientError, match="OUTPUT_LIMIT"):
        client._response("GET", "/large", limit=4)
    client._http.close()

    def fail(request):
        raise httpx.ConnectError("SECRET")

    client = client_for(fail)
    with pytest.raises(ServiceClientError) as error:
        client._request("GET", "/lost")
    assert error.value.code == "TRANSPORT_ERROR"
    assert error.value.retryable
    assert "SECRET" not in str(error.value)
    client._http.close()


def test_cancel_uses_one_canonical_response_no_followup_get() -> None:
    requests = []
    identifier = str(uuid4())
    client = client_for(lambda request: requests.append(request.method) or httpx.Response(
        200, json={"v": PROTOCOL_VERSION, "protocol_version": PROTOCOL_VERSION,
                   "service_instance_id": client._instance, "job_id": identifier,
                   "state": "cancelled", "revision": 1, "host_pending": True},
    ))
    assert client._cancel(identifier)["state"] == "cancelled"
    assert requests == ["POST"]
    client._http.close()


def test_primary_poll_deadline_survives_failed_cancel() -> None:
    client = client_for(lambda request: httpx.Response(500))
    identifier = str(uuid4())
    client._jobs.add(identifier)
    with pytest.raises(ServiceClientError) as error:
        client._poll({"job_id": identifier, "state": "running"}, deadline=time.monotonic() - 1)
    assert error.value.code == "DEADLINE_EXCEEDED"
    assert identifier not in client._jobs
    client._http.close()


def test_cancel_event_returns_canonical_terminal_not_success() -> None:
    client = client_for(lambda request: httpx.Response(500))
    identifier = str(uuid4())
    event = threading.Event()
    event.set()
    client._cancel = lambda *args, **kwargs: {"job_id": identifier, "state": "cancelled"}
    assert client._poll({"job_id": identifier, "state": "running"},
                        cancellation=event, snapshot=True)["state"] == "cancelled"
    client._http.close()


def test_watcher_keeps_deadline_when_cancel_transport_fails() -> None:
    client = client_for(lambda request: httpx.Response(500))
    turn = _Turn(client, str(uuid4()), str(uuid4()), None, [], None, None,
                 time.monotonic() - 1)
    turn.job = str(uuid4())
    turn.job_ready.set()
    turn.watch_cancel()
    assert turn.error.code == "DEADLINE_EXCEEDED"
    assert turn.revoked.is_set()
    client._http.close()


@pytest.mark.parametrize("code,status,phase,definite", [
    ("RESOURCE_BUSY", 409, "request", True),
    ("NOT_READY", 503, "request", True),
    ("INVALID_SPEC", None, "validation", True),
    ("TRANSPORT_ERROR", None, "transport", False),
    ("INTERNAL_ERROR", 500, "request", False),
    ("INTERNAL_ERROR", 503, "request", False),
    ("HTTP_ERROR", 503, "request", False),
    ("PROTOCOL_ERROR", 503, "request", False),
    ("NOT_READY", 502, "request", False),
])
def test_stt_upload_cleanup_only_after_definite_rejection(
    code: str, status: int | None, phase: str, definite: bool,
) -> None:
    identifier = str(uuid4())
    requests = []
    client = client_for(lambda request: httpx.Response(500))
    client._request = lambda method, path, **kw: requests.append((method, path)) or {
        "artifact_id": identifier,
    }

    def submit(*args, **kwargs):
        raise ServiceClientError(
            code, "rejected", status=status, phase=phase,
        )

    client._submit = submit
    with pytest.raises(ServiceClientError):
        client.transcribe_samples(np.zeros(32))
    assert ("DELETE", f"/v1/artifacts/{identifier}") in requests if definite else (
        "DELETE", f"/v1/artifacts/{identifier}") not in requests
    client._http.close()


@pytest.mark.parametrize("body,definite", [
    ({"error": {"code": "NOT_READY", "retryable": True, "job_id": None}}, True),
    ({"error": {"code": "NOT_READY", "retryable": 1, "job_id": None}}, False),
    ({"detail": "NOT_READY"}, False),
])
def test_stt_not_ready_cleanup_requires_validated_wire_error(body: dict, definite: bool) -> None:
    identifier = str(uuid4())
    deletes = []

    def handler(request):
        if request.method == "DELETE":
            deletes.append(request.url.path)
            return httpx.Response(200, json={})
        if request.url.path == "/v1/artifacts":
            return httpx.Response(201, json={"artifact_id": identifier})
        return httpx.Response(503, json=body)

    client = client_for(handler)
    try:
        with pytest.raises(ServiceClientError):
            client.transcribe_samples(np.zeros(32))
        assert bool(deletes) is definite
    finally:
        client._http.close()


def test_verified_tts_audio_survives_delete_failure_and_shares_budget() -> None:
    identifier = str(uuid4())
    raw = b"\x01\x00\x02\x00"
    digest = hashlib.sha256(raw).hexdigest()
    deadlines = []
    client = client_for(lambda request: httpx.Response(500))
    client._submit = lambda spec, **kw: deadlines.append(kw["deadline"]) or spec
    client._poll = lambda job, **kw: deadlines.append(kw["deadline"]) or {
        "artifact_id": identifier, "sample_rate": 16000, "sample_count": 2, "sha256": digest,
    }

    def request(method, path, **kw):
        deadlines.append(kw["deadline"])
        if method == "DELETE":
            raise ServiceClientError("TRANSPORT_ERROR", "cleanup failed")
        return {}

    client._request = request
    client._response = lambda *args, **kw: deadlines.append(kw["deadline"]) or (
        raw, httpx.Headers({"x-content-sha256": digest, "x-sample-rate": "16000",
                            "x-sample-count": "2"}), 200,
    )
    audio, rate = client._synthesize_samples("hello")
    assert rate == 16000 and audio.size == 2
    assert len(set(deadlines)) == 1
    client._http.close()


def test_socket_loss_is_typed_and_revokes_pending_tools() -> None:
    client = client_for(lambda request: httpx.Response(500))
    turn = _Turn(client, str(uuid4()), str(uuid4()), None, [], None, None,
                 time.monotonic() + 1)

    class Socket:
        def recv(self, **kwargs):
            raise OSError("SECRET")

    turn.socket = Socket()
    turn.pending[str(uuid4())] = time.monotonic() + 1
    turn.receive()
    assert turn.error.code == "SOCKET_LOST"
    assert "SECRET" not in str(turn.error)
    assert turn.pending == {}
    assert turn.revoked.is_set()
    client._http.close()


def test_stalled_socket_cleanup_does_not_extend_expired_turn() -> None:
    release = threading.Event()
    closed = threading.Event()
    client = client_for(lambda request: httpx.Response(500))
    turn = _Turn(client, str(uuid4()), str(uuid4()), None, [], None, None,
                 time.monotonic() - 1)

    class Socket:
        def close(self):
            release.wait(1)
            closed.set()

    turn.socket = Socket()
    start = time.monotonic()
    try:
        turn.close()
        assert time.monotonic() - start < 0.3
    finally:
        release.set()
        assert closed.wait(1)
        client._http.close()