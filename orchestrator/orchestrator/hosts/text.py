"""Text host: drives an OpenAI-compatible chat endpoint.

The design doc standardises on **llama.cpp ``llama-server``** (router mode) and
keeps Ollama as an optional convenience front-end (exp. 0101). Both speak the
same ``/v1/chat/completions`` API, so this host is written against that
interface and does not care which one is behind it.

The host owns the server process lifecycle: it downloads the llama.cpp binary
and the GGUF on demand, starts ``llama-server``, waits for ``/health``, and
stops it on unload. Structured output uses ``response_format`` with a JSON
schema, because LLM output is untrusted (Golden Rule 5).
"""

from __future__ import annotations

import http.client
import json
import shutil
import socket
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
import zipfile
from contextlib import closing
from pathlib import Path
from typing import Any, Callable, Iterator

from orchestrator.catalog import ModelDescriptor
from orchestrator.hosts.base import HostError
from orchestrator.paths import model_store_dir
from orchestrator.store import InstalledModel

#: Pinned llama.cpp release. Bump deliberately; the binary is ~250 MB (CUDA).
LLAMA_CPP_BUILD = "b11284"
LLAMA_CPP_ASSET = f"llama-{LLAMA_CPP_BUILD}-bin-win-cuda-12.4-x64.zip"
LLAMA_CPP_URL = (
    "https://github.com/ggml-org/llama.cpp/releases/download/"
    f"{LLAMA_CPP_BUILD}/{LLAMA_CPP_ASSET}"
)
LLAMA_CPP_CUDA_RUNTIME = "cudart-llama-bin-win-cuda-12.4-x64.zip"
LLAMA_CPP_CUDA_URL = (
    "https://github.com/ggml-org/llama.cpp/releases/download/"
    f"{LLAMA_CPP_BUILD}/{LLAMA_CPP_CUDA_RUNTIME}"
)


