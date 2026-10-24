"""Synchronous, loopback-only inference facade for the cached service.

Inference belongs to the service; device I/O, VAD and authoritative tools stay
with the caller. No provisioning or gameplay model selection happens here.
An attached service is never shut down or process-terminated by this client.
"""

from __future__ import annotations

import asyncio
import ctypes
import hashlib
import os
import queue
import secrets
import signal
import subprocess
import sys
import threading
import time
import wave
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Callable, cast
from urllib.parse import urlsplit
from uuid import uuid4

import httpx
import numpy as np
from websockets.sync.client import connect

from orchestrator.errors import OrchestratorError
from orchestrator.paths import model_store_dir
from orchestrator.service_protocol import (
    JSON_LIMIT, PCM_LIMIT, PROTOCOL_VERSION, TERMINAL, TOOL_LIMIT,
    JobSpec, ServiceError, decode, encode, envelope, uuid_string,
)
from orchestrator.client_types import (
    Capabilities, Message, TextResult, ToolOutcome, ToolRegistry, VisualResult, freeze_json,
)


_VISUAL_LIMIT = 32 * 1024 * 1024


class ServiceClientError(OrchestratorError):
    """Sanitized failure metadata; partial results never imply successful completion."""

    def __init__(
        self, code: str, message: str, *, status: int | None = None,
        retryable: bool = False, job_id: str | None = None, phase: str = "client",
        partial_result: TextResult | None = None,
    ) -> None:
        self.code = code
        self.status = status
        self.retryable = retryable
        self.job_id = job_id
        self.phase = phase
        self.partial_result = partial_result
        super().__init__(f"{code}: {message}")


class ServiceCancelledError(ServiceClientError):
    """Cancellation of a delta iterator/non-text job; carries any known partial result."""


def _failure(code: str, message: str, **metadata: Any) -> ServiceClientError:
    cls = ServiceCancelledError if code == "CANCELLED" else ServiceClientError
    return cls(code, message, **metadata)


def _wire_error(
    value: Any, *, status: int | None = None, job_id: str | None = None,
    phase: str = "inference", partial_result: TextResult | None = None,
) -> ServiceClientError:
    if not isinstance(value, dict):
        raise _failure("PROTOCOL_ERROR", "Invalid service error metadata.", phase=phase)
    code, retryable, correlation = value.get("code"), value.get("retryable"), value.get("job_id")
    if (not isinstance(code, str) or not code or len(code) > 80
            or any(c not in "ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_" for c in code)
            or type(retryable) is not bool):
        raise _failure("PROTOCOL_ERROR", "Invalid service error metadata.", phase=phase)
    if correlation is not None:
        correlation = _identifier(correlation)
        if job_id is not None and correlation != job_id:
            raise _failure("PROTOCOL_ERROR", "Error job identity mismatch.", phase=phase)
    return _failure(
        code, "Service operation did not complete.", status=status, retryable=retryable,
        job_id=correlation or job_id, phase=phase, partial_result=partial_result,
    )


def _deadline(timeout: float) -> float:
    if (isinstance(timeout, bool) or not isinstance(timeout, (int, float))
            or not 0 < timeout <= 120):
        raise _failure("INVALID_CONFIG", "Timeout must be in (0, 120] seconds.")
    return time.monotonic() + timeout


def _remaining(deadline: float, phase: str = "transport") -> float:
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise _failure("DEADLINE_EXCEEDED", "Total operation budget expired.", phase=phase)
    return remaining


def _cancellation(event: threading.Event | None) -> None:
    if event is not None and not isinstance(event, threading.Event):
        raise _failure("INVALID_CONFIG", "Cancellation must be a threading.Event.")


def _identifier(value: Any) -> str:
    try:
        return uuid_string(value)
    except ServiceError:
        raise _failure("PROTOCOL_ERROR", "Invalid response identifier.") from None


class _WindowsJob:
    """Kill-on-close Job Object, assigned before the suspended child can run."""

    def __init__(self) -> None:
        from ctypes import wintypes

        class BasicLimits(ctypes.Structure):
            _fields_ = [
                ("process_time", ctypes.c_int64), ("job_time", ctypes.c_int64),
                ("flags", wintypes.DWORD), ("min_working", ctypes.c_size_t),
                ("max_working", ctypes.c_size_t), ("active", wintypes.DWORD),
                ("affinity", ctypes.c_size_t), ("priority", wintypes.DWORD),
                ("scheduling", wintypes.DWORD),
            ]

        class IoCounters(ctypes.Structure):
            _fields_ = [(name, ctypes.c_uint64) for name in (
                "read_ops", "write_ops", "other_ops", "read_bytes", "write_bytes", "other_bytes",
            )]

        class ExtendedLimits(ctypes.Structure):
            _fields_ = [
                ("basic", BasicLimits), ("io", IoCounters),
                ("process_memory", ctypes.c_size_t), ("job_memory", ctypes.c_size_t),
                ("peak_process", ctypes.c_size_t), ("peak_job", ctypes.c_size_t),
            ]

        self._kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        self._kernel.CreateJobObjectW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR]
        self._kernel.CreateJobObjectW.restype = wintypes.HANDLE
        self._kernel.SetInformationJobObject.argtypes = [
            wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD,
        ]
        self._kernel.SetInformationJobObject.restype = wintypes.BOOL
        self._kernel.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
        self._kernel.AssignProcessToJobObject.restype = wintypes.BOOL
        self._kernel.CloseHandle.argtypes = [wintypes.HANDLE]
        self._kernel.CloseHandle.restype = wintypes.BOOL
        self._handle = self._kernel.CreateJobObjectW(None, None)
        if not self._handle:
            raise _failure("OWNERSHIP_FAILED", "Cannot create service process container.")
        limits = ExtendedLimits()
        limits.basic.flags = 0x2000  # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE, no breakaway.
        if not self._kernel.SetInformationJobObject(
            self._handle, 9, ctypes.byref(limits), ctypes.sizeof(limits),
        ):
            self.close()
            raise _failure("OWNERSHIP_FAILED", "Cannot configure service process container.")

    def assign_and_resume(self, process: subprocess.Popen[Any]) -> None:
        from ctypes import wintypes

        handle = wintypes.HANDLE(int(process._handle))
        if not self._kernel.AssignProcessToJobObject(self._handle, handle):
            raise _failure("OWNERSHIP_FAILED", "Cannot contain the service process.")
        native = ctypes.WinDLL("ntdll")
        native.NtResumeProcess.argtypes = [wintypes.HANDLE]
        native.NtResumeProcess.restype = ctypes.c_long
        if native.NtResumeProcess(handle) != 0:
            raise _failure("OWNERSHIP_FAILED", "Cannot resume the contained service.")

    def close(self) -> None:
        if self._handle:
            self._kernel.CloseHandle(self._handle)
            self._handle = None


class _TtsProxy:
    def __init__(self, client: ServiceClient) -> None:
        self._client = client
        self.sample_rate = 0
        self.speaker_id = 0

    def synthesize_samples(self, sentence: str) -> tuple[np.ndarray, int]:
        return self._client._synthesize_samples(sentence)


