"""Default provisioning contracts: tiny in-memory downloads, no native processes/network."""

from __future__ import annotations

import asyncio
import io
import tarfile
import threading
import urllib.error
import zipfile
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from orchestrator.catalog import Catalog, Modality, ModelDescriptor
from orchestrator.errors import ModelDownloadError
from orchestrator.hosts.base import HostError
from orchestrator.hosts.text import TextHost, _download
from orchestrator.probe import ProbeReport
from orchestrator.runtimes import RuntimeCatalog
from orchestrator.service import ServiceConfig, StoreWriterLock, _Runtime
from orchestrator.service_protocol import JobSpec
from orchestrator.store import ModelStore


class Response(io.BytesIO):
    def __init__(self, body: bytes, length: str | None = None) -> None:
        super().__init__(body)
        self.headers = {} if length is None else {"Content-Length": length}


def model(**changes: object) -> ModelDescriptor:
    return ModelDescriptor(**{
        "id": "tiny", "modality": Modality.TEXT, "family": "test", "tier": 0,
        "quality": 1.0, "license": "MIT", "shippable": True,
        "url": "https://example.invalid/model", "size_gb": 0.0,
        "single_file": True, **changes,
    })


@pytest.mark.parametrize("profile", ["text", "voice"])
def test_default_startup_provisions_under_lock_off_loop_and_reuses_cache(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, profile: str,
) -> None:
    async def run() -> None:
        main_thread = threading.get_ident()
        events = []
        downloads = []
        entered = asyncio.Event()
        proceed = threading.Event()
        loop = asyncio.get_running_loop()
        lock = StoreWriterLock(tmp_path)
        acquire, release = lock.acquire, lock.release

        def acquire_lock() -> None:
            acquire()
            events.append("acquire")

        def release_lock() -> None:
            events.append("release")
            release()

        monkeypatch.setattr(lock, "acquire", acquire_lock)
        monkeypatch.setattr(lock, "release", release_lock)
        descriptors = [model()]
        for modality in (Modality.TTS, Modality.STT):
            descriptors.append(model(
                id=modality.value, modality=modality, single_file=False,
                files=("model.onnx",), url=f"https://example.invalid/{modality.value}",
            ))
        monkeypatch.setattr(Catalog, "default", lambda *args: Catalog(descriptors))
        monkeypatch.setattr("orchestrator.orchestrator.probe", lambda: ProbeReport(ram_gb=16))

        class Speech:
            loaded_model_id = None
            num_speakers = 1
            sample_rate = 16000
            speaker_id = 0
            is_streaming = True

            def load(self, installed: object) -> None:
                assert lock._file is not None
                self.loaded_model_id = installed.descriptor.id

            def unload(self) -> None:
                assert lock._file is not None
                events.append("unload-speech")
                self.loaded_model_id = None

            def transcribe_samples(self, blocks: object, rate: int) -> str:
                return "test"

        monkeypatch.setattr("orchestrator.orchestrator.TtsHost", Speech)
        monkeypatch.setattr("orchestrator.orchestrator.SttHost", Speech)

        def start_server(host: TextHost, installed: object) -> None:
            assert lock._file is not None
            assert host._ensure_binary().read_bytes() == b"executable"
            events.append("load-text")

        monkeypatch.setattr(TextHost, "_start_server", start_server)
        native_runtime = RuntimeCatalog.default().get("llama.cpp")

        def urlopen(url: str, **kwargs: object) -> Response:
            assert isinstance(url, str)  # No credential-bearing Request or HF client.
            assert threading.get_ident() != main_thread
            assert lock._file is not None
            downloads.append(url)
            if len(downloads) == 1:
                loop.call_soon_threadsafe(entered.set)
                assert proceed.wait(3)
            body = io.BytesIO()
            if url in native_runtime.archives:
                with zipfile.ZipFile(body, "w") as archive:
                    archive.writestr(native_runtime.executable, b"executable")
            elif url.endswith(("/tts", "/stt")):
                with tarfile.open(fileobj=body, mode="w:gz") as archive:
                    member = tarfile.TarInfo("top/model.onnx")
                    member.size = 7
                    archive.addfile(member, io.BytesIO(b"weights"))
            else:
                body.write(b"weights")
            payload = body.getvalue()
            return Response(payload, str(len(payload)))

        monkeypatch.setenv("HF_TOKEN", "must-not-be-used")
        monkeypatch.setattr("urllib.request.urlopen", urlopen)
        runtime = _Runtime(ServiceConfig(profile=profile, store_root=tmp_path), None, lock)
        await runtime.start()
        try:
            await asyncio.wait_for(entered.wait(), 2)
            # A blocked download must not block health/capability reads on the loop.
            capabilities = runtime.capabilities()
            assert capabilities["state"] == "starting"
            assert capabilities["cached_only"] is False
            assert capabilities["features"]["provisioning"] is True
            assert capabilities["kinds"]["image"]["state"] == "unavailable"
            assert not runtime.startup_task.done()
            proceed.set()
            await asyncio.wait_for(runtime.startup_task, 3)
            assert runtime.state == "ready", runtime.startup_error
            assert type(runtime.facade.store) is ModelStore
            assert type(runtime.facade.text) is TextHost
            assert len(downloads) == (5 if profile == "voice" else 3)
            original_downloads = list(downloads)
            # Visual eviction followed by text use reloads the originally selected
            # model under the same writer ownership, without downloading again.
            runtime.facade.text.unload()
            runtime._infer = lambda job: {"text": "ok", "calls": []}
            job = runtime.submit(JobSpec.parse({"kind": "text", "text": "hello"}))
            await asyncio.wait_for(runtime.queues["text"].join(), 2)
            assert job.state == "succeeded"
            assert runtime.facade.text.loaded_model_id == "tiny"
            assert downloads == original_downloads
            assert events[0] == "acquire" and "release" not in events
        finally:
            proceed.set()
            await runtime.stop()
        assert events[-1] == "release" and lock._file is None

    asyncio.run(run())