class TextHost:
    """Runs a GGUF chat model behind ``llama-server``."""

    modality = "text"

    def __init__(
        self,
        *,
        port: int = 8080,
        base_url: str | None = None,
        n_gpu_layers: int = 99,
        context: int = 4096,
        startup_timeout_s: float = 180.0,
        binary_dir: Path | None = None,
    ) -> None:
        #: When ``base_url`` is set the host talks to an *external* server
        #: (e.g. a running Ollama) and never spawns a process.
        self.base_url = base_url.rstrip("/") if base_url else None
        self.port = port
        self.n_gpu_layers = n_gpu_layers
        self.context = context
        self.startup_timeout_s = startup_timeout_s
        self.binary_dir = binary_dir or (model_store_dir() / "llama.cpp" / LLAMA_CPP_BUILD)
        self._process: subprocess.Popen | None = None
        self._model: InstalledModel | None = None

    # -- lifecycle ---------------------------------------------------------

    @property
    def loaded_model_id(self) -> str | None:
        return self._model.descriptor.id if self._model else None

    def load(self, model: InstalledModel) -> None:
        if self._model and self._model.descriptor.id == model.descriptor.id:
            return
        self.unload()
        self._model = model
        if self.base_url is not None:
            return  # external server; nothing to start
        self._start_server(model)

    def unload(self) -> None:
        if self._process is not None:
            self._process.terminate()
            try:
                self._process.wait(timeout=15)
            except subprocess.TimeoutExpired:
                self._process.kill()
                self._process.wait(timeout=5)
            self._process = None
        self._model = None

    def __del__(self) -> None:
        """Safety net: never leave a llama-server holding VRAM."""
        try:
            self.unload()
        except Exception:  # noqa: BLE001 - interpreter shutdown
            pass

    # -- inference ---------------------------------------------------------

    def _payload(
        self,
        messages: list[dict[str, str]],
        *,
        max_tokens: int,
        temperature: float,
        stream: bool = False,
    ) -> dict[str, Any]:
        """Build a completion payload, honouring per-model parameters.

        Reasoning models (Granite 4.2, Qwen3) spend their whole token budget on
        hidden reasoning and return empty content unless thinking is disabled.
        The catalog records ``enable_thinking: false`` for those models.
        """
        payload: dict[str, Any] = {
            "model": self._model_name(),
            "messages": messages,
            "max_tokens": max_tokens,
            "temperature": temperature,
            "stream": stream,
        }
        if self._model is not None:
            params = self._model.descriptor.params
            if params.get("enable_thinking") is False:
                payload["chat_template_kwargs"] = {"enable_thinking": False}
        return payload

    def chat(
        self,
        messages: list[dict[str, str]],
        *,
        json_schema: dict[str, Any] | None = None,
        max_tokens: int = 512,
        temperature: float = 0.7,
    ) -> str:
        """One non-streaming completion. Returns the assistant text."""
        payload = self._payload(
            messages, max_tokens=max_tokens, temperature=temperature
        )
        if json_schema is not None:
            payload["response_format"] = {
                "type": "json_schema",
                "json_schema": {"name": "response", "schema": json_schema, "strict": True},
            }
        data = self._post("/v1/chat/completions", payload)
        try:
            return data["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError) as exc:
            raise HostError(f"unexpected completion payload: {data!r}") from exc

    def chat_with_tools(
        self,
        messages: list[dict[str, str]],
        registry: Any,
        *,
        max_tokens: int = 512,
        temperature: float = 0.7,
        max_rounds: int = 4,
    ) -> tuple[str, list[Any]]:
        """Run a completion, executing any tool calls the model requests.

        Returns ``(final_text, calls)``. The loop is bounded by ``max_rounds``
        so a model that keeps calling tools cannot spin forever.
        """
        from orchestrator.tools import ToolCall  # noqa: PLC0415

        conversation = list(messages)
        calls: list[ToolCall] = []
        tools = registry.specs()

        for _ in range(max_rounds):
            payload = self._payload(
                conversation, max_tokens=max_tokens, temperature=temperature
            )
            if tools:
                payload["tools"] = tools
                payload["tool_choice"] = "auto"

            data = self._post("/v1/chat/completions", payload)
            try:
                message = data["choices"][0]["message"]
            except (KeyError, IndexError, TypeError) as exc:
                raise HostError(f"unexpected completion payload: {data!r}") from exc

            requested = message.get("tool_calls") or []
            if not requested:
                return message.get("content") or "", calls

            conversation.append(message)
            for raw in requested:
                function = raw.get("function") or {}
                name = function.get("name", "")
                try:
                    arguments = json.loads(function.get("arguments") or "{}")
                except json.JSONDecodeError:
                    arguments = {}
                result = registry.invoke(name, arguments)
                call = ToolCall(id=raw.get("id", ""), name=name, arguments=arguments, result=result)
                calls.append(call)
                conversation.append(
                    {
                        "role": "tool",
                        "tool_call_id": call.id,
                        "content": json.dumps(result),
                    }
                )

        # Out of rounds: ask once more without tools so we always get text.
        payload = self._payload(conversation, max_tokens=max_tokens, temperature=temperature)
        data = self._post("/v1/chat/completions", payload)
        try:
            return data["choices"][0]["message"].get("content") or "", calls
        except (KeyError, IndexError, TypeError) as exc:
            raise HostError(f"unexpected completion payload: {data!r}") from exc

    def stream_chat(
        self,
        messages: list[dict[str, str]],
        *,
        max_tokens: int = 512,
        temperature: float = 0.7,
        json_schema: dict[str, Any] | None = None,
        should_stop: Callable[[], bool] | None = None,
    ) -> Iterator[str]:
        """Stream plain or schema-constrained text, with interruptible body reads."""
        payload = self._payload(
            messages, max_tokens=max_tokens, temperature=temperature, stream=True
        )
        if json_schema is not None:
            payload["response_format"] = {
                "type": "json_schema",
                "json_schema": {"name": "response", "schema": json_schema, "strict": True},
            }
        stream = (self._sse_chunks(payload, should_stop=should_stop)
                  if should_stop is not None else self._sse_chunks(payload))
        with closing(stream):
            for chunk in stream:
                if should_stop is not None and should_stop():
                    return
                try:
                    delta = chunk["choices"][0]["delta"].get("content")
                except (KeyError, IndexError, TypeError):
                    continue
                if delta:
                    yield delta

    def stream_chat_with_tools(
        self,
        messages: list[dict[str, str]],
        registry: Any,
        *,
        max_tokens: int = 512,
        temperature: float = 0.7,
        max_rounds: int = 4,
        on_text: Any = None,
        should_stop: Callable[[], bool] | None = None,
    ) -> tuple[str, list[Any]]:
        """Stream a completion, executing tool calls, reporting text as it arrives.

        ``on_text(chunk)`` is called for every text delta, so the caller can
        speak while the model is still writing. ``should_stop()`` is polled
        between deltas and by a transport watcher while reads stall; when it
        returns True the stream is abandoned and the
        text received so far is returned. That is what makes barge-in possible:
        the caller stops generation the moment the player speaks.

        The stop callback must be thread-safe and non-blocking. Cancellation
        interrupts response-body reads, not urllib's connection/header setup
        or a tool invocation already in progress.

        Tool calls arrive as deltas too (``function.arguments`` is a partial
        string), so they are accumulated per index and executed once the round
        finishes. Returns ``(full_text, calls)``.
        """
        from orchestrator.tools import ToolCall  # noqa: PLC0415

        conversation = list(messages)
        calls: list[ToolCall] = []
        tools = registry.specs()
        full_text = ""
        stopped = threading.Event()

        def stop_requested() -> bool:
            if not stopped.is_set() and should_stop is not None and should_stop():
                stopped.set()
            return stopped.is_set()

        def chunks(payload: dict[str, Any]) -> Iterator[dict[str, Any]]:
            if stop_requested():
                return
            # Keep the one-argument fake/override interface when no stop callback is used.
            stream = (
                self._sse_chunks(payload, should_stop=stop_requested)
                if should_stop is not None else self._sse_chunks(payload)
            )
            with closing(stream):
                try:
                    yield from stream
                except (HostError, OSError, http.client.HTTPException, ValueError):
                    if not stop_requested():
                        raise

        for _ in range(max_rounds):
            if stop_requested():
                return full_text, calls
            payload = self._payload(
                conversation, max_tokens=max_tokens, temperature=temperature, stream=True
            )
            if tools:
                payload["tools"] = tools
                payload["tool_choice"] = "auto"

            text_parts: list[str] = []
            tool_buf: dict[int, dict[str, str]] = {}
            with closing(chunks(payload)) as stream:
                for chunk in stream:
                    if stop_requested():
                        break
                    try:
                        delta = chunk["choices"][0].get("delta") or {}
                    except (KeyError, IndexError, TypeError):
                        continue
                    content = delta.get("content")
                    if content:
                        text_parts.append(content)
                        if on_text is not None:
                            on_text(content)
                    for raw in delta.get("tool_calls") or []:
                        index = raw.get("index", 0)
                        slot = tool_buf.setdefault(
                            index, {"id": "", "name": "", "arguments": ""}
                        )
                        if raw.get("id"):
                            slot["id"] = raw["id"]
                        function = raw.get("function") or {}
                        if function.get("name"):
                            slot["name"] = function["name"]
                        if function.get("arguments"):
                            slot["arguments"] += function["arguments"]

            text = "".join(text_parts)
            full_text += text
            if stop_requested() or not tool_buf:
                return full_text, calls

            # Execute the round's tool calls, then let the model continue.
            conversation.append(
                {
                    "role": "assistant",
                    "content": text or None,
                    "tool_calls": [
                        {
                            "id": tool_buf[i]["id"],
                            "type": "function",
                            "function": {
                                "name": tool_buf[i]["name"],
                                "arguments": tool_buf[i]["arguments"],
                            },
                        }
                        for i in sorted(tool_buf)
                    ],
                }
            )
            for index in sorted(tool_buf):
                slot = tool_buf[index]
                try:
                    arguments = json.loads(slot["arguments"] or "{}")
                except json.JSONDecodeError:
                    arguments = {}
                if stop_requested():
                    return full_text, calls
                result = registry.invoke(slot["name"], arguments)
                call = ToolCall(
                    id=slot["id"], name=slot["name"], arguments=arguments, result=result
                )
                calls.append(call)
                conversation.append(
                    {
                        "role": "tool",
                        "tool_call_id": call.id,
                        "content": json.dumps(result),
                    }
                )

        # Out of rounds: ask once more without tools so we always get text.
        if stop_requested():
            return full_text, calls
        payload = self._payload(
            conversation, max_tokens=max_tokens, temperature=temperature, stream=True
        )
        with closing(chunks(payload)) as stream:
            for chunk in stream:
                if stop_requested():
                    break
                try:
                    delta = chunk["choices"][0].get("delta") or {}
                except (KeyError, IndexError, TypeError):
                    continue
                content = delta.get("content")
                if content:
                    full_text += content
                    if on_text is not None:
                        on_text(content)
        return full_text, calls

    def _sse_chunks(
        self,
        payload: dict[str, Any],
        should_stop: Callable[[], bool] | None = None,
    ) -> Iterator[dict[str, Any]]:
        """Yield SSE chunks, interrupting stalled body reads on cancellation.

        CPython urllib exposes the response socket through ``fp.raw._sock``.
        The watcher shuts that socket down: closing a buffered response
        on the watcher can deadlock on the reader's lock. The reader owns close
        and always signals/joins the watcher, including on generator.close().
        Windows also requires CPython's socket._real_close() to wake recv;
        socket.close() defers handle closure while the response owns a makefile.
        """
        if should_stop is not None and should_stop():
            return
        request = urllib.request.Request(
            f"{self._base()}/v1/chat/completions",
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        cancelled = threading.Event()
        try:
            with urllib.request.urlopen(request, timeout=300) as response:
                finished = threading.Event()
                watcher: threading.Thread | None = None
                watcher_errors: list[Exception] = []
                if should_stop is not None:
                    transport_socket = response.fp.raw._sock

                    def watch() -> None:
                        while not finished.is_set():
                            try:
                                stop = should_stop()
                            except Exception as exc:
                                watcher_errors.append(exc)
                                stop = True
                            if stop:
                                cancelled.set()
                                try:
                                    transport_socket.shutdown(socket.SHUT_RDWR)
                                except OSError:
                                    pass  # EOF/close may have won the race.
                                if sys.platform == "win32":
                                    transport_socket._real_close()
                                return
                            if finished.wait(0.02):
                                return

                    watcher = threading.Thread(target=watch, name="text-sse-cancel")
                    watcher.start()
                try:
                    for raw in response:
                        if cancelled.is_set() or (should_stop is not None and should_stop()):
                            cancelled.set()
                            break
                        line = raw.decode("utf-8").strip()
                        if not line.startswith("data:"):
                            continue
                        body = line[5:].strip()
                        if body == "[DONE]":
                            break
                        try:
                            yield json.loads(body)
                        except json.JSONDecodeError:
                            continue
                finally:
                    finished.set()
                    if watcher is not None:
                        watcher.join()
                    if watcher_errors:
                        raise watcher_errors[0]
        except (OSError, http.client.HTTPException, ValueError) as exc:
            if cancelled.is_set() or (should_stop is not None and should_stop()):
                return
            if isinstance(exc, ValueError):
                raise
            raise HostError(f"stream failed: {exc}") from exc

    def run(self, spec: dict[str, Any]) -> str:
        """Host contract entry point: ``spec`` carries ``messages``."""
        return self.chat(
            spec["messages"],
            json_schema=spec.get("json_schema"),
            max_tokens=int(spec.get("max_tokens", 512)),
            temperature=float(spec.get("temperature", 0.7)),
        )

    # -- internals ---------------------------------------------------------

    def _base(self) -> str:
        return self.base_url or f"http://127.0.0.1:{self.port}"

    def _model_name(self) -> str:
        if self._model is None:
            raise HostError("no text model loaded")
        return self._model.descriptor.id

    def _post(self, path: str, payload: dict[str, Any]) -> dict[str, Any]:
        request = urllib.request.Request(
            f"{self._base()}{path}",
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=300) as response:
                return json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")
            raise HostError(f"HTTP {exc.code} from {path}: {detail}") from exc
        except urllib.error.URLError as exc:
            raise HostError(f"cannot reach {self._base()}: {exc}") from exc

    def _start_server(self, model: InstalledModel) -> None:
        exe = self._ensure_binary()
        gguf = model.path if model.path.is_file() else model.path / f"{model.descriptor.id}.gguf"
        if not gguf.exists():
            raise HostError(f"GGUF not found at {gguf}")

        cmd = [
            str(exe),
            "-m",
            str(gguf),
            "--port",
            str(self.port),
            "-ngl",
            str(self.n_gpu_layers),
            "-c",
            str(self.context),
            "--host",
            "127.0.0.1",
            # --jinja enables the model's own chat template, which is required
            # for native tool calling and for reasoning models to behave.
            "--jinja",
        ]
        self._process = subprocess.Popen(
            cmd,
            cwd=str(exe.parent),
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        self._wait_for_health()

    def _wait_for_health(self) -> None:
        deadline = time.time() + self.startup_timeout_s
        while time.time() < deadline:
            if self._process is not None and self._process.poll() is not None:
                raise HostError(
                    f"llama-server exited with code {self._process.returncode}"
                )
            try:
                with urllib.request.urlopen(f"{self._base()}/health", timeout=2) as r:
                    if r.status == 200:
                        return
            except (urllib.error.URLError, OSError):
                time.sleep(0.5)
        raise HostError(f"llama-server did not become healthy within {self.startup_timeout_s}s")

    def _ensure_binary(self) -> Path:
        """Download and extract llama-server if it is not already present."""
        exe = self.binary_dir / "llama-server.exe"
        if exe.exists():
            return exe
        self.binary_dir.mkdir(parents=True, exist_ok=True)
        for url in (LLAMA_CPP_URL, LLAMA_CPP_CUDA_URL):
            archive = self.binary_dir / Path(url).name
            _download(url, archive)
            with zipfile.ZipFile(archive) as zf:
                zf.extractall(self.binary_dir)
            archive.unlink(missing_ok=True)
        if not exe.exists():
            # Some builds nest the binaries one level down.
            found = next(self.binary_dir.rglob("llama-server.exe"), None)
            if found is None:
                raise HostError("llama-server.exe not found after extraction")
            return found
        return exe


def _download(url: str, dest: Path) -> None:
    part = dest.with_suffix(dest.suffix + ".part")
    try:
        with urllib.request.urlopen(url, timeout=120) as response, part.open("wb") as out:
            shutil.copyfileobj(response, out, length=1 << 20)
    except (OSError, urllib.error.URLError) as exc:
        part.unlink(missing_ok=True)
        raise HostError(f"download failed for {url}: {exc}") from exc
    part.replace(dest)