class ServiceClient:
    """Blocking application client. Use ``start_owned`` or ``attach`` explicitly.

    Constructor model overrides are service startup policy only. Tool turns use
    the backend's fixed four-round policy. STT uploads at most 30 seconds of
    mono 16 kHz PCM; the streaming capability describes the server's recognizer,
    not partial network transcripts. Callbacks and tool invocation run only on
    the caller thread. An Event is safe to set from any thread. Already-running
    user code cannot be forcibly interrupted: a blocked callback/tool can exceed
    the budget, but the watcher still cancels remote work. Never block callbacks.
    Transport waits use a total monotonic budget, not a resetting read timeout.
    """

    @classmethod
    def start_owned(
        cls, *, profile: str = "voice", text_model: str | None = None,
        tts_model: str | None = None, stt_model: str | None = None,
        speaker_id: int | None = None, shippable_only: bool = False,
        vad_path: Path | str | None = None,
        visual_config: Path | None = None,
    ) -> ServiceClient:
        """Start a cached-only service; missing deployments fail, never provision."""
        return cls(
            profile=profile, text_model=text_model, tts_model=tts_model,
            stt_model=stt_model, speaker_id=speaker_id,
            shippable_only=shippable_only, vad_path=vad_path,
            visual_config=visual_config,
        )

    @classmethod
    def attach(
        cls, url: str, *, token: str | None = None, vad_path: Path | str | None = None,
    ) -> ServiceClient:
        """Attach without acquiring process ownership or altering service policy."""
        return cls(url=url, token=token, vad_path=vad_path)

    def __init__(
        self,
        *,
        profile: str = "voice",
        url: str | None = None,
        token: str | None = None,
        text_model: str | None = None,
        tts_model: str | None = None,
        stt_model: str | None = None,
        speaker_id: int | None = None,
        shippable_only: bool = False,
        vad_path: Path | str | None = None,
        visual_config: Path | None = None,
    ) -> None:
        if profile not in ("voice", "text", "visual"):
            raise _failure("INVALID_CONFIG", "Profile must be voice, text or visual.")
        if url is None and profile == "visual" and visual_config is None:
            raise _failure("INVALID_CONFIG", "Visual profile requires visual_config.")
        self._lock = threading.RLock()
        self._closing = threading.Event()
        self._closed = False
        self._jobs: set[str] = set()
        self._sockets: set[Any] = set()
        self._threads: set[threading.Thread] = set()
        self._process: subprocess.Popen[Any] | None = None
        self._windows_job: _WindowsJob | None = None
        self._http: httpx.Client | None = None
        self._instance: str | None = None
        self._owned = url is None
        self._url = ""
        self._token = ""
        self._vad_path = Path(vad_path) if vad_path is not None else (
            model_store_dir() / "silero-vad" / "silero_vad.onnx"
        )
        self.tts = _TtsProxy(self)
        self.stt = SimpleNamespace(sample_rate=16000, is_streaming=False)
        self.report = SimpleNamespace()
        try:
            if self._owned:
                self._token = secrets.token_urlsafe(32)
                bootstrap = self._spawn(
                    profile, text_model, tts_model, stt_model, speaker_id, shippable_only,
                    visual_config,
                )
                if bootstrap.get("protocol_version") != PROTOCOL_VERSION:
                    raise _failure("PROTOCOL_ERROR", "Unsupported bootstrap protocol.")
                port = bootstrap.get("port")
                if type(port) is not int or not 1 <= port <= 65535:
                    raise _failure("PROTOCOL_ERROR", "Invalid bootstrap listener.")
                self._instance = _identifier(bootstrap.get("service_instance_id"))
                self._url = f"http://127.0.0.1:{port}"
            else:
                if any(value is not None for value in (
                    text_model, tts_model, stt_model, speaker_id, visual_config,
                )) or shippable_only:
                    raise _failure("INVALID_CONFIG", "Attached service owns model policy.")
                self._url = self._validate_url(url or "")
                self._token = token or os.environ.get("ECHOCRADLE_SERVICE_TOKEN", "")
            if (not self._token or not self._token.isascii()
                    or any(char.isspace() for char in self._token)):
                raise _failure("INVALID_CONFIG", "An ASCII bearer token is required.")
            self._http = httpx.Client(
                base_url=self._url, headers={"Authorization": f"Bearer {self._token}"},
                timeout=httpx.Timeout(5), trust_env=False, follow_redirects=False,
            )
            self._wait_ready()
            self.capabilities()
        except BaseException:
            self.close()
            raise

    @staticmethod
    def _validate_url(url: str) -> str:
        try:
            parsed = urlsplit(url)
            if (parsed.scheme not in ("http", "https")
                    or parsed.hostname not in ("127.0.0.1", "localhost", "::1")
                    or parsed.username is not None or parsed.password is not None
                    or parsed.query or parsed.fragment or parsed.path not in ("", "/")
                    or "?" in url or "#" in url or any(c.isspace() for c in url)):
                raise ValueError
            if parsed.port is not None and not 1 <= parsed.port <= 65535:
                raise ValueError
        except ValueError:
            raise _failure("INVALID_CONFIG", "Service URL must be a loopback origin.") from None
        return url.rstrip("/")

    def _spawn(
        self, profile: str, text_model: str | None, tts_model: str | None,
        stt_model: str | None, speaker_id: int | None, shippable_only: bool,
        visual_config: Path | None = None,
    ) -> dict[str, Any]:
        command = [sys.executable, "-m", "orchestrator.service", "--profile", profile]
        for flag, value in (
            ("--text-model", text_model), ("--tts-model", tts_model),
            ("--stt-model", stt_model), ("--speaker-id", speaker_id),
            ("--visual-config", visual_config.resolve() if visual_config is not None else None),
        ):
            if value is not None:
                command.extend([flag, str(value)])
        if shippable_only:
            command.append("--shippable-only")
        environment = os.environ.copy()
        environment["ECHOCRADLE_SERVICE_TOKEN"] = self._token
        environment["PYTHONUNBUFFERED"] = "1"
        parent = Path(__file__).resolve().parent.parent
        environment["PYTHONPATH"] = os.pathsep.join(
            filter(None, [str(parent), environment.get("PYTHONPATH", "")]),
        )
        try:
            if os.name == "nt":
                self._windows_job = _WindowsJob()
            self._process = subprocess.Popen(
                command, cwd=parent, env=environment, stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE, stderr=None,
                start_new_session=os.name != "nt",
                creationflags=0x4 if os.name == "nt" else 0,  # CREATE_SUSPENDED
            )
            if self._windows_job is not None:
                self._windows_job.assign_and_resume(self._process)
        except (OSError, ValueError):
            raise _failure("STARTUP_FAILED", "Cannot launch the local service.") from None
        process = self._process
        output: queue.Queue[Any] = queue.Queue(maxsize=1)

        def bootstrap_read() -> None:
            try:
                assert process.stdout is not None
                output.put(process.stdout.readline(JSON_LIMIT + 1))
            except OSError:
                output.put(b"")

        # Exactly one bounded readline in exactly one bootstrap reader.
        reader = self._thread(bootstrap_read, "service-bootstrap")
        try:
            raw = output.get(timeout=90)
        except queue.Empty:
            raise _failure("STARTUP_TIMEOUT", "Service bootstrap exceeded 90 seconds.") from None
        if not raw or not raw.endswith(b"\n"):
            raise _failure("STARTUP_FAILED", "Service exited without a bootstrap record.")
        reader.join(timeout=1)
        if process.stdout is not None:
            process.stdout.close()
        try:
            return decode(raw)
        except ServiceError:
            raise _failure("PROTOCOL_ERROR", "Invalid service bootstrap record.") from None

    def _thread(self, target: Callable[[], None], name: str) -> threading.Thread:
        def run() -> None:
            try:
                target()
            finally:
                with self._lock:
                    self._threads.discard(threading.current_thread())

        thread = threading.Thread(target=run, name=name, daemon=True)
        with self._lock:
            if self._closing.is_set():
                raise _failure("CLIENT_CLOSED", "Service client is closing.")
            self._threads.add(thread)
            thread.start()
        return thread

    def _check_identity(self, data: dict[str, Any]) -> None:
        instance = _identifier(data.get("service_instance_id"))
        if (type(data.get("protocol_version")) is not int
                or data["protocol_version"] != PROTOCOL_VERSION):
            raise _failure("PROTOCOL_ERROR", "Unsupported service protocol.")
        with self._lock:
            if self._instance is None:
                self._instance = instance
            if self._instance != instance:
                raise _failure("INSTANCE_CHANGED", "The service instance changed.")

    def _response(
        self, method: str, path: str, *, payload: dict[str, Any] | None = None,
        content: bytes | None = None, headers: dict[str, str] | None = None,
        limit: int = JSON_LIMIT, closing: bool = False, deadline: float | None = None,
    ) -> tuple[bytes, httpx.Headers, int]:
        if self._http is None or (self._closing.is_set() and not closing):
            raise _failure("CLIENT_CLOSED", "Service client is closed.")
        deadline = deadline if deadline is not None else time.monotonic() + 5
        remaining = _remaining(deadline)
        if payload is not None:
            try:
                content = encode(payload)
                headers = {**(headers or {}), "Content-Type": "application/json"}
            except ServiceError as exc:
                raise _failure(exc.code, exc.message, phase="validation") from None
        http = self._http
        output: queue.Queue[Any] = queue.Queue(maxsize=1)

        def request() -> None:
            # Isolate blocking header/body reads. Caller never waits past its total
            # budget; the transport also receives the remaining inactivity timeout.
            # A pathological custom transport may retain this daemon until it returns.
            try:
                with http.stream(
                    method, path, content=content, headers=headers,
                    timeout=httpx.Timeout(min(5, remaining)),
                ) as response:
                    raw = bytearray()
                    for chunk in response.iter_bytes():
                        _remaining(deadline)
                        raw.extend(chunk)
                        if len(raw) > limit:
                            raise _failure("OUTPUT_LIMIT", "Response exceeds its byte limit.",
                                           phase="transport")
                    _remaining(deadline)
                    output.put((bytes(raw), response.headers, response.status_code))
            except ServiceClientError as exc:
                output.put(exc)
            except Exception:
                output.put(_failure("TRANSPORT_ERROR", "Local service request failed.",
                                    phase="transport", retryable=True))

        # Closing requests must also work after the normal thread gate is revoked.
        worker = threading.Thread(target=request, name="service-http", daemon=True)
        worker.start()
        try:
            result = output.get(timeout=_remaining(deadline))
        except queue.Empty:
            raise _failure("DEADLINE_EXCEEDED", "Total transport budget expired.",
                           phase="transport") from None
        if isinstance(result, ServiceClientError):
            raise result
        return result

    def _request(
        self, method: str, path: str, *, payload: dict[str, Any] | None = None,
        content: bytes | None = None, headers: dict[str, str] | None = None,
        identity: bool = False, allow_unready: bool = False, closing: bool = False,
        deadline: float | None = None,
    ) -> dict[str, Any]:
        raw, _, status = self._response(
            method, path, payload=payload, content=content, headers=headers, closing=closing,
            deadline=deadline,
        )
        try:
            data = decode(raw)
        except ServiceError:
            if not 200 <= status < 300:
                raise _failure("HTTP_ERROR", f"Service HTTP failure ({status}).", status=status,
                               retryable=status in (429, 502, 503, 504), phase="request") from None
            raise _failure("PROTOCOL_ERROR", "Invalid JSON service response.") from None
        if not 200 <= status < 300 and not (allow_unready and status == 503):
            if "error" not in data:
                raise _failure("HTTP_ERROR", f"Service HTTP failure ({status}).", status=status,
                               retryable=status in (429, 502, 503, 504), phase="request")
            raise _wire_error(data["error"], status=status, phase="request")
        if identity:
            self._check_identity(data)
        return data

    def _wait_ready(self) -> None:
        deadline = time.monotonic() + 120
        while time.monotonic() < deadline:
            if self._process is not None and self._process.poll() is not None:
                raise _failure("STARTUP_FAILED", "Owned service exited during startup.")
            try:
                health = self._request(
                    "GET", "/v1/health/ready", identity=True, allow_unready=True,
                    deadline=deadline,
                )
            except ServiceClientError as exc:
                if exc.code != "TRANSPORT_ERROR":
                    raise
            else:
                if health.get("state") == "failed":
                    error = health.get("error") or {}
                    code = error.get("code", "STARTUP_FAILED")
                    raise _failure(str(code)[:80], "Configured service profile failed to load.")
                if health.get("ready") is True and health.get("state") == "ready":
                    return
                if health.get("state") in ("draining", "stopped"):
                    raise _failure("NOT_READY", "Service is shutting down.")
            if self._closing.wait(min(0.1, max(0, deadline - time.monotonic()))):
                raise _failure("CLIENT_CLOSED", "Startup cancelled.")
        raise _failure("STARTUP_TIMEOUT", "Service readiness exceeded 120 seconds.")

    def capabilities(self) -> Capabilities:
        data = self._request("GET", "/v1/capabilities", identity=True)
        probe = data.get("probe")
        kinds = data.get("kinds")
        if not isinstance(probe, dict) or not isinstance(kinds, dict):
            raise _failure("PROTOCOL_ERROR", "Service capabilities are incomplete.")
        report = dict(probe)
        gpus = report.get("gpus", [])
        if (not isinstance(gpus, list) or any(
            not isinstance(gpu, dict) or not isinstance(gpu.get("total_gb"), (int, float))
            or isinstance(gpu.get("total_gb"), bool) or not 0 <= gpu["total_gb"] <= 1_000_000
            for gpu in gpus
        )):
            raise _failure("PROTOCOL_ERROR", "Invalid GPU capabilities.")
        for kind in kinds.values():
            if not isinstance(kind, dict) or kind.get("state") not in (
                "ready", "warming", "unsupported", "unprovisioned", "unavailable",
            ):
                raise _failure("PROTOCOL_ERROR", "Invalid modality capabilities.")
        tts, stt = kinds.get("tts", {}), kinds.get("stt", {})
        if (type(tts.get("sample_rate", 0)) is not int
                or not 0 <= tts.get("sample_rate", 0) <= 192000
                or type(tts.get("speaker_id", 0)) is not int
                or type(stt.get("partials", False)) is not bool):
            raise _failure("PROTOCOL_ERROR", "Invalid speech capabilities.")
        report["gpus"] = [SimpleNamespace(**gpu) for gpu in gpus]
        report["total_vram_gb"] = sum(gpu.total_gb for gpu in report["gpus"])
        report["has_gpu"] = bool(gpus)
        self.report = SimpleNamespace(**report)
        self.stt.is_streaming = bool(kinds.get("stt", {}).get("partials", False))
        self.tts.sample_rate = int(kinds.get("tts", {}).get("sample_rate", 0))
        self.tts.speaker_id = int(kinds.get("tts", {}).get("speaker_id", 0))
        return cast(Capabilities, data)

    def _submit(
        self, spec: dict[str, Any], *, deadline: float | None = None,
    ) -> dict[str, Any]:
        spec = {"deadline_ms": 120_000, "idempotency_key": str(uuid4()), **spec}
        try:
            JobSpec.parse(spec)
        except ServiceError as exc:
            raise _failure(exc.code, exc.message, phase="validation") from None
        data = self._request("POST", "/v1/jobs", payload=spec, identity=True, deadline=deadline)
        self._validate_snapshot(data)
        identifier = _identifier(data.get("job_id"))
        if data.get("session_id") != spec.get("session_id") or (
            data.get("turn_id") != spec.get("turn_id") or data.get("kind") != spec["kind"]
        ):
            raise _failure("PROTOCOL_ERROR", "Submitted job identity mismatch.")
        with self._lock:
            closing = self._closing.is_set()
            self._jobs.add(identifier)
        if closing:
            try:
                self._cancel(identifier, closing=True)
            finally:
                with self._lock:
                    self._jobs.discard(identifier)
            raise _failure("CLIENT_CLOSED", "Service client closed during job submission.")
        return data

    def _snapshot(
        self, identifier: str, *, closing: bool = False, deadline: float | None = None,
    ) -> dict[str, Any]:
        data = self._request("GET", f"/v1/jobs/{identifier}", identity=True, closing=closing,
                             deadline=deadline)
        self._validate_snapshot(data)
        if data.get("job_id") != identifier:
            raise _failure("PROTOCOL_ERROR", "Job snapshot identity mismatch.")
        return data

    def _cancel(
        self, identifier: str, *, closing: bool = False, deadline: float | None = None,
    ) -> dict[str, Any]:
        data = self._request("POST", f"/v1/jobs/{identifier}/cancel", closing=closing,
                             identity=True, deadline=deadline)
        self._validate_snapshot(data)
        if data.get("job_id") != identifier:
            raise _failure("PROTOCOL_ERROR", "Cancel snapshot identity mismatch.")
        return data

    def _cleanup_cancel(self, identifier: str, deadline: float) -> None:
        """Cleanup cannot extend a failed operation or replace its primary error."""
        def cancel() -> None:
            try:
                self._cancel(identifier, closing=True, deadline=time.monotonic() + 0.2)
            except OrchestratorError:
                pass

        if time.monotonic() >= deadline:
            threading.Thread(target=cancel, name="service-cancel-cleanup", daemon=True).start()
        else:
            try:
                self._cancel(identifier, closing=True,
                             deadline=min(deadline, time.monotonic() + 0.2))
            except OrchestratorError:
                pass

    @staticmethod
    def _validate_snapshot(data: dict[str, Any]) -> None:
        if (type(data.get("v")) is not int or data["v"] != PROTOCOL_VERSION
                or data.get("state") not in
                ("queued", "running", "waiting_tool", "succeeded", "failed", "cancelled")
                or type(data.get("revision")) is not int or data["revision"] < 0
                or type(data.get("host_pending")) is not bool):
            raise _failure("PROTOCOL_ERROR", "Invalid canonical job snapshot.")

    @staticmethod
    def _result(snapshot: dict[str, Any]) -> dict[str, Any]:
        if snapshot.get("state") != "succeeded":
            raise _wire_error(snapshot.get("error"), job_id=snapshot.get("job_id"))
        result = snapshot.get("result")
        if not isinstance(result, dict):
            raise _failure("PROTOCOL_ERROR", "Missing job result.")
        return result

    def _poll(
        self, job: dict[str, Any], *, deadline: float | None = None,
        cancellation: threading.Event | None = None, snapshot: bool = False,
        expected_kind: str | None = None,
    ) -> dict[str, Any]:
        identifier = job["job_id"]
        deadline = deadline if deadline is not None else time.monotonic() + 120
        try:
            while True:
                if expected_kind is not None and (
                    job.get("kind") != expected_kind or job.get("job_id") != identifier
                    or job.get("session_id") is not None or job.get("turn_id") is not None
                ):
                    raise _failure("PROTOCOL_ERROR", "Visual job identity mismatch.")
                if job.get("state") in TERMINAL:
                    return job if snapshot else self._result(job)
                if cancellation is not None and cancellation.is_set():
                    job = self._cancel(identifier, deadline=deadline)
                    continue
                if self._closing.is_set():
                    raise _failure("CLIENT_CLOSED", "Remote inference cancelled.")
                if time.monotonic() >= deadline:
                    raise _failure("DEADLINE_EXCEEDED", "Total job budget expired.",
                                   job_id=identifier, phase="inference")
                self._closing.wait(min(0.05, _remaining(deadline)))
                job = self._snapshot(identifier, deadline=deadline)
        except BaseException:
            if job.get("state") not in TERMINAL:
                self._cleanup_cancel(identifier, deadline)
            raise
        finally:
            with self._lock:
                self._jobs.discard(identifier)

    def chat(
        self, messages: list[Message], *, max_tokens: int = 512,
        temperature: float = 0.7, json_schema: dict[str, Any] | None = None,
        cancellation: threading.Event | None = None, timeout: float = 120,
    ) -> TextResult:
        _cancellation(cancellation)
        deadline = _deadline(timeout)
        job = self._submit({
            "kind": "text", "messages": messages,
            "max_tokens": max_tokens, "temperature": temperature,
            "json_schema": json_schema,
        }, deadline=deadline)
        try:
            final = self._poll(job, deadline=deadline, cancellation=cancellation, snapshot=True)
            return self._text_result(final)
        except ServiceClientError as exc:
            exc.job_id = exc.job_id or job["job_id"]
            exc.partial_result = exc.partial_result or self._text_result(job, allow_active=True)
            raise

    @staticmethod
    def _text_result(
        snapshot: dict[str, Any], *, text: str = "", local: tuple[ToolOutcome, ...] = (),
        allow_active: bool = False,
    ) -> TextResult:
        identifier = _identifier(snapshot.get("job_id"))
        turn = snapshot.get("turn_id")
        if turn is not None:
            turn = _identifier(turn)
        state = snapshot.get("state")
        if state not in TERMINAL:
            if not allow_active:
                raise _failure("PROTOCOL_ERROR", "Nonterminal text result.")
            state = "failed"  # Local failure, not a fabricated remote success/cancellation.
        result = snapshot.get("result")
        calls: list[ToolOutcome] = []
        executed = {call.dispatch_id: call for call in local}
        seen: set[str] = set()
        if result is not None:
            if not isinstance(result, dict) or not isinstance(result.get("text"), str):
                raise _failure("PROTOCOL_ERROR", "Invalid text result.")
            text = result["text"]
            records = result.get("calls")
            if not isinstance(records, list):
                raise _failure("PROTOCOL_ERROR", "Invalid tool outcomes.")
            for record in records:
                if not isinstance(record, dict):
                    raise _failure("PROTOCOL_ERROR", "Invalid tool outcome.")
                model_id, dispatch = record.get("id"), record.get("dispatch_id")
                if model_id is not None and (not isinstance(model_id, str) or not model_id):
                    raise _failure("PROTOCOL_ERROR", "Invalid model call identifier.")
                if dispatch is not None:
                    dispatch = _identifier(dispatch)
                    if dispatch in seen:
                        raise _failure("PROTOCOL_ERROR", "Duplicate dispatch outcome.")
                    seen.add(dispatch)
                outcome, delivered = record.get("outcome"), record.get("result_delivered")
                if (outcome not in ("rejected", "completed", "unknown")
                        or type(delivered) is not bool
                    or (delivered and outcome == "unknown")
                        or not isinstance(record.get("name"), str)
                        or not isinstance(record.get("arguments"), dict)
                        or "result" not in record):
                    raise _failure("PROTOCOL_ERROR", "Invalid tool outcome facts.")
                evidence = executed.get(dispatch)
                if evidence is not None and (
                    evidence.name != record["name"]
                    or evidence.arguments != freeze_json(record["arguments"])
                ):
                    raise _failure("PROTOCOL_ERROR", "Tool execution identity mismatch.")
                try:
                    encode(record["arguments"], TOOL_LIMIT)
                    encode(record["result"], TOOL_LIMIT)
                except ServiceError:
                    raise _failure("PROTOCOL_ERROR", "Invalid tool outcome JSON.") from None
                calls.append(ToolOutcome(
                    model_id, dispatch, record["name"], record["arguments"],
                    evidence.result if evidence and outcome == "unknown" else record["result"],
                    outcome, delivered, evidence is not None,
                ))
        elif state == "succeeded":
            raise _failure("PROTOCOL_ERROR", "Missing successful text result.")
        calls.extend(call for call in local if call.dispatch_id not in seen)
        partial = TextResult(state, text, identifier, turn, tuple(calls))
        if state == "failed" and not allow_active:
            raise _wire_error(snapshot.get("error"), job_id=identifier, partial_result=partial)
        return partial

    @staticmethod
    def _text(result: dict[str, Any]) -> str:
        text = result.get("text")
        if not isinstance(text, str):
            raise _failure("PROTOCOL_ERROR", "Missing text result.")
        return text

    def chat_with_tools(
        self, messages: list[Message], registry: ToolRegistry, *, max_tokens: int = 512,
        temperature: float = 0.7, max_rounds: int = 4,
        cancellation: threading.Event | None = None, timeout: float = 120,
    ) -> TextResult:
        return self.stream_chat_with_tools(
            messages, registry, max_tokens=max_tokens,
            temperature=temperature, max_rounds=max_rounds,
            cancellation=cancellation, timeout=timeout,
        )

    def stream_chat(
        self, messages: list[Message], *, max_tokens: int = 512,
        temperature: float = 0.7, json_schema: dict[str, Any] | None = None,
        on_text: Callable[[str], None] | None = None,
        cancellation: threading.Event | None = None, timeout: float = 120,
    ) -> TextResult:
        """Deliver deltas via caller-thread callback, return explicit final outcome."""
        _cancellation(cancellation)
        deadline = _deadline(timeout)
        session = self._request("POST", "/v1/sessions", payload={
            "tools": [],
        }, identity=True, deadline=deadline)
        session_id = _identifier(session.get("session_id"))
        turn = _Turn(self, session_id, str(uuid4()), None, [], on_text, cancellation, deadline)
        try:
            return turn.run(messages, max_tokens, temperature, json_schema=json_schema)
        except ServiceClientError as exc:
            turn.enrich_error(exc)
            raise
        finally:
            turn.close()
            try:
                self._request("DELETE", f"/v1/sessions/{session_id}", closing=True,
                              deadline=deadline)
            except OrchestratorError:
                pass

    def _tool_declarations(self, registry: ToolRegistry) -> list[dict[str, Any]]:
        from orchestrator.service_protocol import session_spec

        try:
            declarations = registry.specs()
        except Exception:
            raise _failure("CALLBACK_FAILED", "Tool declarations callback failed.",
                           phase="callback") from None
        try:
            session_spec({"tools": declarations})
        except ServiceError as exc:
            raise _failure(exc.code, exc.message) from None
        return declarations

    def stream_chat_with_tools(
        self, messages: list[Message], registry: ToolRegistry, *, max_tokens: int = 512,
        temperature: float = 0.7, max_rounds: int = 4,
        on_text: Callable[[str], None] | None = None,
        cancellation: threading.Event | None = None, timeout: float = 120,
    ) -> TextResult:
        _cancellation(cancellation)
        if max_rounds != 4:
            raise _failure("UNSUPPORTED_OPTION", "Service fixes tool turns at four rounds.")
        deadline = _deadline(timeout)
        declarations = self._tool_declarations(registry)
        names = [tool["function"]["name"] for tool in declarations]
        session = self._request("POST", "/v1/sessions", payload={
            "tools": declarations,
        }, identity=True, deadline=deadline)
        session_id = _identifier(session.get("session_id"))
        turn_id = str(uuid4())
        turn = _Turn(self, session_id, turn_id, registry, names, on_text, cancellation, deadline)
        try:
            return turn.run(messages, max_tokens, temperature)
        except ServiceClientError as exc:
            turn.enrich_error(exc)
            raise
        finally:
            turn.close()
            try:
                self._request("DELETE", f"/v1/sessions/{session_id}", closing=True,
                              deadline=deadline)
            except OrchestratorError:
                pass

    async def generate_image(
        self, text: str, *, seed: int, negative_prompt: str = "",
        deadline_ms: int = 600_000,
    ) -> VisualResult:
        """Generate an experimental PNG without blocking the event loop."""
        return await self._visual_async(
            {"kind": "image", "text": text, "seed": seed, "negative_prompt": negative_prompt,
             "deadline_ms": deadline_ms},
        )

    async def generate_3d(
        self, image: bytes | VisualResult, *, seed: int, deadline_ms: int = 600_000,
    ) -> VisualResult:
        """Generate a static GLB from PNG bytes or a retained PNG artifact."""
        return await self._visual_async(
            {"kind": "image_to_3d", "seed": seed, "deadline_ms": deadline_ms},
            image, "image/png",
        )

    async def animate_3d(
        self, mesh: bytes | VisualResult, *, seed: int, deadline_ms: int = 600_000,
    ) -> VisualResult:
        """Rig and animate GLB bytes or a retained GLB artifact."""
        return await self._visual_async(
            {"kind": "animate_3d", "seed": seed, "deadline_ms": deadline_ms},
            mesh, "model/gltf-binary",
        )

    async def _visual_async(
        self, spec: dict[str, Any], payload: bytes | VisualResult | None = None,
        input_media_type: str | None = None,
    ) -> VisualResult:
        cancellation = threading.Event()
        worker = asyncio.create_task(asyncio.to_thread(
            self._visual_job, spec, payload, input_media_type, cancellation,
        ))
        try:
            return await asyncio.shield(worker)
        except asyncio.CancelledError:
            # The worker still owns admission/polling and cleanup. In particular,
            # cancellation during POST must not abandon a subsequently admitted job.
            cancellation.set()
            worker.add_done_callback(lambda task: task.exception() if not task.cancelled() else None)
            raise

    @staticmethod
    def _validate_visual_payload(payload: bytes, media_type: str) -> None:
        from orchestrator.visual import validate_visual

        if not isinstance(payload, bytes) or not 0 < len(payload) <= _VISUAL_LIMIT:
            raise _failure("ARTIFACT_INVALID", "Expected 1..32 MiB of visual bytes.",
                           phase="validation")
        try:
            validate_visual(payload, media_type)
        except (OrchestratorError, ValueError):
            raise _failure("ARTIFACT_INVALID", "Invalid visual container.",
                           phase="validation") from None

    def _visual_job(
        self, spec: dict[str, Any], payload: bytes | VisualResult | None,
        input_media_type: str | None,
        cancellation: threading.Event,
    ) -> VisualResult:
        # Validate all arguments before upload. The placeholder is never sent.
        validation = dict(spec)
        if input_media_type is not None:
            validation["input_artifact_id"] = str(uuid4())
        try:
            JobSpec.parse(validation)
        except ServiceError as exc:
            raise _failure(exc.code, exc.message, phase="validation") from None
        deadline = time.monotonic() + spec["deadline_ms"] / 1000
        if cancellation.is_set():
            raise _failure("CANCELLED", "Visual operation cancelled.")
        uploaded: str | None = None
        if input_media_type is not None:
            if isinstance(payload, VisualResult):
                identifier = _identifier(payload.artifact_id)
                if (payload.media_type != input_media_type
                        or type(payload.byte_length) is not int
                        or payload.byte_length != len(payload.payload)
                        or payload.sha256 != hashlib.sha256(payload.payload).hexdigest()
                        or type(payload.seed) is not int or not 0 <= payload.seed < 2 ** 32
                        or payload.experimental is not True):
                    raise _failure("ARTIFACT_INVALID", "Invalid input artifact descriptor.",
                                   phase="validation")
                self._validate_visual_payload(payload.payload, input_media_type)
            else:
                self._validate_visual_payload(payload, input_media_type)
                artifact = self._request(
                    "POST", "/v1/artifacts", content=payload,
                    headers={"Content-Type": input_media_type}, deadline=deadline,
                )
                uploaded = identifier = _identifier(artifact.get("artifact_id"))
            spec = {**spec, "input_artifact_id": identifier}
        try:
            job = self._submit(spec, deadline=deadline)
        except ServiceClientError as exc:
            # As with STT, uncertain admission must leave the input to server TTL.
            if uploaded is not None and (
                exc.phase == "validation" or (exc.phase == "request"
                and exc.code not in ("HTTP_ERROR", "PROTOCOL_ERROR")
                and exc.status is not None and (400 <= exc.status < 500
                or (exc.status == 503 and exc.code == "NOT_READY")))
            ):
                self._visual_cleanup(uploaded, deadline)
            raise
        # Admission copies visual inputs into job-owned storage. Release only our
        # temporary upload now, even if the host is still running or polling fails.
        if uploaded is not None:
            self._visual_cleanup(uploaded, deadline)
        final = self._poll(
            job, deadline=deadline, cancellation=cancellation, snapshot=True,
            expected_kind=spec["kind"],
        )
        result = self._result(final)
        media_type = "image/png" if spec["kind"] == "image" else "model/gltf-binary"
        identifier = _identifier(result.get("artifact_id"))
        length, digest = result.get("byte_length"), result.get("sha256")
        if (result.get("media_type") != media_type or type(length) is not int
                or not 0 < length <= _VISUAL_LIMIT or not isinstance(digest, str)
                or len(digest) != 64 or any(char not in "0123456789abcdef" for char in digest)
                or type(result.get("seed")) is not int or result["seed"] != spec["seed"]
                or result.get("experimental") is not True):
            raise _failure("ARTIFACT_INVALID", "Invalid visual artifact descriptor.")
        self._request("GET", "/v1/health/live", identity=True, deadline=deadline)
        raw, headers, status = self._response(
            "GET", f"/v1/artifacts/{identifier}", limit=length, deadline=deadline,
        )
        self._request("GET", "/v1/health/live", identity=True, deadline=deadline)
        if (status != 200 or len(raw) != length or hashlib.sha256(raw).hexdigest() != digest
                or headers.get("x-content-sha256") != digest
                or headers.get("content-type") != media_type):
            raise _failure("ARTIFACT_INVALID", "Visual artifact integrity check failed.")
        self._validate_visual_payload(raw, media_type)
        # Unlike transient TTS audio, visual outputs are reusable stage artifacts.
        return VisualResult(identifier, media_type, length, digest, result["seed"], True, raw)

    def _visual_cleanup(self, identifier: str, deadline: float) -> None:
        try:
            self._request("DELETE", f"/v1/artifacts/{identifier}", deadline=deadline)
        except OrchestratorError:
            pass  # Retention TTL owns failed cleanup; preserve the primary outcome.

    def _synthesize_samples(self, sentence: str) -> tuple[np.ndarray, int]:
        """Whitespace-chunked TTS, bounded to 2400 characters / 120 seconds total."""
        if not isinstance(sentence, str) or not sentence.strip() or len(sentence) > 2400:
            raise _failure("INVALID_SPEC", "TTS requires 1..2400 characters.")
        chunks: list[str] = []
        remaining = sentence.strip()
        while len(remaining) > 300:
            cut = next((i for i in range(300, 0, -1) if remaining[i].isspace()), None)
            if cut is None:
                raise _failure("INVALID_SPEC", "TTS word exceeds 300 characters.")
            chunks.append(remaining[:cut].strip())
            remaining = remaining[cut:].lstrip()
        if remaining:
            chunks.append(remaining)
        deadline = time.monotonic() + 120
        audio: list[np.ndarray] = []
        total_samples = 0
        sample_rate = 0
        for chunk in chunks:
            if time.monotonic() >= deadline:
                raise _failure("DEADLINE_EXCEEDED", "Combined TTS exceeded 120 seconds.")
            result = self._poll(
                self._submit({"kind": "tts", "text": chunk}, deadline=deadline), deadline=deadline,
            )
            rate, count = result.get("sample_rate"), result.get("sample_count")
            if (type(rate) is not int or not 1 <= rate <= 192000
                    or type(count) is not int or not 0 <= count <= rate * 20
                    or (sample_rate and sample_rate != rate)):
                raise _failure("PROTOCOL_ERROR", "Invalid TTS artifact format.")
            identifier = _identifier(result.get("artifact_id"))
            self._request("GET", "/v1/health/live", identity=True, deadline=deadline)
            raw, headers, status = self._response(
                "GET", f"/v1/artifacts/{identifier}", limit=rate * 20 * 2, deadline=deadline,
            )
            self._request("GET", "/v1/health/live", identity=True, deadline=deadline)
            digest = hashlib.sha256(raw).hexdigest()
            if (status != 200 or len(raw) != count * 2 or digest != result.get("sha256")
                    or headers.get("x-content-sha256") != digest
                    or headers.get("x-sample-rate") != str(rate)
                    or headers.get("x-sample-count") != str(count)):
                raise _failure("ARTIFACT_INVALID", "TTS artifact integrity check failed.")
            try:
                self._request("DELETE", f"/v1/artifacts/{identifier}", deadline=deadline)
            except OrchestratorError:
                pass  # Verified audio remains usable; retention TTL owns failed cleanup.
            sample_rate = rate
            total_samples += count
            if total_samples > rate * 120:
                raise _failure("OUTPUT_LIMIT", "Combined TTS exceeds 120 seconds of audio.")
            audio.append(np.frombuffer(raw, dtype="<i2").astype(np.float32) / 32768)
        return np.concatenate(audio), sample_rate

    def transcribe_samples(self, samples: Any, *, rate: int = 16000) -> str:
        """Upload complete native 16 kHz mono audio; never resample live capture."""
        if rate != 16000:
            raise _failure("INVALID_PCM", "Live PCM must already be 16000 Hz.")
        try:
            audio = np.asarray(samples, dtype=np.float32)
        except (TypeError, ValueError, OverflowError):
            raise _failure("INVALID_PCM", "Expected finite mono audio samples.") from None
        if (audio.ndim != 1 or not 0 < audio.size <= PCM_LIMIT // 2
                or not np.isfinite(audio).all()):
            raise _failure("INVALID_PCM", "Expected at most 30 seconds of finite mono PCM.")
        pcm = np.rint(np.clip(audio, -1, 1) * 32767).astype("<i2").tobytes()
        deadline = time.monotonic() + 120
        artifact = self._request(
            "POST", "/v1/artifacts", content=pcm,
            headers={"Content-Type": "application/octet-stream", "X-Sample-Rate": "16000"},
            deadline=deadline,
        )
        identifier = _identifier(artifact.get("artifact_id"))
        try:
            job = self._submit({"kind": "stt", "input_artifact_id": identifier},
                               deadline=deadline)
        except ServiceClientError as exc:
            # Only a validated admission rejection proves no server owns the PCM.
                # NOT_READY is also a definite pre-admission rejection. Other 5xx,
                # transport/protocol failures may have accepted it: let TTL reclaim.
            if (exc.phase == "validation" or (exc.phase == "request"
                    and exc.code not in ("HTTP_ERROR", "PROTOCOL_ERROR")
                    and exc.status is not None and (400 <= exc.status < 500
                    or (exc.status == 503 and exc.code == "NOT_READY")))):
                try:
                    self._request("DELETE", f"/v1/artifacts/{identifier}", deadline=deadline)
                except OrchestratorError:
                    pass
            raise
        result = self._poll(job, deadline=deadline)
        return self._text(result)

    def transcribe(self, wav_path: Path | str, *, on_partial: Any = None) -> str:
        if on_partial is not None:
            raise _failure("UNSUPPORTED_OPTION", "Service does not emit STT partials.")
        try:
            with wave.open(str(Path(wav_path)), "rb") as source:
                rate, channels = source.getframerate(), source.getnchannels()
                frames = source.getnframes()
                if (source.getsampwidth() != 2 or source.getcomptype() != "NONE"
                        or not 1 <= channels <= 8 or not 1 <= rate <= 192000
                        or not 0 < frames <= rate * 30):
                    raise _failure("INVALID_PCM", "WAV must be PCM16 and at most 30 seconds.")
                raw = source.readframes(frames)
            if len(raw) != frames * channels * 2:
                raise _failure("INVALID_PCM", "WAV sample data is truncated.")
        except (OSError, wave.Error, EOFError):
            raise _failure("INVALID_PCM", "Cannot read PCM16 WAV input.") from None
        audio = np.frombuffer(raw, dtype="<i2").astype(np.float32).reshape(-1, channels)
        mono = audio.mean(axis=1) / 32768
        if rate != 16000:
            # Offline files only. Device-rate capture is the recorder's responsibility.
            count = max(1, round(len(mono) * 16000 / rate))
            mono = np.interp(np.arange(count) * rate / 16000, np.arange(len(mono)), mono)
        return self.transcribe_samples(mono)

    def listen_continuous(
        self, recorder: Any, *, on_block: Any = None, on_partial: Any = None,
    ) -> str:
        if on_partial is not None:
            raise _failure("UNSUPPORTED_OPTION", "Service does not emit STT partials.")
        if recorder.sample_rate != 16000:
            raise _failure("INVALID_PCM", "Recorder must capture model-rate 16000 Hz audio.")
        # ContinuousRecorder.record_utterance acknowledges at its local endpoint,
        # BEFORE decoding, preserving queued following speech. Do not acknowledge
        # again after remote inference: that would touch the next utterance.
        captured = recorder.record_utterance(on_block=on_block)
        return "" if captured is None else self.transcribe_samples(captured)

    def create_vad(self) -> Any:
        from orchestrator.vad import SileroVad

        if not self._vad_path.is_file():
            raise _failure("MODEL_NOT_PROVISIONED", "Local Silero VAD is not cached.")
        return SileroVad(self._vad_path)

    def _terminate_tree(self) -> None:
        process = self._process
        if self._windows_job is not None:
            self._windows_job.close()  # Includes llama descendants even after parent exit.
            self._windows_job = None
        elif process is not None and os.name != "nt":
            try:
                os.killpg(process.pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
            try:
                process.wait(timeout=0.5)
            except subprocess.TimeoutExpired:
                pass
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
        if process is not None:
            if process.poll() is None:
                process.kill()  # Also handles failure before Job Object assignment.
            try:
                process.wait(timeout=2)
            except subprocess.TimeoutExpired:
                pass

    def close(self) -> None:
        with self._lock:
            if self._closing.is_set():
                return
            self._closing.set()
            jobs, sockets = tuple(self._jobs), tuple(self._sockets)
        deadline = time.monotonic() + 15
        try:
            if self._http is not None:
                if self._owned:
                    try:
                        self._request("POST", "/v1/control/shutdown", closing=True, identity=True)
                    except OrchestratorError:
                        pass
                else:
                    for identifier in jobs:
                        if time.monotonic() >= deadline:
                            break
                        try:
                            self._cancel(identifier, closing=True)
                        except OrchestratorError:
                            pass
            for websocket in sockets:
                try:
                    websocket.close()
                except Exception:
                    pass
            if self._process is not None:
                try:
                    self._process.wait(timeout=max(0, deadline - time.monotonic()))
                except subprocess.TimeoutExpired:
                    pass
        finally:
            if self._owned:
                self._terminate_tree()
            with self._lock:
                threads = tuple(self._threads)
            for thread in threads:
                if thread is not threading.current_thread():
                    thread.join(timeout=max(0, deadline - time.monotonic()))
            if self._process is not None and self._process.stdout is not None:
                self._process.stdout.close()
            if self._http is not None:
                self._http.close()
            with self._lock:
                self._jobs.clear()
                self._sockets.clear()
                self._closed = True
            self._token = ""

    def stop(self) -> None:
        self.close()

    def __enter__(self) -> ServiceClient:
        return self

    def __exit__(self, *args: Any) -> None:
        self.close()


class _Turn:
    """Independent socket reader/heartbeat/cancellation; caller consumes tools."""

    def __init__(
        self, client: ServiceClient, session: str, turn: str, registry: ToolRegistry | None,
        names: list[str], on_text: Callable[[str], None] | None,
        cancellation: threading.Event | None, deadline: float,
    ) -> None:
        self.client, self.session, self.turn = client, session, turn
        self.registry, self.names = registry, names
        self.on_text, self.cancellation = on_text, cancellation
        self.socket: Any = None
        self.queue: queue.Queue[tuple[str, dict[str, Any]]] = queue.Queue(maxsize=64)
        self.lock = threading.RLock()
        self.send_lock = threading.Lock()
        self.done = threading.Event()
        self.hello = threading.Event()
        self.revoked = threading.Event()
        self.job_ready = threading.Event()
        self.job: str | None = None
        self.terminal: dict[str, Any] | None = None
        self.error: ServiceClientError | None = None
        self.sequence = 1
        self.deadline = deadline
        self.pending: dict[str, float] = {}
        self.seen: set[str] = set()
        self.threads: list[threading.Thread] = []
        self.text: list[str] = []
        self.calls: list[ToolOutcome] = []

    def enrich_error(self, error: ServiceClientError) -> None:
        error.job_id = error.job_id or self.job
        if self.job is not None and self.terminal is None:
            try:
                self._accept_terminal(self.client._snapshot(
                    self.job, closing=True,
                    deadline=min(self.deadline, time.monotonic() + 0.2),
                ))
            except OrchestratorError:
                pass
        if self.job is not None and error.partial_result is None:
            try:
                error.partial_result = self.client._text_result(
                    self.terminal or {
                        "job_id": self.job, "turn_id": self.turn, "state": "failed",
                    }, text="".join(self.text), local=tuple(self.calls), allow_active=True,
                )
            except OrchestratorError:
                # Invalid remote evidence must not replace the primary failure.
                error.partial_result = TextResult(
                    "failed", "".join(self.text), self.job, self.turn, tuple(self.calls),
                )

    def send(self, event: str, payload: dict[str, Any], *, job: bool = False) -> None:
        with self.send_lock:
            data = {
                "v": PROTOCOL_VERSION, "seq": self.sequence, "type": event,
                "service_instance_id": self.client._instance, "session_id": self.session,
                "job_id": self.job if job else None, "turn_id": self.turn if job else None,
                "payload": payload,
            }
            try:
                self.socket.send(encode(data).decode("utf-8"))
            except ServiceError:
                raise _failure("PROTOCOL_ERROR", "Invalid outgoing socket message.") from None
            except Exception:
                raise _failure("SOCKET_LOST", "Service socket send failed.") from None
            self.sequence += 1

    def fail(self, error: ServiceClientError) -> None:
        with self.lock:
            if self.error is None:
                self.error = error
            self.revoked.set()
            self.pending.clear()

    def _accept_terminal(self, snapshot: dict[str, Any]) -> None:
        with self.lock:
            if snapshot.get("state") in TERMINAL:
                self.terminal = snapshot
                self.pending.clear()
                if snapshot["state"] != "succeeded":
                    self.revoked.set()

    def receive(self) -> None:
        sequence = 1
        try:
            while not self.done.is_set():
                try:
                    raw = self.socket.recv(timeout=0.2)
                except TimeoutError:
                    continue
                if not isinstance(raw, str):
                    raise _failure("PROTOCOL_ERROR", "Binary socket messages are unsupported.")
                try:
                    data = decode(raw)
                    event, payload = envelope(data, sequence)
                except ServiceError:
                    raise _failure("PROTOCOL_ERROR", "Invalid socket envelope.") from None
                sequence += 1
                if (data.get("service_instance_id") != self.client._instance
                        or data.get("session_id") != self.session):
                    raise _failure("INSTANCE_CHANGED", "Socket service/session identity changed.")
                if not self.hello.is_set():
                    if event != "hello.accepted" or (
                        payload.get("protocol_version") != PROTOCOL_VERSION
                    ):
                        raise _failure("PROTOCOL_ERROR", "Socket negotiation failed.")
                    self.hello.set()
                    continue
                if event == "pong":
                    continue
                if event == "error" and data.get("job_id") is None:
                    raise _failure("PROTOCOL_ERROR", "Service rejected a socket operation.")
                if data.get("turn_id") != self.turn:
                    raise _failure("PROTOCOL_ERROR", "Socket turn identity mismatch.")
                identifier = _identifier(data.get("job_id"))
                with self.lock:
                    if self.job is None:
                        self.job = identifier  # Running event can beat POST response.
                    if self.job != identifier:
                        raise _failure("PROTOCOL_ERROR", "Socket job identity mismatch.")
                if event == "job.state":
                    if (payload.get("job_id") != identifier
                            or payload.get("turn_id") != self.turn
                            or payload.get("session_id") != self.session):
                        raise _failure("PROTOCOL_ERROR", "Socket snapshot identity mismatch.")
                    self.client._check_identity(payload)
                    self.client._validate_snapshot(payload)
                    self._accept_terminal(payload)
                    continue
                if event in ("text.end", "error"):
                    # Final snapshots, not text.end/error, own completion ordering.
                    continue
                if event not in ("text.delta", "tool.call"):
                    raise _failure("PROTOCOL_ERROR", "Unknown service socket event.")
                if self.revoked.is_set():
                    continue
                if event == "text.delta" and not isinstance(payload.get("text"), str):
                    raise _failure("PROTOCOL_ERROR", "Invalid text delta.")
                if event == "tool.call":
                    call_id = _identifier(payload.get("call_id"))
                    if (payload.get("name") not in self.names
                            or not isinstance(payload.get("arguments"), dict)
                            or payload.get("timeout_ms") != 5000):
                        raise _failure("PROTOCOL_ERROR", "Invalid remote tool call.")
                    with self.lock:
                        if call_id in self.seen or self.pending:
                            raise _failure("PROTOCOL_ERROR", "Duplicate or overlapping tool call.")
                        self.seen.add(call_id)
                        self.pending[call_id] = time.monotonic() + 5
                try:
                    self.queue.put_nowait((event, payload))
                except queue.Full:
                    # Never let a blocked callback suspend the socket reader.
                    self.fail(_failure("SLOW_CONSUMER", "Turn callback queue exceeded 64 events."))
        except ServiceClientError as exc:
            self.fail(exc)
        except Exception:
            if not self.done.is_set():
                self.fail(_failure("SOCKET_LOST", "Service socket reader disconnected."))
        finally:
            self.hello.set()  # Wake negotiation on failure too.

    def heartbeat(self) -> None:
        while not self.done.wait(5):
            try:
                self.send("ping", {})
            except ServiceClientError as exc:
                self.fail(exc)
                return

    def watch_cancel(self) -> None:
        while not self.done.wait(0.02):
            try:
                stopped = self.client._closing.is_set() or (
                    self.cancellation is not None and self.cancellation.is_set()
                )
                expired = time.monotonic() >= self.deadline
                if not (stopped or expired or self.error is not None):
                    continue
                with self.lock:
                    self.revoked.set()
                    self.pending.clear()
                if not self.job_ready.wait(0.02):
                    continue
                assert self.job is not None
                if expired:
                    self.fail(_failure("DEADLINE_EXCEEDED", "Total turn budget expired.",
                                       job_id=self.job, phase="inference"))
                    self.client._cleanup_cancel(self.job, self.deadline)
                    return
                snapshot = self.client._cancel(
                    self.job, closing=True, deadline=self.deadline,
                )
                self._accept_terminal(snapshot)
                return
            except ServiceClientError as exc:
                self.fail(exc)
                return
            except Exception:
                self.fail(_failure("TRANSPORT_ERROR", "Cancellation request failed."))
                return

    def _invoke(self, payload: dict[str, Any]) -> None:
        identifier = payload["call_id"]
        with self.lock:
            expires = self.pending.get(identifier, 0)
            if self.revoked.is_set() or time.monotonic() >= expires or self.terminal is not None:
                return
        # Read the authoritative job immediately before mutating client state.
        assert self.job is not None
        snapshot = self.client._snapshot(self.job, deadline=self.deadline)
        self._accept_terminal(snapshot)
        if snapshot.get("state") != "waiting_tool":
            with self.lock:
                self.pending.pop(identifier, None)
            return
        with self.lock:
            if (self.revoked.is_set() or time.monotonic() >= expires
                    or identifier not in self.pending):
                return
        if self.cancellation is not None and self.cancellation.is_set():
            self.revoked.set()
            return
        # Record execution evidence even if invocation raises after a side effect.
        # No model ID exists in tool.call. A socket send proves no model delivery.
        assert self.registry is not None
        arguments = freeze_json(payload["arguments"])
        call = ToolOutcome(None, identifier, payload["name"], arguments, None,
                           "unknown", False, True)
        self.calls.append(call)
        try:
            result = self.registry.invoke(payload["name"], dict(payload["arguments"]))
        except Exception:
            raise _failure("CALLBACK_FAILED", "Tool invocation failed.", phase="callback") from None
        try:
            encode(result, TOOL_LIMIT)
        except ServiceError:
            raise _failure("OUTPUT_LIMIT", "Client tool result exceeds its JSON limit.") from None
        self.calls[-1] = ToolOutcome(None, identifier, call.name, arguments, result,
                                     "unknown", False, True)
        with self.lock:
            if (self.revoked.is_set() or time.monotonic() >= expires
                or (self.cancellation is not None and self.cancellation.is_set())
                    or identifier not in self.pending or self.terminal is not None):
                return
            self.pending.pop(identifier)
            self.send("tool.result", {"call_id": identifier, "result": result}, job=True)

    def run(
        self, messages: list[Message], max_tokens: int, temperature: float,
        *, json_schema: dict[str, Any] | None = None,
    ) -> TextResult:
        url = self.client._url
        socket_url = ("wss" if url.startswith("https:") else "ws") + url[url.index(":"):]
        try:
            self.socket = connect(
                f"{socket_url}/v1/sessions/{self.session}/events",
                additional_headers={"Authorization": f"Bearer {self.client._token}"},
                open_timeout=min(5, _remaining(self.deadline)), close_timeout=1, max_size=JSON_LIMIT,
                max_queue=16, compression=None, proxy=None,
            )
        except ServiceClientError:
            raise
        except Exception:
            raise _failure("SOCKET_LOST", "Cannot connect service session socket.") from None
        with self.client._lock:
            if self.client._closing.is_set():
                raise _failure("CLIENT_CLOSED", "Service client is closing.")
            self.client._sockets.add(self.socket)
        self.threads.append(self.client._thread(self.receive, "service-events"))
        self.send("hello", {})
        if not self.hello.wait(min(5, _remaining(self.deadline))):
            raise _failure("SOCKET_LOST", "Socket negotiation timed out.")
        if self.error is not None:
            raise self.error
        self.threads.append(self.client._thread(self.heartbeat, "service-heartbeat"))
        self.threads.append(self.client._thread(self.watch_cancel, "service-cancel"))
        submitted = self.client._submit({
            "kind": "text", "session_id": self.session, "turn_id": self.turn,
            "messages": messages, "max_tokens": max_tokens,
            "temperature": temperature, "streaming": True,
            "json_schema": json_schema,
            "idempotency_key": self.turn,
        }, deadline=self.deadline)
        with self.lock:
            if self.job is not None and self.job != submitted["job_id"]:
                raise _failure("PROTOCOL_ERROR", "Submitted turn job changed.")
            self.job = submitted["job_id"]
        self.job_ready.set()
        while True:
            if self.error is not None:
                raise self.error
            with self.lock:
                terminal = self.terminal
            if terminal is not None and (self.revoked.is_set() or self.queue.empty()):
                return self.client._text_result(
                    terminal, text="".join(self.text), local=tuple(self.calls),
                )
            _remaining(self.deadline, "inference")
            try:
                event, payload = self.queue.get(timeout=0.05)
            except queue.Empty:
                continue
            if self.revoked.is_set():
                continue
            if event == "tool.call":
                self._invoke(payload)
            else:
                text = payload["text"]
                self.text.append(text)
                if self.on_text is not None:
                    try:
                        self.on_text(text)
                    except Exception:
                        raise _failure("CALLBACK_FAILED", "Text callback failed.",
                                       phase="callback") from None

    def close(self) -> None:
        self.revoked.set()
        if self.job_ready.is_set() and self.job is not None and self.terminal is None:
            self.client._cleanup_cancel(self.job, self.deadline)
        self.done.set()
        if self.socket is not None:
            closed = threading.Event()

            def close_socket() -> None:
                try:
                    self.socket.close()
                except Exception:
                    pass
                finally:
                    closed.set()

            threading.Thread(target=close_socket, name="service-socket-close", daemon=True).start()
            closed.wait(max(0, min(1, self.deadline - time.monotonic())))
            with self.client._lock:
                self.client._sockets.discard(self.socket)
        deadline = min(self.deadline, time.monotonic() + 1)
        for thread in self.threads:
            if thread is not threading.current_thread():
                thread.join(timeout=max(0, deadline - time.monotonic()))
        if self.job is not None:
            with self.client._lock:
                self.client._jobs.discard(self.job)