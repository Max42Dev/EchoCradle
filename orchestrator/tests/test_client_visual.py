"""Offline visual client contracts: mock HTTP only, no models or listeners."""

import asyncio
import hashlib
import io
import json
import struct
import threading
import time
import zlib
from dataclasses import FrozenInstanceError, replace
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import httpx
import pytest

from orchestrator import VisualResult
from orchestrator.client import ServiceClient, ServiceClientError
from orchestrator.service_protocol import PROTOCOL_VERSION


def png() -> bytes:
    def chunk(kind: bytes, data: bytes) -> bytes:
        return (struct.pack(">I", len(data)) + kind + data
                + struct.pack(">I", zlib.crc32(kind + data)))

    return (b"\x89PNG\r\n\x1a\n"
            + chunk(b"IHDR", struct.pack(">IIBBBBB", 1, 1, 8, 6, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(b"\x00\xff\x00\x00\xff"))
            + chunk(b"IEND", b""))


def glb() -> bytes:
    data = json.dumps({"asset": {"version": "2.0"}, "scene": 0,
                       "scenes": [{"nodes": [0]}], "nodes": [{}]}).encode()
    data += b" " * (-len(data) % 4)
    return struct.pack("<4sIIII", b"glTF", 2, 20 + len(data), len(data), 0x4E4F534A) + data


class VisualService:
    def __init__(self, kind: str = "image") -> None:
        self.kind = kind
        self.instance = str(uuid4())
        self.job_id = str(uuid4())
        self.input_id = str(uuid4())
        self.output_id = str(uuid4())
        self.payload = png() if kind == "image" else glb()
        self.media_type = "image/png" if kind == "image" else "model/gltf-binary"
        self.result = {
            "artifact_id": self.output_id, "media_type": self.media_type,
            "byte_length": len(self.payload), "sha256": hashlib.sha256(self.payload).hexdigest(),
            "seed": 42, "experimental": True,
        }
        self.requests: list[httpx.Request] = []
        self.spec: dict = {}
        self.state = "succeeded"
        self.final_kind = kind
        self.headers: dict[str, str] = {}
        self.changed_instance = False
        self.admission_error: tuple[int, dict] | None = None
        self.delete_fails = False
        self.polled = threading.Event()
        self.cancelled = threading.Event()

    def identity(self) -> dict:
        return {"protocol_version": PROTOCOL_VERSION, "service_instance_id": self.instance}

    def snapshot(self, state: str, kind: str) -> dict:
        return {**self.identity(), "v": PROTOCOL_VERSION, "job_id": self.job_id,
                "kind": kind, "session_id": None, "turn_id": None, "state": state,
                "revision": 1, "host_pending": False, "result": self.result,
                "error": {"code": "CANCELLED", "retryable": False, "job_id": self.job_id}}

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        path = request.url.path
        if request.method == "DELETE":
            return httpx.Response(500 if self.delete_fails else 200, json={})
        if path == "/v1/artifacts":
            return httpx.Response(201, json={"artifact_id": self.input_id})
        if path == "/v1/jobs":
            self.spec = json.loads(request.content)
            if self.admission_error:
                status, body = self.admission_error
                return httpx.Response(status, json=body)
            return httpx.Response(202, json=self.snapshot("queued", self.kind))
        if path.endswith("/cancel"):
            self.cancelled.set()
            return httpx.Response(200, json=self.snapshot("cancelled", self.kind))
        if path == f"/v1/jobs/{self.job_id}":
            self.polled.set()
            return httpx.Response(200, json=self.snapshot(self.state, self.final_kind))
        if path == "/v1/health/live":
            identity = self.identity()
            if self.changed_instance:
                identity["service_instance_id"] = str(uuid4())
            return httpx.Response(200, json=identity)
        if path == f"/v1/artifacts/{self.output_id}":
            return httpx.Response(200, content=self.payload, headers={
                "content-type": self.media_type,
                "x-content-sha256": self.result["sha256"], **self.headers,
            })
        raise AssertionError(f"Unexpected request: {request.method} {path}")


@pytest.fixture
def make_client():
    clients = []

    def create(service: VisualService) -> ServiceClient:
        client = object.__new__(ServiceClient)
        client._closing = threading.Event()
        client._lock = threading.RLock()
        client._jobs = set()
        client._sockets = set()
        client._instance = service.instance
        client._http = httpx.Client(base_url="http://127.0.0.1",
                                    transport=httpx.MockTransport(service))
        clients.append(client)
        return client

    yield create
    for client in clients:
        client._http.close()


def invoke(client: ServiceClient, kind: str, **kwargs) -> VisualResult:
    if kind == "image":
        return asyncio.run(client.generate_image("a knight", seed=42, **kwargs))
    if kind == "image_to_3d":
        return asyncio.run(client.generate_3d(png(), seed=42, **kwargs))
    return asyncio.run(client.animate_3d(glb(), seed=42, **kwargs))


@pytest.mark.parametrize("kind", ["image", "image_to_3d", "animate_3d"])
def test_visual_helpers_routes_results_and_reusable_outputs(make_client, kind: str) -> None:
    service = VisualService(kind)
    client = make_client(service)
    result = invoke(client, kind)
    assert isinstance(result, VisualResult)
    assert result.artifact_id == service.output_id
    assert result.payload == service.payload
    assert result.media_type == service.media_type
    assert result.byte_length == len(result.payload)
    assert result.sha256 == hashlib.sha256(result.payload).hexdigest()
    assert result.seed == 42 and result.experimental is True
    assert service.spec["kind"] == kind
    assert service.spec["seed"] == 42
    assert service.spec["deadline_ms"] == 600_000
    assert service.spec["idempotency_key"]
    assert not client._jobs
    uploads = [r for r in service.requests if r.url.path == "/v1/artifacts"]
    if kind == "image":
        assert not uploads
        assert service.spec["text"] == "a knight"
        assert service.spec["negative_prompt"] == ""
    else:
        assert len(uploads) == 1
        expected = "image/png" if kind == "image_to_3d" else "model/gltf-binary"
        assert uploads[0].headers["content-type"] == expected
        assert "x-sample-rate" not in uploads[0].headers
        assert not uploads[0].url.query
        assert service.spec["input_artifact_id"] == service.input_id
    assert not any(r.method == "DELETE" and r.url.path.endswith(service.output_id)
                   for r in service.requests)
    with pytest.raises(FrozenInstanceError):
        result.seed = 7


def test_visual_result_detaches_mutable_payload() -> None:
    source = bytearray(b"data")
    result = VisualResult(str(uuid4()), "image/png", 4, "digest", 42, True, source)
    source[0] = 0
    assert result.payload == b"data"


def test_deadline_not_capped_at_120_and_shared_across_requests(make_client, monkeypatch) -> None:
    service = VisualService()
    client = make_client(service)
    original = client._response
    deadlines = []

    def response(*args, **kwargs):
        deadlines.append(kwargs["deadline"])
        return original(*args, **kwargs)

    monkeypatch.setattr(client, "_response", response)
    start = time.monotonic()
    invoke(client, "image", negative_prompt="blur", deadline_ms=3_600_000)
    assert service.spec["negative_prompt"] == "blur"
    assert service.spec["deadline_ms"] == 3_600_000
    assert len(set(deadlines)) == 1
    assert 3599 < deadlines[0] - start <= 3601


@pytest.mark.parametrize("changes", [
    {"byte_length": True}, {"byte_length": 0}, {"byte_length": 32 * 1024 * 1024 + 1},
    {"byte_length": 1}, {"sha256": "x" * 64}, {"sha256": "0" * 64},
    {"seed": True}, {"seed": 43}, {"experimental": 1}, {"experimental": False},
    {"media_type": "model/gltf-binary"}, {"artifact_id": "../bad"},
])
def test_invalid_descriptors_fail_closed(make_client, changes: dict) -> None:
    service = VisualService()
    service.result.update(changes)
    with pytest.raises(ServiceClientError):
        invoke(make_client(service), "image")


@pytest.mark.parametrize("headers", [
    {"content-type": "application/octet-stream"}, {"x-content-sha256": "0" * 64},
])
def test_download_headers_are_verified(make_client, headers: dict) -> None:
    service = VisualService()
    service.headers = headers
    with pytest.raises(ServiceClientError, match="ARTIFACT_INVALID"):
        invoke(make_client(service), "image")


def test_short_download_is_rejected(make_client) -> None:
    service = VisualService()
    service.payload = service.payload[:-1]
    with pytest.raises(ServiceClientError, match="ARTIFACT_INVALID"):
        invoke(make_client(service), "image")


@pytest.mark.parametrize("kind", ["image", "image_to_3d", "animate_3d"])
def test_hash_matching_malformed_output_is_rejected(make_client, kind: str) -> None:
    service = VisualService(kind)
    service.payload = b"not a container"
    service.result.update(byte_length=len(service.payload),
                          sha256=hashlib.sha256(service.payload).hexdigest())
    with pytest.raises(ServiceClientError, match="ARTIFACT_INVALID"):
        invoke(make_client(service), kind)


@pytest.mark.parametrize("value", [b"bad", b"", b"x" * (32 * 1024 * 1024 + 1), "not bytes"],
                         ids=["malformed", "empty", "oversized", "wrong-type"])
@pytest.mark.parametrize("kind", ["image_to_3d", "animate_3d"])
def test_invalid_input_never_uploads(make_client, value, kind: str) -> None:
    service = VisualService(kind)
    client = make_client(service)
    method = client.generate_3d if kind == "image_to_3d" else client.animate_3d
    with pytest.raises(ServiceClientError, match="ARTIFACT_INVALID"):
        asyncio.run(method(value, seed=42))
    assert service.requests == []


@pytest.mark.parametrize("kwargs", [{"seed": True}, {"seed": -1}, {"seed": 2**32},
                                    {"deadline_ms": 999}, {"deadline_ms": 3_600_001},
                                    {"deadline_ms": True}, {"negative_prompt": 3}])
def test_invalid_parameters_never_submit(make_client, kwargs: dict) -> None:
    service = VisualService()
    with pytest.raises(ServiceClientError, match="INVALID_SPEC"):
        asyncio.run(make_client(service).generate_image("a knight", **{"seed": 42, **kwargs}))
    assert service.requests == []


def test_wrong_job_kind_and_changed_instance_are_rejected(make_client) -> None:
    service = VisualService()
    service.final_kind = "tts"
    with pytest.raises(ServiceClientError, match="PROTOCOL_ERROR"):
        invoke(make_client(service), "image")
    service = VisualService()
    service.changed_instance = True
    with pytest.raises(ServiceClientError, match="INSTANCE_CHANGED"):
        invoke(make_client(service), "image")


@pytest.mark.parametrize("status,body,cleanup", [
    (400, {"error": {"code": "INVALID_SPEC", "retryable": False}}, True),
    (503, {"error": {"code": "NOT_READY", "retryable": True}}, True),
    (500, {"error": {"code": "INTERNAL_ERROR", "retryable": False}}, False),
    (503, {"detail": "NOT_READY"}, False),
    (503, {"error": {"code": "NOT_READY", "retryable": 1}}, False),
])
def test_upload_cleanup_only_for_definite_rejection(make_client, status, body, cleanup) -> None:
    service = VisualService("image_to_3d")
    service.admission_error = (status, body)
    with pytest.raises(ServiceClientError):
        invoke(make_client(service), "image_to_3d")
    assert any(r.method == "DELETE" for r in service.requests) is cleanup


def test_cleanup_failure_does_not_hide_verified_result(make_client) -> None:
    service = VisualService("image_to_3d")
    service.delete_fails = True
    assert invoke(make_client(service), "image_to_3d").payload == service.payload


def test_async_cancellation_cancels_admitted_job(make_client) -> None:
    service = VisualService()
    service.state = "running"
    client = make_client(service)

    async def run() -> None:
        task = asyncio.create_task(client.generate_image("a knight", seed=42))
        assert await asyncio.to_thread(service.polled.wait, 2)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert await asyncio.to_thread(service.cancelled.wait, 2)

    asyncio.run(run())
    assert not client._jobs


def test_server_cancellation_never_exposes_result(make_client) -> None:
    service = VisualService()
    service.state = "cancelled"
    with pytest.raises(ServiceClientError, match="CANCELLED"):
        invoke(make_client(service), "image")
    assert not any(r.url.path.endswith(service.output_id) for r in service.requests)


def test_host_pending_releases_upload_before_polling(make_client, monkeypatch) -> None:
    service = VisualService("image_to_3d")
    original = service.snapshot

    def snapshot(state: str, kind: str) -> dict:
        if state == "succeeded":
            assert any(r.method == "DELETE" and r.url.path.endswith(service.input_id)
                       for r in service.requests)
        return {**original(state, kind), "host_pending": True}

    monkeypatch.setattr(service, "snapshot", snapshot)
    assert invoke(make_client(service), "image_to_3d").payload == service.payload
    assert sum(r.method == "DELETE" for r in service.requests) == 1


def test_cancellation_during_admission_still_cancels_accepted_job(make_client, monkeypatch) -> None:
    service = VisualService("image_to_3d")
    service.state = "running"
    client = make_client(service)
    entered, release, finished = threading.Event(), threading.Event(), threading.Event()
    original_request = client._request
    original_worker = client._visual_job

    def request(method, path, **kwargs):
        if path == "/v1/jobs":
            entered.set()
            assert release.wait(2)
        return original_request(method, path, **kwargs)

    def worker(*args):
        try:
            return original_worker(*args)
        finally:
            finished.set()

    monkeypatch.setattr(client, "_request", request)
    monkeypatch.setattr(client, "_visual_job", worker)

    async def run() -> None:
        task = asyncio.create_task(client.generate_3d(png(), seed=42))
        try:
            assert await asyncio.to_thread(entered.wait, 2)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
        finally:
            release.set()
        assert await asyncio.to_thread(finished.wait, 2)

    asyncio.run(run())
    assert service.cancelled.is_set()
    assert not client._jobs
    assert sum(r.method == "DELETE" and r.url.path.endswith(service.input_id)
               for r in service.requests) == 1


def retained(kind: str) -> VisualResult:
    payload = png() if kind == "image_to_3d" else glb()
    media_type = "image/png" if kind == "image_to_3d" else "model/gltf-binary"
    return VisualResult(str(uuid4()), media_type, len(payload),
                        hashlib.sha256(payload).hexdigest(), 7, True, payload)


@pytest.mark.parametrize("kind", ["image_to_3d", "animate_3d"])
def test_retained_input_reuses_id_without_upload_or_delete(make_client, kind: str) -> None:
    service = VisualService(kind)
    client = make_client(service)
    source = retained(kind)
    method = client.generate_3d if kind == "image_to_3d" else client.animate_3d
    assert asyncio.run(method(source, seed=42)).payload == service.payload
    assert source.seed != service.spec["seed"]
    assert service.spec["input_artifact_id"] == source.artifact_id
    assert not any(r.url.path == "/v1/artifacts" or r.method == "DELETE"
                   for r in service.requests)
    assert set(service.spec) == {"kind", "seed", "deadline_ms", "input_artifact_id",
                                 "idempotency_key"}


@pytest.mark.parametrize("kind", ["image_to_3d", "animate_3d"])
@pytest.mark.parametrize("changes", [
    {"artifact_id": "../bad"}, {"media_type": "application/octet-stream"},
    {"byte_length": True}, {"byte_length": 0}, {"byte_length": 1},
    {"sha256": "0" * 64}, {"sha256": None}, {"seed": True}, {"seed": -1},
    {"seed": 2**32}, {"experimental": 1}, {"experimental": False},
    {"payload": b"bad", "byte_length": 3, "sha256": hashlib.sha256(b"bad").hexdigest()},
])
def test_invalid_retained_input_never_requests(make_client, kind: str, changes: dict) -> None:
    service = VisualService(kind)
    client = make_client(service)
    method = client.generate_3d if kind == "image_to_3d" else client.animate_3d
    with pytest.raises(ServiceClientError):
        asyncio.run(method(replace(retained(kind), **changes), seed=42))
    assert not service.requests


@pytest.mark.parametrize("kind", ["image_to_3d", "animate_3d"])
def test_retained_input_rejection_never_deletes_source(make_client, kind: str) -> None:
    service = VisualService(kind)
    service.admission_error = (404, {"error": {"code": "ARTIFACT_NOT_FOUND",
                                              "retryable": False}})
    client = make_client(service)
    method = client.generate_3d if kind == "image_to_3d" else client.animate_3d
    with pytest.raises(ServiceClientError, match="ARTIFACT_NOT_FOUND"):
        asyncio.run(method(retained(kind), seed=42))
    assert not any(r.method == "DELETE" for r in service.requests)


def test_upload_released_even_when_polling_fails(make_client, monkeypatch) -> None:
    service = VisualService("image_to_3d")
    client = make_client(service)

    def poll(*args, **kwargs):
        assert any(r.method == "DELETE" and r.url.path.endswith(service.input_id)
                   for r in service.requests)
        raise ServiceClientError("HTTP_ERROR", "Polling failed.")

    monkeypatch.setattr(client, "_poll", poll)
    with pytest.raises(ServiceClientError, match="HTTP_ERROR"):
        invoke(client, "image_to_3d")


@pytest.mark.parametrize("factory", [ServiceClient, ServiceClient.start_owned])
def test_owned_visual_config_reaches_spawn(monkeypatch, tmp_path, factory) -> None:
    config = tmp_path / "visual config.json"
    captured = []

    def spawn(self, *args):
        captured.append(args)
        return {"protocol_version": PROTOCOL_VERSION, "port": 1234,
                "service_instance_id": str(uuid4())}

    monkeypatch.setattr(ServiceClient, "_spawn", spawn)
    monkeypatch.setattr(ServiceClient, "_wait_ready", lambda self: None)
    monkeypatch.setattr(ServiceClient, "capabilities", lambda self: {})
    monkeypatch.setattr(ServiceClient, "_request", lambda *args, **kwargs: {})
    with factory(profile="text", visual_config=config):
        assert captured == [("text", None, None, None, None, False, config)]


def test_attached_client_rejects_visual_config_before_requests(monkeypatch, tmp_path) -> None:
    def unexpected(*args, **kwargs):
        pytest.fail("Attached visual config must be rejected before startup or HTTP.")

    monkeypatch.setattr(ServiceClient, "_spawn", unexpected)
    monkeypatch.setattr(httpx, "Client", unexpected)
    with pytest.raises(ServiceClientError, match="INVALID_CONFIG"):
        ServiceClient(url="http://127.0.0.1:1234", token="token",
                      visual_config=tmp_path / "visual.json")


@pytest.mark.parametrize("config", [None, Path("visual config.json")])
def test_spawn_visual_config_command_is_server_owned(monkeypatch, config) -> None:
    import orchestrator.client as module

    commands = []
    bootstrap = {"protocol_version": PROTOCOL_VERSION, "port": 1234,
                 "service_instance_id": str(uuid4())}

    def popen(command, **kwargs):
        commands.append(command)
        return SimpleNamespace(stdout=io.BytesIO(json.dumps(bootstrap).encode() + b"\n"))

    monkeypatch.setattr(module.subprocess, "Popen", popen)
    monkeypatch.setattr(module, "_WindowsJob", lambda: SimpleNamespace(
        assign_and_resume=lambda process: None))
    client = object.__new__(ServiceClient)
    client._token = "token"
    client._windows_job = None
    client._lock = threading.RLock()
    client._closing = threading.Event()
    client._threads = set()
    assert client._spawn("text", None, None, None, None, False, config) == bootstrap
    expected = [module.sys.executable, "-m", "orchestrator.service", "--profile", "text"]
    if config is not None:
        expected.extend(["--visual-config", str(config.resolve())])
    assert commands == [expected]


@pytest.mark.parametrize("method", ["generate_3d", "animate_3d"])
def test_visual_jobs_reject_arbitrary_paths(make_client, tmp_path, method: str) -> None:
    service = VisualService()
    client = make_client(service)
    with pytest.raises(TypeError):
        asyncio.run(getattr(client, method)(b"data", seed=42,
                                           visual_config=tmp_path / "visual.json"))
    assert not service.requests