def test_lock_failure_prevents_store_or_host_construction(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    lock = Mock()
    lock.acquire.side_effect = OSError("busy")
    factory = Mock(side_effect=AssertionError("must not construct store"))
    monkeypatch.setattr("orchestrator.service.ModelStore", factory)
    runtime = _Runtime(ServiceConfig(store_root=tmp_path), None, lock)
    try:
        with pytest.raises(OSError, match="busy"):
            runtime._load_profile()
        factory.assert_not_called()
    finally:
        for executor in runtime.executors.values():
            executor.shutdown(wait=True)


def test_provision_failure_unloads_before_releasing_writer_lock(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    async def run() -> None:
        lock = StoreWriterLock(tmp_path)
        events = []
        release = lock.release

        def release_lock() -> None:
            events.append("release")
            release()

        def stop() -> None:
            assert lock._file is not None
            events.append("stop")

        def fetch(*args: object, **kwargs: object) -> None:
            assert lock._file is not None
            events.append("download-failed")
            raise ModelDownloadError("private path or URL details")

        def factory(**kwargs: object) -> SimpleNamespace:
            assert lock._file is not None
            descriptor = model()
            return SimpleNamespace(
                runtimes=kwargs["runtimes"], _hosts={}, stop=stop,
                report=ProbeReport(ram_gb=16),
                planner=SimpleNamespace(plan=lambda *a, **k: SimpleNamespace(
                    model=descriptor, reason="test",
                )),
                ensure_model=lambda *a, **k: kwargs["store"].ensure(descriptor),
            )

        monkeypatch.setattr(lock, "release", release_lock)
        monkeypatch.setattr(ModelStore, "_fetch_file", fetch)
        monkeypatch.setattr("orchestrator.orchestrator.ModelOrchestrator", factory)
        runtime = _Runtime(ServiceConfig(store_root=tmp_path), None, lock)
        await runtime.start()
        try:
            await asyncio.wait_for(runtime.startup_task, 2)
            assert runtime.state == "failed"
            assert runtime.startup_error["code"] == "STARTUP_FAILED"
            assert "private" not in str(runtime.startup_error)
            assert events == ["download-failed", "stop", "release"]
            assert lock._file is None and runtime.facade is None
        finally:
            await runtime.stop()
        assert events == ["download-failed", "stop", "release"]

    asyncio.run(run())


def test_shippable_policy_runs_before_any_download(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    async def run() -> None:
        monkeypatch.setattr(Catalog, "default", lambda *a: Catalog([model(shippable=False)]))
        monkeypatch.setattr("orchestrator.orchestrator.probe", lambda: ProbeReport(ram_gb=16))
        fetch = Mock(side_effect=AssertionError("ineligible model must not download"))
        monkeypatch.setattr("urllib.request.urlopen", fetch)
        runtime = _Runtime(ServiceConfig(
            store_root=tmp_path, shippable_only=True, text_model="tiny",
        ), None, None)
        await runtime.start()
        try:
            await asyncio.wait_for(runtime.startup_task, 2)
            assert runtime.state == "failed"
            fetch.assert_not_called()
            assert runtime.store_lock._file is None
        finally:
            await runtime.stop()

    asyncio.run(run())


@pytest.mark.parametrize("url", [
    "http://example.invalid/model", "https://user:secret@example.invalid/model",
    "https://example.invalid/model?token=secret",
])
def test_model_credentials_and_insecure_urls_are_rejected_before_network(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, url: str,
) -> None:
    fetch = Mock(side_effect=AssertionError("must not use network"))
    monkeypatch.setattr("urllib.request.urlopen", fetch)
    with pytest.raises(ModelDownloadError, match="anonymous HTTPS"):
        ModelStore(tmp_path).ensure(model(url=url))
    fetch.assert_not_called()


@pytest.mark.parametrize("kind", ["model", "runtime"])
@pytest.mark.parametrize("body,length", [(b"", "0"), (b"short", "10"), (b"x", "invalid")])
def test_failed_download_never_publishes_and_cleans_temporary_file(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, kind: str, body: bytes, length: str,
) -> None:
    monkeypatch.setattr("urllib.request.urlopen", lambda *a, **k: Response(body, length))
    destination = tmp_path / "cached"
    destination.write_bytes(b"previous")
    with pytest.raises((ModelDownloadError, HostError)):
        if kind == "model":
            ModelStore(tmp_path)._fetch_file("https://example.invalid/x", destination, "x", None)
        else:
            _download("https://example.invalid/x", destination)
    assert destination.read_bytes() == b"previous"
    assert not destination.with_suffix(".part").exists()


@pytest.mark.parametrize("kind", ["model", "runtime"])
def test_download_is_anonymous_and_does_not_retry_with_credentials(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, kind: str,
) -> None:
    monkeypatch.setenv("HF_TOKEN", "must-not-be-used")
    url = "https://huggingface.co/example/model/resolve/main/model.bin"
    fetch = Mock(side_effect=urllib.error.HTTPError(url, 401, "gated", {}, None))
    monkeypatch.setattr("urllib.request.urlopen", fetch)
    with pytest.raises((ModelDownloadError, HostError)):
        if kind == "model":
            ModelStore(tmp_path).ensure(model(url=url))
        else:
            _download(url, tmp_path / "runtime.zip")
    fetch.assert_called_once()
    assert fetch.call_args.args == (url,)
    assert not list(tmp_path.glob("*.part"))


def test_runtime_dependency_failure_cannot_leave_false_cached_executable(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    runtime = replace(RuntimeCatalog.default().get("llama.cpp"), archives=(
        "https://example.invalid/server.zip", "https://example.invalid/dependency.zip",
    ))
    calls = []

    def download(url: str, destination: Path) -> None:
        calls.append(url)
        if url.endswith("dependency.zip"):
            raise HostError("dependency failed")
        with zipfile.ZipFile(destination, "w") as archive:
            archive.writestr(runtime.executable, b"executable")

    monkeypatch.setattr("orchestrator.hosts.text._download", download)
    host = TextHost(runtime=runtime, binary_dir=tmp_path / "runtime")
    for _ in range(2):
        with pytest.raises(HostError, match="dependency failed"):
            host._ensure_binary()
        assert not host.binary_dir.exists()
        assert not list(tmp_path.glob(".runtime-*"))
    assert calls == list(runtime.archives) * 2


def test_model_archive_failure_does_not_publish_partial_install(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    store = ModelStore(tmp_path)
    descriptor = model(single_file=False, files=("model.onnx", "tokens.txt"))

    def fetch(url: str, destination: Path, *args: object) -> None:
        with tarfile.open(destination, "w:gz") as archive:
            member = tarfile.TarInfo("top/model.onnx")
            member.size = 7
            archive.addfile(member, io.BytesIO(b"weights"))

    monkeypatch.setattr(store, "_fetch_file", fetch)
    with pytest.raises(ModelDownloadError, match="required files"):
        store.ensure(descriptor)
    assert not store.is_installed(descriptor)
    assert not list(tmp_path.iterdir())