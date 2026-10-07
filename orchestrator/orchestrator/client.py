"""Synchronous, loopback-only inference facade for the cached service.

Inference belongs to the service; device I/O, VAD and authoritative tools stay
with the caller. No provisioning or gameplay model selection happens here.
An attached service is never shut down or process-terminated by this client.
"""

from __future__ import annotations

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
from typing import Any, Callable
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
from orchestrator.tools import ToolCall


class ServiceClientError(OrchestratorError):
    """Concise transport/protocol failure, with a machine-readable code."""

    def __init__(self, code: str, message: str) -> None:
        self.code = code
        super().__init__(f"{code}: {message}")


def _failure(code: str, message: str) -> ServiceClientError:
    return ServiceClientError(code, message)


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
    """Owned service by default, or attach with ``url`` and bearer ``token``.

    Constructor model overrides are service startup policy only. Tool turns use
    the backend's fixed four-round policy. STT uploads at most 30 seconds of
    mono 16 kHz PCM; the streaming capability describes the server's recognizer,
    not partial network transcripts. Callbacks run synchronously on the caller.
    """

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
    ) -> None:
        if profile not in ("voice", "text"):
            raise _failure("INVALID_CONFIG", "Profile must be voice or text.")
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
                    text_model, tts_model, stt_model, speaker_id,
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
    ) -> dict[str, Any]:
        command = [sys.executable, "-m", "orchestrator.service", "--profile", profile]
        for flag, value in (
            ("--text-model", text_model), ("--tts-model", tts_model),
            ("--stt-model", stt_model), ("--speaker-id", speaker_id),
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
        limit: int = JSON_LIMIT, closing: bool = False,
    ) -> tuple[bytes, httpx.Headers, int]:
        if self._http is None or (self._closing.is_set() and not closing):
            raise _failure("CLIENT_CLOSED", "Service client is closed.")
        try:
            if payload is not None:
                content = encode(payload)
                headers = {**(headers or {}), "Content-Type": "application/json"}
            with self._http.stream(method, path, content=content, headers=headers) as response:
                raw = bytearray()
                for chunk in response.iter_bytes():
                    raw.extend(chunk)
                    if len(raw) > limit:
                        raise _failure("OUTPUT_LIMIT", "Service response exceeds its byte limit.")
                return bytes(raw), response.headers, response.status_code
        except ServiceError as exc:
            raise _failure(exc.code, exc.message) from None
        except (httpx.HTTPError, RuntimeError, OSError):
            raise _failure("TRANSPORT_ERROR", "Local service request failed.") from None

    def _request(
        self, method: str, path: str, *, payload: dict[str, Any] | None = None,
        content: bytes | None = None, headers: dict[str, str] | None = None,
        identity: bool = False, allow_unready: bool = False, closing: bool = False,
    ) -> dict[str, Any]:
        raw, _, status = self._response(
            method, path, payload=payload, content=content, headers=headers, closing=closing,
        )
        try:
            data = decode(raw)
        except ServiceError:
            raise _failure("PROTOCOL_ERROR", "Invalid JSON service response.") from None
        if not 200 <= status < 300 and not (allow_unready and status == 503):
            error = data.get("error")
            code = error.get("code") if isinstance(error, dict) else None
            if not isinstance(code, str) or len(code) > 80:
                code = "REQUEST_FAILED"
            # Never surface arbitrary server/transport text containing secrets or prompts.
            raise _failure(code, f"Service rejected the request (HTTP {status}).")
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

    def capabilities(self) -> dict[str, Any]:
        data = self._request("GET", "/v1/capabilities", identity=True)
        probe = data.get("probe")
        kinds = data.get("kinds")
        if not isinstance(probe, dict) or not isinstance(kinds, dict):
            raise _failure("PROTOCOL_ERROR", "Service capabilities are incomplete.")
        report = dict(probe)
        gpus = report.get("gpus", [])
        report["gpus"] = [SimpleNamespace(**gpu) for gpu in gpus]
        report["total_vram_gb"] = sum(gpu.total_gb for gpu in report["gpus"])
        report["has_gpu"] = bool(gpus)
        self.report = SimpleNamespace(**report)
        self.stt.is_streaming = bool(kinds.get("stt", {}).get("partials", False))
        self.tts.sample_rate = int(kinds.get("tts", {}).get("sample_rate", 0))
        self.tts.speaker_id = int(kinds.get("tts", {}).get("speaker_id", 0))
        return data

    def _submit(self, spec: dict[str, Any]) -> dict[str, Any]:
        spec = {"deadline_ms": 120_000, "idempotency_key": str(uuid4()), **spec}
        try:
            JobSpec.parse(spec)
        except ServiceError as exc:
            raise _failure(exc.code, exc.message) from None
        data = self._request("POST", "/v1/jobs", payload=spec, identity=True)
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

    def _snapshot(self, identifier: str, *, closing: bool = False) -> dict[str, Any]:
        data = self._request("GET", f"/v1/jobs/{identifier}", identity=True, closing=closing)
        if data.get("job_id") != identifier:
            raise _failure("PROTOCOL_ERROR", "Job snapshot identity mismatch.")
        return data

    def _cancel(self, identifier: str, *, closing: bool = False) -> dict[str, Any]:
        # Cancel responses omit instance identity; confirm it with the subsequent read.
        self._request("POST", f"/v1/jobs/{identifier}/cancel", closing=closing)
        return self._snapshot(identifier, closing=closing)

    @staticmethod
    def _result(snapshot: dict[str, Any]) -> dict[str, Any]:
        if snapshot.get("state") != "succeeded":
            error = snapshot.get("error") or {}
            code = error.get("code", "JOB_FAILED") if isinstance(error, dict) else "JOB_FAILED"
            raise _failure(str(code)[:80], "Remote inference did not complete.")
        result = snapshot.get("result")
        if not isinstance(result, dict):
            raise _failure("PROTOCOL_ERROR", "Missing job result.")
        return result

    def _poll(self, job: dict[str, Any], *, deadline: float | None = None) -> dict[str, Any]:
        identifier = job["job_id"]
        deadline = deadline if deadline is not None else time.monotonic() + 120
        try:
            while True:
                if job.get("state") in TERMINAL:
                    return self._result(job)
                if self._closing.is_set():
                    raise _failure("CLIENT_CLOSED", "Remote inference cancelled.")
                if time.monotonic() >= deadline:
                    self._cancel(identifier)
                    raise _failure("DEADLINE_EXCEEDED", "Remote job exceeded 120 seconds.")
                self._closing.wait(0.05)
                job = self._snapshot(identifier)
        except BaseException:
            if job.get("state") not in TERMINAL:
                try:
                    self._cancel(identifier, closing=True)
                except OrchestratorError:
                    pass
            raise
        finally:
            with self._lock:
                self._jobs.discard(identifier)

    def chat(
        self, messages: list[dict[str, str]], *, max_tokens: int = 512,
        temperature: float = 0.7, json_schema: dict[str, Any] | None = None,
    ) -> str:
        result = self._poll(self._submit({
            "kind": "text", "messages": messages,
            "max_tokens": max_tokens, "temperature": temperature,
            "json_schema": json_schema,
        }))
        return self._text(result)

    @staticmethod
    def _text(result: dict[str, Any]) -> str:
        text = result.get("text")
        if not isinstance(text, str):
            raise _failure("PROTOCOL_ERROR", "Missing text result.")
        return text

    def chat_with_tools(
        self, messages: list[dict[str, str]], registry: Any, *, max_tokens: int = 512,
        temperature: float = 0.7, max_rounds: int = 4,
    ) -> tuple[str, list[ToolCall]]:
        return self.stream_chat_with_tools(
            messages, registry, max_tokens=max_tokens,
            temperature=temperature, max_rounds=max_rounds,
        )

    def stream_chat(
        self, messages: list[dict[str, str]], *, max_tokens: int = 512,
        temperature: float = 0.7, json_schema: dict[str, Any] | None = None,
        on_text: Callable[[str], None] | None = None,
        should_stop: Callable[[], bool] | None = None,
    ) -> str:
        """Stream text or constrained JSON over a cancellable service session.

        Callbacks must only hand off work; never wait for TTS or playback here.
        A cancelled call returns partial text, which must not be used as actions.
        """
        session = self._request("POST", "/v1/sessions", payload={
            "tools": [], "delivery_mode": "text",
        }, identity=True)
        session_id = _identifier(session.get("session_id"))
        turn = _Turn(self, session_id, str(uuid4()), None, [], on_text, should_stop)
        try:
            text, _ = turn.run(messages, max_tokens, temperature, json_schema=json_schema,
                               kind="text")
            return text
        finally:
            turn.close()
            try:
                self._request("DELETE", f"/v1/sessions/{session_id}", closing=True)
            except OrchestratorError:
                pass

    def _tool_names(self, registry: Any) -> list[str]:
        from orchestrator.service_protocol import session_spec

        declarations = registry.specs()
        try:
            session_spec({"tools": declarations})
        except ServiceError as exc:
            raise _failure(exc.code, exc.message) from None
        return [tool["function"]["name"] for tool in declarations]

    def stream_chat_with_tools(
        self, messages: list[dict[str, str]], registry: Any, *, max_tokens: int = 512,
        temperature: float = 0.7, max_rounds: int = 4,
        on_text: Callable[[str], None] | None = None,
        should_stop: Callable[[], bool] | None = None,
    ) -> tuple[str, list[ToolCall]]:
        if max_rounds != 4:
            raise _failure("UNSUPPORTED_OPTION", "Service fixes tool turns at four rounds.")
        names = self._tool_names(registry)
        session = self._request("POST", "/v1/sessions", payload={
            "tools": registry.specs(), "delivery_mode": "text",
        }, identity=True)
        session_id = _identifier(session.get("session_id"))
        turn_id = str(uuid4())
        turn = _Turn(self, session_id, turn_id, registry, names, on_text, should_stop)
        try:
            return turn.run(messages, max_tokens, temperature)
        finally:
            turn.close()
            try:
                self._request("DELETE", f"/v1/sessions/{session_id}", closing=True)
            except OrchestratorError:
                pass

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
            result = self._poll(self._submit({"kind": "tts", "text": chunk}), deadline=deadline)
            rate, count = result.get("sample_rate"), result.get("sample_count")
            if (type(rate) is not int or not 1 <= rate <= 192000
                    or type(count) is not int or not 0 <= count <= rate * 20
                    or (sample_rate and sample_rate != rate)):
                raise _failure("PROTOCOL_ERROR", "Invalid TTS artifact format.")
            identifier = _identifier(result.get("artifact_id"))
            self._request("GET", "/v1/health/live", identity=True)
            raw, headers, status = self._response(
                "GET", f"/v1/artifacts/{identifier}", limit=rate * 20 * 2,
            )
            self._request("GET", "/v1/health/live", identity=True)
            digest = hashlib.sha256(raw).hexdigest()
            if (status != 200 or len(raw) != count * 2 or digest != result.get("sha256")
                    or headers.get("x-content-sha256") != digest
                    or headers.get("x-sample-rate") != str(rate)
                    or headers.get("x-sample-count") != str(count)):
                raise _failure("ARTIFACT_INVALID", "TTS artifact integrity check failed.")
            self._request("DELETE", f"/v1/artifacts/{identifier}")
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
        artifact = self._request(
            "POST", "/v1/artifacts", content=pcm,
            headers={"Content-Type": "application/octet-stream", "X-Sample-Rate": "16000"},
        )
        result = self._poll(self._submit({
            "kind": "stt", "input_artifact_id": _identifier(artifact.get("artifact_id")),
        }))
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
        self, client: ServiceClient, session: str, turn: str, registry: Any,
        names: list[str], on_text: Any, should_stop: Any,
    ) -> None:
        self.client, self.session, self.turn = client, session, turn
        self.registry, self.names = registry, names
        self.on_text, self.should_stop = on_text, should_stop
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
        self.deadline = time.monotonic() + 120
        self.pending: dict[str, float] = {}
        self.seen: set[str] = set()
        self.threads: list[threading.Thread] = []
        self.text: list[str] = []
        self.calls: list[ToolCall] = []

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
                    self.should_stop is not None and self.should_stop()
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
                snapshot = self.client._cancel(self.job, closing=True)
                self._accept_terminal(snapshot)
                if expired:
                    self.fail(_failure("DEADLINE_EXCEEDED", "Remote turn exceeded 120 seconds."))
                return
            except ServiceClientError as exc:
                self.fail(exc)
                return
            except Exception:
                self.fail(_failure("CALLBACK_FAILED", "Cancellation callback failed."))
                return

    def _invoke(self, payload: dict[str, Any]) -> None:
        identifier = payload["call_id"]
        with self.lock:
            expires = self.pending.get(identifier, 0)
            if self.revoked.is_set() or time.monotonic() >= expires or self.terminal is not None:
                return
        # Read the authoritative job immediately before mutating client state.
        assert self.job is not None
        snapshot = self.client._snapshot(self.job)
        if snapshot.get("state") != "waiting_tool":
            with self.lock:
                self.pending.pop(identifier, None)
            return
        with self.lock:
            if (self.revoked.is_set() or time.monotonic() >= expires
                    or identifier not in self.pending):
                return
        if self.should_stop is not None and self.should_stop():
            self.revoked.set()
            return
        call = ToolCall(identifier, payload["name"], payload["arguments"])
        # Registry.invoke (experiment authority) runs on the consumer, never reader.
        call.result = self.registry.invoke(call.name, call.arguments)
        self.calls.append(call)
        try:
            encode(call.result, TOOL_LIMIT)
        except ServiceError:
            raise _failure("OUTPUT_LIMIT", "Client tool result exceeds its JSON limit.") from None
        with self.lock:
            if (self.revoked.is_set() or time.monotonic() >= expires
                    or identifier not in self.pending or self.terminal is not None):
                return
            self.pending.pop(identifier)
            self.send("tool.result", {"call_id": identifier, "result": call.result}, job=True)

    def run(
        self, messages: list[dict[str, str]], max_tokens: int, temperature: float,
        *, json_schema: dict[str, Any] | None = None, kind: str = "dialogue",
    ) -> tuple[str, list[ToolCall]]:
        url = self.client._url
        socket_url = ("wss" if url.startswith("https:") else "ws") + url[url.index(":"):]
        try:
            self.socket = connect(
                f"{socket_url}/v1/sessions/{self.session}/events",
                additional_headers={"Authorization": f"Bearer {self.client._token}"},
                open_timeout=5, close_timeout=1, max_size=JSON_LIMIT,
                max_queue=16, compression=None, proxy=None,
            )
        except Exception:
            raise _failure("SOCKET_LOST", "Cannot connect service session socket.") from None
        with self.client._lock:
            if self.client._closing.is_set():
                raise _failure("CLIENT_CLOSED", "Service client is closing.")
            self.client._sockets.add(self.socket)
        self.threads.append(self.client._thread(self.receive, "service-events"))
        self.send("hello", {})
        if not self.hello.wait(5):
            raise _failure("SOCKET_LOST", "Socket negotiation timed out.")
        if self.error is not None:
            raise self.error
        self.threads.append(self.client._thread(self.heartbeat, "service-heartbeat"))
        self.threads.append(self.client._thread(self.watch_cancel, "service-cancel"))
        submitted = self.client._submit({
            "kind": kind, "session_id": self.session, "turn_id": self.turn,
            "messages": messages, "max_tokens": max_tokens,
            "temperature": temperature, "streaming": True,
            "json_schema": json_schema,
            "idempotency_key": self.turn,
        })
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
                if terminal.get("state") == "cancelled":
                    return "".join(self.text), self.calls
                result = self.client._result(terminal)
                if self.revoked.is_set():
                    return "".join(self.text), self.calls
                return self.client._text(result), self.calls
            if time.monotonic() >= self.deadline + 10:
                raise _failure("DEADLINE_EXCEEDED", "Cancelled turn snapshot timed out.")
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
                        raise _failure("CALLBACK_FAILED", "Text callback failed.") from None

    def close(self) -> None:
        self.revoked.set()
        if self.job_ready.is_set() and self.job is not None and self.terminal is None:
            try:
                self._accept_terminal(self.client._cancel(self.job, closing=True))
            except OrchestratorError:
                pass
        self.done.set()
        if self.socket is not None:
            try:
                self.socket.close()
            except Exception:
                pass
            with self.client._lock:
                self.client._sockets.discard(self.socket)
        deadline = time.monotonic() + 6
        for thread in self.threads:
            if thread is not threading.current_thread():
                thread.join(timeout=max(0, deadline - time.monotonic()))
        if self.job is not None:
            with self.client._lock:
                self.client._jobs.discard(self.job)