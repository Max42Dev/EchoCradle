"""Local, cached-only REST/JSON-WebSocket service over the real host facade.

Run with ``python -m orchestrator.service``. The inherited environment must
contain ECHOCRADLE_SERVICE_TOKEN. FastAPI, uvicorn and jsonschema are runtime
dependencies; importing this module does not probe hardware or load models.

This bounded slice intentionally has no device I/O, provisioning, conversation
history, delivery reconciliation or binary WebSocket audio. PCM uses artifacts.
Native inference is not forcibly interrupted: its lane stays reserved until
the call returns. Native crashes still affect the entire service process.
"""

from __future__ import annotations

import argparse
import asyncio
import concurrent.futures
import hashlib
import hmac
import inspect
import json
import logging
import os
import socket
import sys
import threading
import time
from contextlib import asynccontextmanager
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urlsplit
from uuid import uuid4

from orchestrator.catalog import Modality, ModelDescriptor
from orchestrator.hosts.text import TextHost
from orchestrator.paths import model_store_dir
from orchestrator.service_protocol import (
    ARTIFACT_LIMIT, JSON_LIMIT, PCM_LIMIT,
    PROTOCOL_VERSION, TERMINAL, TOOL_LIMIT, TTL_SECONDS, JobSpec, ServiceError,
    decode, encode, envelope, fields, session_spec, string, uuid_string,
)
from orchestrator.store import InstalledModel, ModelStore, ProgressFn

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class ServiceConfig:
    """Server-owned policy. None of these settings are accepted in job JSON."""

    profile: str = "text"
    shippable_only: bool = False
    text_model: str | None = None
    tts_model: str | None = None
    stt_model: str | None = None
    speaker_id: int | None = None
    port: int = 0
    store_root: Path | None = None
    allowed_origins: tuple[str, ...] = ()
    spawned_owner: bool = True

    def __post_init__(self) -> None:
        if self.profile not in ("text", "voice") or not 0 <= self.port <= 65535:
            raise ValueError("Invalid profile or port")
        if self.speaker_id is not None and self.speaker_id < 0:
            raise ValueError("speaker_id must be nonnegative")
        for origin in self.allowed_origins:
            parsed = urlsplit(origin)
            if (parsed.scheme not in ("http", "https")
                    or parsed.hostname not in ("127.0.0.1", "localhost", "::1")
                    or parsed.username or parsed.password or parsed.path
                    or parsed.query or parsed.fragment):
                raise ValueError("Development origins must be exact loopback origins")


class CachedModelStore(ModelStore):
    """Never fall through to provisioning, even on an accidental facade call."""

    def ensure(
        self, model: ModelDescriptor, *, progress: ProgressFn | None = None,
    ) -> InstalledModel:
        installed = self.installed(model)
        if installed is None:
            raise ServiceError("MODEL_NOT_PROVISIONED", "Selected model is not cached.", 503)
        return installed


class CachedTextHost(TextHost):
    """The real text host, with its implicit llama.cpp download disabled."""

    def _ensure_binary(self) -> Path:
        executable = self.binary_dir / "llama-server.exe"
        if not executable.is_file():
            executable = next(self.binary_dir.rglob("llama-server.exe"), executable)
        if not executable.is_file():
            raise ServiceError(
                "MODEL_NOT_PROVISIONED", "The local text runtime is not cached.", 503,
            )
        return executable


class StoreWriterLock:
    """OS-released exclusive advisory writer lock; never unlink the lock file."""

    def __init__(self, root: Path) -> None:
        self.root = root
        self._file: Any = None

    def acquire(self) -> None:
        if self._file is not None:
            return
        self.root.mkdir(parents=True, exist_ok=True)
        handle = (self.root / ".orchestrator-service.lock").open("a+b")
        try:
            handle.seek(0, 2)
            if handle.tell() == 0:
                handle.write(b"\0")
                handle.flush()
            handle.seek(0)
            if os.name == "nt":
                import msvcrt

                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            handle.close()
            raise ServiceError("STORE_BUSY", "Another service owns the model store.", 503) from exc
        self._file = handle

    def release(self) -> None:
        if self._file is not None:
            # Closing the descriptor releases either OS advisory lock.
            self._file.close()
            self._file = None


def _authorize(scope: dict[str, Any], token: str, config: ServiceConfig) -> None:
    headers: dict[bytes, list[bytes]] = {}
    for key, value in scope.get("headers", []):
        headers.setdefault(key.lower(), []).append(value)
    auth = headers.get(b"authorization", [])
    expected = f"Bearer {token}".encode("ascii")
    if len(auth) != 1 or not hmac.compare_digest(auth[0], expected):
        raise ServiceError("UNAUTHORIZED", "Bearer authentication required.", 401)
    hosts = headers.get(b"host", [])
    try:
        host = hosts[0].decode("ascii") if len(hosts) == 1 else ""
        parsed = urlsplit(f"http://{host}")
        actual_port = parsed.port or 80
        server = scope.get("server") or ("127.0.0.1", config.port)
        port = config.port or server[1]
        if (parsed.hostname not in ("127.0.0.1", "localhost", "::1")
                or actual_port != port or parsed.username or parsed.password
                or parsed.path or parsed.query or parsed.fragment):
            raise ValueError("host")
    except (ValueError, UnicodeError, IndexError):
        raise ServiceError("INVALID_HOST", "Host must match this loopback listener.", 403)
    origins = headers.get(b"origin", [])
    if origins and (len(origins) != 1
                    or origins[0].decode("latin1") not in config.allowed_origins):
        raise ServiceError("INVALID_ORIGIN", "Browser origin is not permitted.", 403)
    encodings = headers.get(b"content-encoding", [])
    if encodings and encodings != [b"identity"]:
        raise ServiceError("INVALID_ENCODING", "Compressed requests are not supported.", 415)


class _BoundaryMiddleware:
    """Authenticate before reading; count chunked bodies before FastAPI parses."""

    def __init__(self, app: Any, token: str, config: ServiceConfig) -> None:
        self.app, self.token, self.config = app, token, config

    async def __call__(self, scope: dict[str, Any], receive: Any, send: Any) -> None:
        if scope["type"] not in ("http", "websocket"):
            await self.app(scope, receive, send)
            return
        try:
            _authorize(scope, self.token, self.config)
            if scope["type"] == "websocket":
                await self.app(scope, receive, send)
                return
            limit = (PCM_LIMIT if scope["method"] == "POST"
                     and scope["path"] == "/v1/artifacts" else JSON_LIMIT)
            lengths = [v for k, v in scope.get("headers", []) if k.lower() == b"content-length"]
            if lengths:
                if len(lengths) != 1:
                    raise ServiceError("INVALID_SPEC", "Ambiguous content length.", 400)
                try:
                    length = int(lengths[0])
                except ValueError as exc:
                    raise ServiceError("INVALID_SPEC", "Invalid content length.", 400) from exc
                if length < 0 or length > limit:
                    raise ServiceError("PAYLOAD_TOO_LARGE", "Body exceeds its byte limit.", 413)
            # Buffer one bounded body before routing, including unrecognized routes.
            body = bytearray()
            body_deadline = asyncio.get_running_loop().time() + 5
            while True:
                remaining = body_deadline - asyncio.get_running_loop().time()
                if remaining <= 0:
                    raise asyncio.TimeoutError
                message = await asyncio.wait_for(receive(), timeout=remaining)
                if message["type"] == "http.disconnect":
                    return
                body.extend(message.get("body", b""))
                if len(body) > limit:
                    raise ServiceError("PAYLOAD_TOO_LARGE", "Body exceeds its byte limit.", 413)
                if not message.get("more_body", False):
                    break
            delivered = False

            async def bounded_receive() -> dict[str, Any]:
                nonlocal delivered
                if not delivered:
                    delivered = True
                    return {"type": "http.request", "body": bytes(body), "more_body": False}
                return await receive()

            await self.app(scope, bounded_receive, send)
        except (ServiceError, asyncio.TimeoutError) as exc:
            error = (exc if isinstance(exc, ServiceError) else
                     ServiceError("REQUEST_TIMEOUT", "Request body stalled.", 408))
            if scope["type"] == "websocket":
                # An ASGI close before accept is an HTTP 403 denial, not an upgrade.
                await send({"type": "websocket.close", "code": 1008})
            else:
                payload = encode(error.body())
                await send({"type": "http.response.start", "status": error.status,
                            "headers": [(b"content-type", b"application/json"),
                                        (b"content-length", str(len(payload)).encode())]})
                await send({"type": "http.response.body", "body": payload})


@dataclass
class _Artifact:
    pcm: bytes
    sample_rate: int
    created: float = field(default_factory=time.monotonic)
    sha256: str = ""

    def __post_init__(self) -> None:
        self.sha256 = hashlib.sha256(self.pcm).hexdigest()


@dataclass
class _Job:
    id: str
    spec: JobSpec
    request_hash: str
    deadline: float
    state: str = "queued"
    revision: int = 1
    host_pending: bool = False
    result: Any = None
    error: Any = None
    completed: float | None = None
    cancel: threading.Event = field(default_factory=threading.Event)
    input_pcm: bytes | None = None

    def should_stop(self) -> bool:
        return self.cancel.is_set() or time.monotonic() >= self.deadline

    def snapshot(self) -> dict[str, Any]:
        return {
            "job_id": self.id, "session_id": self.spec.session_id,
            "turn_id": self.spec.turn_id, "kind": self.spec.kind, "state": self.state,
            "revision": self.revision, "host_pending": self.host_pending,
            "result": self.result, "error": self.error,
        }


@dataclass
class _Session:
    id: str
    tools: list[dict[str, Any]]
    delivery_mode: str
    connection: _Connection | None = None
    disconnected_at: float = field(default_factory=time.monotonic)

    @property
    def permitted_tools(self) -> list[str]:
        return [tool["function"]["name"] for tool in self.tools]


@dataclass
class _PendingTool:
    job: _Job
    future: asyncio.Future[Any]


class _Connection:
    """One bounded writer, independent reader, connection-local sequence space."""

    def __init__(self, runtime: _Runtime, session: _Session, websocket: Any) -> None:
        self.runtime, self.session, self.websocket = runtime, session, websocket
        self.queue: asyncio.Queue[tuple[str, dict[str, Any], _Job | None, int]] = (
            asyncio.Queue(maxsize=64)
        )
        self.queued_bytes = 0
        self.next_seq = 1
        self.closed = False
        self.accepted = False
        self.pending: dict[str, _PendingTool] = {}

    async def emit(self, event: str, payload: dict[str, Any], job: _Job | None = None) -> None:
        if self.closed or not self.accepted:
            raise ServiceError("SOCKET_LOST", "The session socket is unavailable.", 409)
        size = len(encode(payload)) + 512
        deadline = self.runtime.loop.time() + 5
        while self.queued_bytes + size > 256 * 1024 or self.queue.full():
            if self.closed or self.runtime.loop.time() >= deadline:
                raise ServiceError("SLOW_CONSUMER", "Socket backpressure exceeded 5 seconds.", 409)
            await asyncio.sleep(0.01)
        if self.closed:
            raise ServiceError("SOCKET_LOST", "The session socket is unavailable.", 409)
        self.queued_bytes += size
        self.queue.put_nowait((event, payload, job, size))

    async def writer(self) -> None:
        try:
            while not self.closed:
                event, payload, job, size = await self.queue.get()
                self.queued_bytes -= size
                try:
                    if (job is not None and job.should_stop()
                            and event not in ("job.state", "error")):
                        continue
                    data = {
                        "v": PROTOCOL_VERSION, "seq": self.next_seq, "type": event,
                        "service_instance_id": self.runtime.instance_id,
                        "session_id": self.session.id,
                        "job_id": job.id if job else None,
                        "turn_id": job.spec.turn_id if job else None, "payload": payload,
                    }
                    self.next_seq += 1
                    await asyncio.wait_for(self.websocket.send_text(encode(data).decode()), 5)
                finally:
                    self.queue.task_done()
        finally:
            self.closed = True
            self.runtime.disconnect(self.session, self)
            try:
                await self.websocket.close(code=1008)
            except Exception:
                pass


class _RemoteRegistry:
    """Host-worker adapter: client declarations, client-only execution."""

    def __init__(self, runtime: _Runtime, job: _Job) -> None:
        self.runtime, self.job = runtime, job
        session = runtime.sessions.get(job.spec.session_id or "")
        self.declarations = {
            tool["function"]["name"]: tool for tool in session.tools
        } if session else {}
        self.names = tuple(self.declarations)
        self.count = 0

    def specs(self) -> list[dict[str, Any]]:
        return json.loads(encode(list(self.declarations.values())))

    def invoke(self, name: str, arguments: dict[str, Any]) -> Any:
        from jsonschema import ValidationError, validate

        if self.job.should_stop():
            raise ServiceError("CANCELLED", "Job was cancelled.", 409)
        if not isinstance(name, str) or name not in self.names:
            return {"ok": False, "error": "TOOL_NOT_PERMITTED"}
        self.count += 1
        if self.count > 8:
            raise ServiceError("TOOL_LIMIT", "At most eight tool calls are permitted.")
        try:
            encode(arguments, TOOL_LIMIT)
            validate(arguments, self.declarations[name]["function"]["parameters"])
        except (ValidationError, ServiceError):
            return {"ok": False, "error": "INVALID_TOOL_ARGUMENTS"}
        future = asyncio.run_coroutine_threadsafe(
            self.runtime.remote_tool(self.job, name, arguments), self.runtime.loop,
        )
        try:
            # The coroutine imposes a 5s remote result deadline; allow emission's
            # own bounded backpressure in this outer worker guard.
            return future.result(timeout=min(10.1, max(0.01, self.job.deadline - time.monotonic())))
        except concurrent.futures.TimeoutError as exc:
            future.cancel()
            raise ServiceError("TOOL_TIMEOUT", "Tool outcome is unknown.", 409) from exc


class _Runtime:
    def __init__(
        self, config: ServiceConfig, shutdown_callback: Callable[[], None] | None,
        store_lock: StoreWriterLock | None,
    ) -> None:
        self.config = config
        self.shutdown_callback = shutdown_callback
        self.instance_id = str(uuid4())
        self.state = "starting"
        self.startup_error: dict[str, Any] | None = None
        self.diagnostics: dict[str, Any] = {"probe": None, "planned": {}, "actual": {}}
        self.sessions: dict[str, _Session] = {}
        self.jobs: dict[str, _Job] = {}
        self.idempotency: dict[tuple[str | None, str], str] = {}
        self.artifacts: dict[str, _Artifact] = {}
        self.artifact_bytes = 0
        self.loop: asyncio.AbstractEventLoop
        self.facade: Any = None
        self.store_lock = store_lock
        self.queues = {name: asyncio.Queue(maxsize=16) for name in ("text", "tts", "stt")}
        self.executors = {
            name: concurrent.futures.ThreadPoolExecutor(max_workers=1, thread_name_prefix=name)
            for name in self.queues
        }
        self.workers: list[asyncio.Task[Any]] = []
        self.startup_task: asyncio.Task[Any] | None = None
        self.housekeeping: asyncio.Task[Any] | None = None

    async def start(self) -> None:
        self.loop = asyncio.get_running_loop()
        self.workers = [asyncio.create_task(self.lane(name)) for name in self.queues]
        self.startup_task = asyncio.create_task(self.load_profile())
        self.housekeeping = asyncio.create_task(self.reaper())

    def _load_profile(self) -> dict[str, Any]:
        from orchestrator.orchestrator import ModelOrchestrator

        root = self.config.store_root or model_store_dir()
        if self.store_lock is None:
            self.store_lock = StoreWriterLock(root)
        self.store_lock.acquire()
        self.facade = ModelOrchestrator(
            store=CachedModelStore(root), shippable_only=self.config.shippable_only,
        )
        # Avoid a fixed 8080 runtime port colliding with the public listener.
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as temporary:
            temporary.bind(("127.0.0.1", 0))
            runtime_port = temporary.getsockname()[1]
        host = CachedTextHost(port=runtime_port, context=4096, startup_timeout_s=90)
        self.facade.text = host
        self.facade._hosts[Modality.TEXT] = host
        diagnostics = {"probe": self.facade.report.to_dict(), "planned": {}, "actual": {}}
        selected = [(Modality.TEXT, self.config.text_model)]
        if self.config.profile == "voice":
            selected.extend([(Modality.TTS, self.config.tts_model),
                             (Modality.STT, self.config.stt_model)])
        for modality, model_id in selected:
            if self.state == "draining":
                raise ServiceError("CANCELLED", "Startup cancelled.", 503)
            plan = self.facade.planner.plan(
                modality, model_id=model_id, shippable_only=self.config.shippable_only,
            )
            diagnostics["planned"][modality.value] = {
                "model_id": plan.model.id, "reason": plan.reason,
                "quality": plan.model.quality, "shippable": plan.model.shippable,
            }
            self.loop.call_soon_threadsafe(self._set_diagnostics, json.loads(encode(diagnostics)))
            self.facade.ensure_model(modality, model_id=plan.model.id)
            actual = {"model_id": self.facade._hosts[modality].loaded_model_id}
            if modality is Modality.TTS:
                if self.config.speaker_id is not None:
                    if self.config.speaker_id >= self.facade.tts.num_speakers:
                        raise ServiceError("INVALID_CONFIG", "Speaker ID is outside model range.")
                    self.facade.tts.speaker_id = self.config.speaker_id
                actual.update(sample_rate=self.facade.tts.sample_rate,
                              speaker_id=self.facade.tts.speaker_id)
            if modality is Modality.STT:
                actual.update(sample_rate=self.facade.stt.sample_rate,
                              partials=self.facade.stt.is_streaming)
                # This verified API handles *online* blocks only. Offline models
                # require the host's existing in-memory offline decode adapter.
                parameters = inspect.signature(self.facade.stt.transcribe_samples).parameters
                if "blocks" not in parameters or "rate" not in parameters:
                    raise ServiceError("HOST_API_UNSUPPORTED", "Unsupported STT adapter.", 503)
            diagnostics["actual"][modality.value] = actual
        return diagnostics

    def _set_diagnostics(self, diagnostics: dict[str, Any]) -> None:
        self.diagnostics = diagnostics

    async def load_profile(self) -> None:
        try:
            diagnostics = await self.loop.run_in_executor(
                self.executors["text"], self._load_profile,
            )
            self.diagnostics = diagnostics
            if self.state == "starting":
                self.state = "ready"
        except Exception as exc:
            error = (exc if isinstance(exc, ServiceError) else
                     ServiceError("STARTUP_FAILED", "Selected profile could not be loaded.", 503))
            self.startup_error = error.body()["error"]
            if self.state == "starting":
                self.state = "failed"
            log.error("Service startup failed: %s", error.code)

    def capabilities(self) -> dict[str, Any]:
        kinds: dict[str, Any] = {}
        for kind in ("text", "dialogue", "tts", "stt"):
            lane = "text" if kind == "dialogue" else kind
            supported = lane == "text" or self.config.profile == "voice"
            status = "unsupported" if not supported else (
                "ready" if self.state == "ready" else
                "unprovisioned" if self.startup_error and
                self.startup_error["code"] == "MODEL_NOT_PROVISIONED" else
                "warming" if self.state == "starting" else "unavailable"
            )
            kinds[kind] = {"state": status, **self.diagnostics["actual"].get(lane, {})}
        return {
            "service_instance_id": self.instance_id, "protocol_version": PROTOCOL_VERSION,
            "profile": self.config.profile, "state": self.state, "cached_only": True,
            "shippable_only": self.config.shippable_only, "portfolio_revision": 1,
            "kinds": kinds, **self.diagnostics,
            "limits": {
                "sessions": 4, "queued_active_jobs": 16, "retained_jobs": 256,
                "json_bytes": JSON_LIMIT, "tool_bytes": TOOL_LIMIT,
                "pcm_upload_bytes": PCM_LIMIT, "artifact_bytes": ARTIFACT_LIMIT,
                "artifact_count": 256, "ttl_seconds": TTL_SECONDS,
                "max_tokens": 512, "tts_characters": 300, "tts_seconds": 20,
            },
            "pcm": {"format": "pcm_s16le", "channels": 1, "input_sample_rate": 16000},
            "features": {"client_tools": True, "response_schema": True,
                         "binary_websocket": False, "history": False,
                         "delivery_reconciliation": False, "provisioning": False,
                         "joint_resource_admission": False},
            "startup_error": self.startup_error,
        }

    def require_ready(self) -> None:
        if self.state != "ready":
            raise ServiceError("NOT_READY", "The configured profile is not ready.", 503)

    def get_session(self, identifier: str) -> _Session:
        session = self.sessions.get(uuid_string(identifier))
        if session is None:
            raise ServiceError("SESSION_NOT_FOUND", "Unknown or expired session.", 404)
        return session

    def get_job(self, identifier: str) -> _Job:
        job = self.jobs.get(uuid_string(identifier))
        if job is None:
            raise ServiceError("JOB_NOT_FOUND", "Unknown or expired job.", 404)
        return job

    def prune(self) -> None:
        now = time.monotonic()
        for identifier, artifact in list(self.artifacts.items()):
            if now - artifact.created >= TTL_SECONDS:
                self.artifact_bytes -= len(artifact.pcm)
                del self.artifacts[identifier]
        for identifier, job in list(self.jobs.items()):
            if (not job.host_pending and job.completed is not None
                    and now - job.completed >= TTL_SECONDS):
                del self.jobs[identifier]
                if job.spec.idempotency_key:
                    self.idempotency.pop((job.spec.session_id, job.spec.idempotency_key), None)

    def publish(self, pcm: bytes, sample_rate: int) -> dict[str, Any]:
        self.prune()
        input_bytes = sum(len(job.input_pcm or b"") for job in self.jobs.values())
        if (len(self.artifacts) >= 256
            or self.artifact_bytes + input_bytes + len(pcm) > ARTIFACT_LIMIT):
            raise ServiceError("RESOURCE_EXHAUSTED", "Artifact quota exhausted.", 429)
        identifier = str(uuid4())
        artifact = _Artifact(pcm, sample_rate)
        self.artifacts[identifier] = artifact
        self.artifact_bytes += len(pcm)
        return {"artifact_id": identifier, "sample_rate": sample_rate,
                "sample_count": len(pcm) // 2, "sha256": artifact.sha256}

    def submit(self, spec: JobSpec) -> _Job:
        self.require_ready()
        self.prune()
        fingerprint = hashlib.sha256(encode(asdict(spec))).hexdigest()
        if spec.idempotency_key:
            identifier = self.idempotency.get((spec.session_id, spec.idempotency_key))
            if identifier:
                prior = self.jobs[identifier]
                if prior.request_hash != fingerprint:
                    raise ServiceError(
                        "IDEMPOTENCY_CONFLICT", "Key was used for another request.", 409,
                    )
                return prior
        lane = "text" if spec.kind in ("text", "dialogue") else spec.kind
        if lane != "text" and self.config.profile != "voice":
            raise ServiceError("UNSUPPORTED_KIND", "Voice profile is not configured.")
        session = self.get_session(spec.session_id) if spec.session_id else None
        if spec.turn_id and session is None:
            raise ServiceError("INVALID_SPEC", "Turn IDs require a session.")
        connection = session.connection if session else None
        if (spec.streaming or (lane == "text" and session and session.permitted_tools)) and (
                connection is None or not connection.accepted or connection.closed):
            raise ServiceError("SOCKET_REQUIRED", "An attached negotiated socket is required.", 409)
        if session and any(j.spec.session_id == session.id and j.state not in TERMINAL
                           for j in self.jobs.values()):
            raise ServiceError("SESSION_BUSY", "Only one active job per session is permitted.", 409)
        active = sum(j.state not in TERMINAL or j.host_pending for j in self.jobs.values())
        if active >= 16 or len(self.jobs) >= 256:
            raise ServiceError("RESOURCE_BUSY", "Job admission quota exhausted.", 429)
        pcm = None
        if spec.input_artifact_id:
            artifact = self.artifacts.get(spec.input_artifact_id)
            if artifact is None:
                raise ServiceError("ARTIFACT_NOT_FOUND", "Unknown or consumed artifact.", 404)
            if artifact.sample_rate != 16000 or len(artifact.pcm) > PCM_LIMIT:
                raise ServiceError("INVALID_SPEC", "STT requires bounded 16 kHz PCM input.")
            pcm = artifact.pcm
        job = _Job(str(uuid4()), spec, fingerprint,
                   time.monotonic() + spec.deadline_ms / 1000, input_pcm=pcm)
        self.jobs[job.id] = job
        if spec.idempotency_key:
            self.idempotency[(spec.session_id, spec.idempotency_key)] = job.id
        if spec.input_artifact_id:
            consumed = self.artifacts.pop(spec.input_artifact_id)
            self.artifact_bytes -= len(consumed.pcm)
        self.queues[lane].put_nowait(job)
        return job

    def cancel_job(self, job: _Job, *, deadline: bool = False) -> None:
        if job.state in TERMINAL:
            return
        job.cancel.set()
        # Remove queued jobs physically as well as logically. Otherwise repeated
        # submit/cancel cycles could exhaust the bounded lane queue despite the
        # active admission count dropping to zero.
        if not job.host_pending:
            lane = "text" if job.spec.kind in ("text", "dialogue") else job.spec.kind
            queue = self.queues[lane]
            retained = []
            while not queue.empty():
                item = queue.get_nowait()
                queue.task_done()
                if item is not job:
                    retained.append(item)
            for item in retained:
                queue.put_nowait(item)
            job.input_pcm = None
        job.state = "failed" if deadline else "cancelled"
        job.completed = time.monotonic()
        job.revision += 1
        job.error = ServiceError(
            "DEADLINE_EXCEEDED" if deadline else "CANCELLED",
            "Job deadline exceeded." if deadline else "Job was cancelled.", 409,
        ).body(job.id)["error"]
        session = self.sessions.get(job.spec.session_id or "")
        if session and session.connection:
            for pending in list(session.connection.pending.values()):
                if pending.job is job and not pending.future.done():
                    pending.future.set_exception(
                        ServiceError("CANCELLED", "Tool outcome is unknown.", 409),
                    )

    def disconnect(self, session: _Session, connection: _Connection) -> None:
        connection.closed = True
        for pending in list(connection.pending.values()):
            if not pending.future.done():
                pending.future.set_exception(
                    ServiceError("SOCKET_LOST", "Tool outcome is unknown.", 409),
                )
        if session.connection is connection:
            session.connection = None
            session.disconnected_at = time.monotonic()
            for job in self.jobs.values():
                if job.spec.session_id == session.id:
                    self.cancel_job(job)

    async def notify(self, job: _Job, event: str, payload: dict[str, Any]) -> None:
        session = self.sessions.get(job.spec.session_id or "")
        connection = session.connection if session else None
        if connection and not connection.closed and connection.accepted:
            try:
                await connection.emit(event, payload, job)
            except ServiceError:
                self.disconnect(session, connection)
                try:
                    await connection.websocket.close(code=1008)
                except Exception:
                    pass

    async def remote_tool(self, job: _Job, name: str, arguments: dict[str, Any]) -> Any:
        if job.should_stop():
            raise ServiceError("CANCELLED", "Tool outcome is unknown.", 409)
        session = self.get_session(job.spec.session_id or "")
        connection = session.connection
        if connection is None or connection.closed or not connection.accepted:
            raise ServiceError("SOCKET_LOST", "Tool outcome is unknown.", 409)
        if connection.pending:
            raise ServiceError("RESOURCE_BUSY", "One pending tool per session.", 409)
        identifier = str(uuid4())
        future = self.loop.create_future()
        connection.pending[identifier] = _PendingTool(job, future)
        job.state = "waiting_tool"
        job.revision += 1
        try:
            await connection.emit("job.state", job.snapshot(), job)
            await connection.emit("tool.call", {
                "call_id": identifier, "name": name, "arguments": arguments, "timeout_ms": 5000,
            }, job)
            result = await asyncio.wait_for(
                future, min(5, max(0.001, job.deadline - time.monotonic())),
            )
            if job.should_stop():
                raise ServiceError("CANCELLED", "Tool outcome is unknown.", 409)
            return result
        except asyncio.TimeoutError as exc:
            raise ServiceError("TOOL_TIMEOUT", "Tool outcome is unknown.", 409) from exc
        finally:
            connection.pending.pop(identifier, None)
            if future.done() and not future.cancelled():
                # Consume an exception even if disconnect occurred while the
                # tool.call enqueue was still backpressured.
                future.exception()
            if job.state not in TERMINAL:
                job.state = "running"
                job.revision += 1

    def _emit_from_worker(self, job: _Job, text: str) -> None:
        if job.should_stop():
            return
        # Split UTF-8 deltas conservatively; model output is untrusted too.
        for offset in range(0, len(text), 2048):
            coroutine = self._stream_delta(job, text[offset:offset + 2048])
            future = asyncio.run_coroutine_threadsafe(coroutine, self.loop)
            try:
                future.result(timeout=min(5.1, max(0.01, job.deadline - time.monotonic())))
            except concurrent.futures.TimeoutError as exc:
                future.cancel()
                job.cancel.set()
                raise ServiceError("SLOW_CONSUMER", "Socket output stalled.", 409) from exc

    async def _stream_delta(self, job: _Job, text: str) -> None:
        if job.should_stop():
            return
        session = self.sessions.get(job.spec.session_id or "")
        connection = session.connection if session else None
        if connection is None:
            raise ServiceError("SOCKET_LOST", "Streaming socket disconnected.", 409)
        await connection.emit("text.delta", {"text": text}, job)

    def infer(self, job: _Job) -> Any:
        if job.should_stop():
            return None
        spec = job.spec
        if spec.kind in ("text", "dialogue"):
            registry = _RemoteRegistry(self, job)
            messages = list(spec.messages)
            if spec.text:
                messages.append({"role": "user", "content": spec.text})
            # Tokenize a conservative serialized prompt/schema budget before
            # inference; exact model chat-template accounting is a later host API.
            serialized = encode({
                "messages": messages, "tools": registry.specs(),
                "json_schema": spec.json_schema,
            }).decode()
            tokens = self.facade.text._post("/tokenize", {"content": serialized}).get("tokens")
            if not isinstance(tokens, list) or len(tokens) + spec.max_tokens + 256 > 4096:
                raise ServiceError("CONTEXT_LIMIT", "Prompt and output exceed context budget.")
            if spec.json_schema is not None:
                from jsonschema import Draft202012Validator

                parts: list[str] = []
                size = 0
                for chunk in self.facade.text.stream_chat(
                    messages, max_tokens=spec.max_tokens, temperature=spec.temperature,
                    json_schema=spec.json_schema, should_stop=job.should_stop,
                ):
                    size += len(chunk.encode("utf-8"))
                    if size > 24 * 1024:
                        raise ServiceError("OUTPUT_LIMIT", "Generated JSON exceeds its byte limit.")
                    parts.append(chunk)
                    if spec.streaming:
                        self._emit_from_worker(job, chunk)
                if job.should_stop():
                    return None
                text = "".join(parts)
                data = decode(text, 24 * 1024)
                if not Draft202012Validator(spec.json_schema).is_valid(data):
                    raise ServiceError("INVALID_OUTPUT", "Model response failed its schema.")
                return {"text": text, "calls": []}
            output_bytes = 0

            def on_text(chunk: str) -> None:
                nonlocal output_bytes
                output_bytes += len(chunk.encode("utf-8"))
                if output_bytes > 24 * 1024:
                    raise ServiceError("OUTPUT_LIMIT", "Generated text exceeds its byte limit.")
                if spec.streaming:
                    self._emit_from_worker(job, chunk)

            text, calls = self.facade.text.stream_chat_with_tools(
                messages, registry, max_tokens=spec.max_tokens,
                temperature=spec.temperature, max_rounds=4,
                on_text=on_text, should_stop=job.should_stop,
            )
            result = {"text": text, "calls": [
                {"id": call.id, "name": call.name, "arguments": call.arguments,
                 "result": call.result} for call in calls
            ]}
            encode(result, JSON_LIMIT - 2048)
            return result
        import numpy as np

        if spec.kind == "tts":
            samples, rate = self.facade.tts.synthesize_samples(spec.text)
            rate = int(rate)
            audio = np.asarray(samples)
            if (rate <= 0 or rate > 192_000 or audio.ndim != 1
                    or len(audio) > rate * 20 or not np.isfinite(audio).all()):
                raise ServiceError("OUTPUT_LIMIT", "TTS output exceeds its format/duration limits.")
            pcm = np.rint(np.clip(audio, -1, 1) * 32767).astype("<i2").tobytes()
            return pcm, rate
        audio = np.frombuffer(job.input_pcm or b"", dtype="<i2").astype(np.float32) / 32768
        if self.facade.stt.is_streaming:
            def blocks() -> Any:
                for offset in range(0, len(audio), 1600):
                    if job.should_stop():
                        break
                    yield audio[offset:offset + 1600]

            text = self.facade.stt.transcribe_samples(blocks(), rate=16000)
        else:
            # transcribe_samples calls OnlineRecognizer-only APIs. Use the real
            # host's offline decoder directly, with no temporary WAV or fake host.
            text = self.facade.stt._transcribe_offline(audio, 16000)
        string(text, 16_384)
        return {"text": text}

    async def lane(self, name: str) -> None:
        queue = self.queues[name]
        while True:
            job = await queue.get()
            try:
                if job is None:
                    return
                if job.state in TERMINAL:
                    continue
                if job.should_stop():
                    self.cancel_job(job, deadline=True)
                    continue
                job.host_pending = True
                job.state = "running"
                job.revision += 1
                await self.notify(job, "job.state", job.snapshot())
                try:
                    result = await self.loop.run_in_executor(self.executors[name], self.infer, job)
                    if job.should_stop():
                        self.cancel_job(job, deadline=not job.cancel.is_set())
                    elif job.state not in TERMINAL:
                        job.result = self.publish(*result) if name == "tts" else result
                        job.state = "succeeded"
                        job.completed = time.monotonic()
                        job.revision += 1
                        if name == "text" and job.spec.streaming:
                            await self.notify(job, "text.end", {"text": job.result["text"]})
                except Exception as exc:
                    if job.state not in TERMINAL:
                        error = (exc if isinstance(exc, ServiceError) else
                                 ServiceError("HOST_FAILED", "Local inference failed.", 500))
                        job.state = "failed"
                        job.error = error.body(job.id)["error"]
                        job.completed = time.monotonic()
                        job.revision += 1
                        await self.notify(job, "error", {"error": job.error})
                finally:
                    # Never free the lane because an HTTP request was cancelled.
                    job.host_pending = False
                    job.input_pcm = None
                    job.revision += 1
                    await self.notify(job, "job.state", job.snapshot())
            finally:
                if job is not None and not job.host_pending:
                    job.input_pcm = None
                queue.task_done()

    async def reaper(self) -> None:
        while True:
            await asyncio.sleep(0.2)
            self.prune()
            for job in list(self.jobs.values()):
                if job.state not in TERMINAL and time.monotonic() >= job.deadline:
                    self.cancel_job(job, deadline=True)
                    await self.notify(job, "job.state", job.snapshot())
            for identifier, session in list(self.sessions.items()):
                if session.connection is None and time.monotonic() - session.disconnected_at >= 60:
                    for job in self.jobs.values():
                        if job.spec.session_id == identifier:
                            self.cancel_job(job)
                    self.sessions.pop(identifier, None)

    def begin_shutdown(self) -> None:
        if self.state in ("draining", "stopped"):
            return
        self.state = "draining"
        for job in self.jobs.values():
            self.cancel_job(job)

    async def stop(self) -> None:
        self.begin_shutdown()
        if self.housekeeping:
            self.housekeeping.cancel()
        for session in list(self.sessions.values()):
            connection = session.connection
            if connection:
                self.disconnect(session, connection)
                try:
                    await asyncio.wait_for(connection.websocket.close(code=1001), 1)
                except Exception:
                    pass
        for queue in self.queues.values():
            queue.put_nowait(None)
        pending = [*self.workers]
        if self.startup_task:
            pending.append(self.startup_task)
        _, remaining = await asyncio.wait(pending, timeout=5)
        if not remaining:
            if self.facade is not None:
                cleanup = self.loop.run_in_executor(self.executors["text"], self.facade.stop)
                try:
                    await asyncio.wait_for(asyncio.shield(cleanup), 5)
                except asyncio.TimeoutError:
                    log.warning("Host unload remains pending; owner termination may be required")
                    for executor in self.executors.values():
                        executor.shutdown(wait=False, cancel_futures=False)
                    return
            if self.store_lock:
                self.store_lock.release()
            self.state = "stopped"
        else:
            # Leave the writer lock and models intact while native work is alive.
            # The spawned owner must force-terminate its owned process if needed.
            log.warning("Native work remains pending; owner termination may be required")
        for executor in self.executors.values():
            executor.shutdown(wait=False, cancel_futures=False)


def create_app(
    config: ServiceConfig | None = None, *, token: str | None = None,
    shutdown_callback: Callable[[], None] | None = None,
    store_lock: StoreWriterLock | None = None,
) -> Any:
    """Build a FastAPI app. Hosts load only during its lifespan, in a worker."""
    from fastapi import FastAPI, Request, WebSocket
    from fastapi.responses import JSONResponse, Response
    # FastAPI resolves postponed endpoint annotations in the module namespace.
    globals().update(Request=Request, WebSocket=WebSocket)
    config = config or ServiceConfig()
    token = token if token is not None else os.environ.get("ECHOCRADLE_SERVICE_TOKEN", "")
    if not token or not token.isascii() or any(c.isspace() for c in token):
        raise ValueError("ECHOCRADLE_SERVICE_TOKEN must contain an ASCII bearer secret")
    runtime = _Runtime(config, shutdown_callback, store_lock)

    @asynccontextmanager
    async def lifespan(app: Any) -> Any:
        await runtime.start()
        try:
            yield
        finally:
            await runtime.stop()

    app = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
    app.state.service = runtime
    app.add_middleware(_BoundaryMiddleware, token=token, config=config)

    @app.exception_handler(ServiceError)
    async def service_error(request: Request, exc: ServiceError) -> Any:
        return JSONResponse(exc.body(), status_code=exc.status)

    async def body(request: Request) -> dict[str, Any]:
        if request.headers.get("content-type", "").split(";")[0].strip() != "application/json":
            raise ServiceError("INVALID_CONTENT_TYPE", "Expected application/json.", 415)
        return decode(await request.body())

    @app.get("/v1/health/live")
    async def live() -> dict[str, Any]:
        return {"service_instance_id": runtime.instance_id, "protocol_version": PROTOCOL_VERSION}

    @app.get("/v1/health/ready")
    async def ready() -> Any:
        return JSONResponse({**await live(), "state": runtime.state,
                             "ready": runtime.state == "ready", "error": runtime.startup_error},
                            status_code=200 if runtime.state == "ready" else 503)

    @app.get("/v1/capabilities")
    async def capabilities() -> dict[str, Any]:
        return runtime.capabilities()

    @app.post("/v1/sessions", status_code=201)
    async def create_session(request: Request) -> dict[str, Any]:
        if runtime.state in ("draining", "stopped"):
            raise ServiceError("NOT_READY", "The service is draining.", 503)
        tools, mode = session_spec(await body(request))
        if len(runtime.sessions) >= 4:
            raise ServiceError("RESOURCE_BUSY", "Session quota exhausted.", 429)
        identifier = str(uuid4())
        runtime.sessions[identifier] = _Session(identifier, tools, mode)
        return {**await live(), "session_id": identifier,
            "permitted_tools": runtime.sessions[identifier].permitted_tools,
                "delivery_mode": mode, "events_path": f"/v1/sessions/{identifier}/events"}

    @app.delete("/v1/sessions/{session_id}")
    async def delete_session(session_id: str) -> dict[str, Any]:
        session = runtime.get_session(session_id)
        for job in runtime.jobs.values():
            if job.spec.session_id == session.id:
                runtime.cancel_job(job)
        connection = session.connection
        if connection:
            runtime.disconnect(session, connection)
            await connection.websocket.close(code=1001)
        runtime.sessions.pop(session.id, None)
        return {"session_id": session.id, "state": "expired"}

    @app.post("/v1/jobs", status_code=202)
    async def submit_job(request: Request) -> dict[str, Any]:
        job = runtime.submit(JobSpec.parse(await body(request)))
        return {**await live(), **job.snapshot()}

    @app.get("/v1/jobs/{job_id}")
    async def get_job(job_id: str) -> dict[str, Any]:
        runtime.prune()
        job = runtime.get_job(job_id)
        lane = "text" if job.spec.kind == "dialogue" else job.spec.kind
        return {**await live(), **job.snapshot(),
                "selected_model": runtime.diagnostics["actual"].get(lane)}

    @app.post("/v1/jobs/{job_id}/cancel")
    async def cancel_job(job_id: str) -> dict[str, Any]:
        job = runtime.get_job(job_id)
        runtime.cancel_job(job)
        await runtime.notify(job, "job.state", job.snapshot())
        return job.snapshot()

    @app.post("/v1/artifacts", status_code=201)
    async def upload_artifact(request: Request) -> dict[str, Any]:
        if runtime.state in ("draining", "stopped"):
            raise ServiceError("NOT_READY", "The service is draining.", 503)
        if request.headers.get("content-type") != "application/octet-stream":
            raise ServiceError("INVALID_CONTENT_TYPE", "Expected raw PCM octet-stream.", 415)
        if request.headers.getlist("x-sample-rate") != ["16000"]:
            raise ServiceError("INVALID_SPEC", "PCM sample rate must be 16000.")
        pcm = await request.body()
        if not pcm or len(pcm) % 2 or len(pcm) > PCM_LIMIT:
            raise ServiceError("INVALID_PCM", "Expected 1..480000 PCM16 mono samples.")
        result = runtime.publish(pcm, 16000)
        return {"artifact_id": result["artifact_id"]}

    @app.get("/v1/artifacts/{artifact_id}")
    async def get_artifact(artifact_id: str) -> Any:
        runtime.prune()
        artifact = runtime.artifacts.get(uuid_string(artifact_id))
        if artifact is None:
            raise ServiceError("ARTIFACT_NOT_FOUND", "Unknown or expired artifact.", 404)
        return Response(artifact.pcm, media_type="application/octet-stream", headers={
            "X-Sample-Rate": str(artifact.sample_rate),
            "X-Sample-Count": str(len(artifact.pcm) // 2),
            "X-Content-SHA256": artifact.sha256, "ETag": f'"{artifact.sha256}"',
            "Cache-Control": "no-store",
        })

    @app.delete("/v1/artifacts/{artifact_id}")
    async def delete_artifact(artifact_id: str) -> dict[str, Any]:
        runtime.prune()
        identifier = uuid_string(artifact_id)
        artifact = runtime.artifacts.pop(identifier, None)
        if artifact is None:
            raise ServiceError("ARTIFACT_NOT_FOUND", "Unknown or expired artifact.", 404)
        runtime.artifact_bytes -= len(artifact.pcm)
        return {"artifact_id": identifier, "state": "deleted"}

    @app.post("/v1/control/shutdown", status_code=202)
    async def shutdown() -> dict[str, Any]:
        if not config.spawned_owner:
            raise ServiceError("FORBIDDEN", "Only the spawned owner may shut down.", 403)
        runtime.begin_shutdown()
        if runtime.shutdown_callback:
            runtime.loop.call_later(0.05, runtime.shutdown_callback)
        return {**await live(), "state": "draining"}

    @app.websocket("/v1/sessions/{session_id}/events")
    async def events(websocket: WebSocket, session_id: str) -> None:
        from starlette.websockets import WebSocketDisconnect

        connection: _Connection | None = None
        writer: asyncio.Task[Any] | None = None
        try:
            if runtime.state in ("draining", "stopped"):
                raise ServiceError("NOT_READY", "The service is draining.", 503)
            session = runtime.get_session(session_id)
            if session.connection is not None:
                raise ServiceError("SOCKET_EXISTS", "One socket per session.", 409)
            connection = _Connection(runtime, session, websocket)
            session.connection = connection
            await websocket.accept()
            sequence = 1
            while not connection.closed:
                raw = await asyncio.wait_for(websocket.receive(), 15 if sequence > 1 else 5)
                if raw["type"] == "websocket.disconnect":
                    break
                if raw.get("bytes") is not None:
                    raise ServiceError("PROTOCOL_ERROR", "Binary socket audio is unsupported.", 400)
                data = decode(raw.get("text") or "")
                event, payload = envelope(data, sequence)
                sequence += 1
                if data.get("service_instance_id", runtime.instance_id) != runtime.instance_id:
                    raise ServiceError("PROTOCOL_ERROR", "Wrong service instance.", 400)
                if data.get("session_id", session.id) != session.id:
                    raise ServiceError("PROTOCOL_ERROR", "Wrong session.", 400)
                if not connection.accepted:
                    if event != "hello" or payload:
                        raise ServiceError(
                            "PROTOCOL_ERROR", "First event must be hello with {}.", 400,
                        )
                    connection.accepted = True
                    writer = asyncio.create_task(connection.writer())
                    await connection.emit("hello.accepted", {
                        "protocol_version": PROTOCOL_VERSION, "json_bytes": JSON_LIMIT,
                        "tool_bytes": TOOL_LIMIT, "ping_interval_ms": 5000,
                        "idle_timeout_ms": 15000,
                    })
                elif event == "ping":
                    fields(payload, set())
                    await connection.emit("pong", {})
                elif event == "turn.cancel":
                    fields(payload, {"job_id"}, {"job_id"})
                    job = runtime.get_job(uuid_string(payload["job_id"]))
                    if job.spec.session_id != session.id:
                        raise ServiceError("FORBIDDEN", "Job belongs to another session.", 403)
                    runtime.cancel_job(job)
                    await connection.emit("job.state", job.snapshot(), job)
                elif event == "tool.result":
                    fields(payload, {"call_id", "result"}, {"call_id", "result"})
                    encode(payload["result"], TOOL_LIMIT)
                    identifier = uuid_string(payload["call_id"])
                    pending = connection.pending.get(identifier)
                    if pending is None or pending.future.done() or pending.job.should_stop():
                        # A late/duplicate result never resumes cancelled work.
                        await connection.emit("error", ServiceError(
                            "STALE_TOOL_RESULT", "Unknown, late or duplicate tool result.", 409,
                        ).body())
                        continue
                    if (data.get("job_id", pending.job.id) != pending.job.id
                            or data.get("turn_id", pending.job.spec.turn_id)
                            != pending.job.spec.turn_id):
                        raise ServiceError("PROTOCOL_ERROR", "Tool result identity mismatch.", 400)
                    pending.future.set_result(payload["result"])
                else:
                    raise ServiceError("PROTOCOL_ERROR", "Unsupported socket event.", 400)
        except (WebSocketDisconnect, asyncio.TimeoutError):
            pass
        except ServiceError as exc:
            if connection and connection.accepted and not connection.closed:
                try:
                    await connection.emit("error", exc.body())
                    await asyncio.wait_for(connection.queue.join(), 0.2)
                except (ServiceError, asyncio.TimeoutError):
                    pass
            try:
                await websocket.close(code=1009 if exc.status == 413 else
                                      1002 if exc.status in (400, 422) else 1008)
            except Exception:
                pass
        finally:
            if connection:
                runtime.disconnect(connection.session, connection)
            if writer:
                writer.cancel()
                await asyncio.gather(writer, return_exceptions=True)
            try:
                await websocket.close(code=1001)
            except Exception:
                pass

    return app


def main(argv: list[str] | None = None) -> int:
    """Bind loopback, emit the sole bootstrap stdout line, then serve."""
    parser = argparse.ArgumentParser(description="EchoCradle cached-only local service")
    parser.add_argument("--port", type=int, default=0)
    parser.add_argument("--profile", choices=("text", "voice"), default="text")
    parser.add_argument("--shippable-only", action="store_true")
    parser.add_argument("--text-model")
    parser.add_argument("--tts-model")
    parser.add_argument("--stt-model")
    parser.add_argument("--speaker-id", type=int)
    args = parser.parse_args(argv)
    bootstrap = sys.stdout
    # All dependency/native prints and uvicorn/access logs go to stderr.
    sys.stdout = sys.stderr
    listener: socket.socket | None = None
    lock: StoreWriterLock | None = None
    try:
        import uvicorn

        config = ServiceConfig(**vars(args))
        token = os.environ.get("ECHOCRADLE_SERVICE_TOKEN", "")
        if not token or not token.isascii() or any(c.isspace() for c in token):
            raise ValueError("ECHOCRADLE_SERVICE_TOKEN is required")
        lock = StoreWriterLock(config.store_root or model_store_dir())
        lock.acquire()
        listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        listener.bind(("127.0.0.1", config.port))
        listener.listen(64)
        port = listener.getsockname()[1]
        config = ServiceConfig(**{**vars(args), "port": port})
        app = create_app(config, token=token, store_lock=lock)
        # Keep access logs disabled: URLs are untrusted and may contain secrets
        # despite the protocol refusing URL-based authentication.
        server = uvicorn.Server(uvicorn.Config(
            app, host="127.0.0.1", port=port, workers=1, access_log=False,
            ws_max_size=JSON_LIMIT, ws_max_queue=4, ws_per_message_deflate=False,
            timeout_keep_alive=5, timeout_graceful_shutdown=5, limit_concurrency=32,
            log_config={
                "version": 1, "disable_existing_loggers": False,
                "formatters": {"default": {"format": "%(levelname)s: %(message)s"}},
                "handlers": {"default": {"class": "logging.StreamHandler",
                                         "stream": "ext://sys.stderr",
                                         "formatter": "default"}},
                "root": {"handlers": ["default"], "level": "INFO"},
                "loggers": {"uvicorn": {"handlers": ["default"], "level": "INFO",
                                         "propagate": False}},
            },
        ))
        app.state.service.shutdown_callback = lambda: setattr(server, "should_exit", True)
        bootstrap.write(json.dumps({"port": port,
                                    "service_instance_id": app.state.service.instance_id,
                                    "protocol_version": PROTOCOL_VERSION}) + "\n")
        bootstrap.flush()
        server.run(sockets=[listener])
        return 0
    except (ImportError, ValueError, OSError, ServiceError) as exc:
        # Do not emit paths, secrets, model prompts or arbitrary native exceptions.
        code = getattr(exc, "code", type(exc).__name__)
        print(f"Service launch failed: {code}", file=sys.stderr)
        return 1
    finally:
        if listener:
            listener.close()
        # Retain an acquired lock if native workers still run; the OS releases
        # it on process death. Clean lifespan shutdown releases it explicitly.
        # Do not restore stdout: cancelled native workers may still print after
        # uvicorn returns. Bootstrap remains the only stdout output from this CLI.


if __name__ == "__main__":
    raise SystemExit(main())