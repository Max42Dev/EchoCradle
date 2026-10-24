"""Visual-only startup and client contracts without native hosts or model files.

The profile requires visual_config. Missing visual deployments remain diagnostic:
the service is ready, but their jobs fail VISUAL_UNAVAILABLE.
"""

from __future__ import annotations

import asyncio
import importlib
import io
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock
from uuid import uuid4

import httpx
import pytest

from orchestrator.client import ServiceClient, ServiceClientError
from orchestrator.service import ServiceConfig, _Runtime, create_app
from orchestrator.store import ModelStore
from orchestrator.service_protocol import PROTOCOL_VERSION, VISUAL_KINDS, JobSpec, ServiceError
from test_service_visual import glb, png


@pytest.fixture
def isolated_visual(monkeypatch: pytest.MonkeyPatch) -> SimpleNamespace:
    """Fail immediately on any unrelated runtime lookup, host creation or load."""
    forbidden = Mock(side_effect=AssertionError("Text/speech dependency used"))
    for target in (
        "orchestrator.orchestrator.ModelOrchestrator.__init__",
        "orchestrator.orchestrator.ModelOrchestrator.ensure_model",
        "orchestrator.orchestrator.TextHost",
        "orchestrator.orchestrator.TtsHost",
        "orchestrator.orchestrator.SttHost",
        "orchestrator.service.TextHost",
        "orchestrator.service.RuntimeCatalog.default",
        "orchestrator.runtimes.RuntimeCatalog.get",
        "orchestrator.service.Catalog.default",
        "orchestrator.service.ModelStore.ensure",
    ):
        monkeypatch.setattr(target, forbidden)
    monkeypatch.setattr(importlib.import_module("orchestrator.probe"), "probe", lambda: SimpleNamespace(
        to_dict=lambda: {"gpus": [], "tier": 0},
    ))
    calls = []
    available = {"value": True}

    class Backend:
        def __init__(self, config: Path, *, shippable_only: bool) -> None:
            calls.append(("init", config, shippable_only))

        def availability(self) -> dict:
            calls.append(("inspect",))
            return {kind: {"available": available["value"]} for kind in VISUAL_KINDS}

        def generate_image(self, text: str, output: Path, **kwargs: object) -> Path:
            calls.append(("image", text))
            output.write_bytes(png())
            return output

        def generate_3d(self, source: Path, output: Path, **kwargs: object) -> Path:
            assert source.read_bytes() == png()
            calls.append(("image_to_3d",))
            output.write_bytes(glb())
            return output

        def animate_3d(self, source: Path, output: Path, **kwargs: object) -> Path:
            assert source.read_bytes() == glb()
            calls.append(("animate_3d",))
            output.write_bytes(glb())
            return output

    monkeypatch.setattr("orchestrator.visual.VisualBackend", Backend)
    return SimpleNamespace(forbidden=forbidden, calls=calls, available=available)


def test_visual_requires_config() -> None:
    with pytest.raises(ServiceError) as error:
        ServiceConfig(profile="visual")
    assert error.value.code == "INVALID_CONFIG"
    for factory in (ServiceClient, ServiceClient.start_owned):
        with pytest.raises(ServiceClientError) as error:
            factory(profile="visual")
        assert error.value.code == "INVALID_CONFIG"


@pytest.mark.parametrize("available", [True, False])
def test_visual_startup_routes_and_shutdown(
    isolated_visual: SimpleNamespace, tmp_path: Path, available: bool,
) -> None:
    async def run() -> None:
        isolated_visual.available["value"] = available
        config = ServiceConfig(
            profile="visual", visual_config=tmp_path / "visual.json", port=80,
            store_root=tmp_path / "models", runtime_config=tmp_path / "missing-runtime.json",
            model_catalog=tmp_path / "missing-catalog.json",
            text_model="not-cached", tts_model="not-cached", stt_model="not-cached",
        )
        app = create_app(config, token="test-token")
        runtime = app.state.service
        async with app.router.lifespan_context(app):
            await asyncio.wait_for(runtime.startup_task, 2)
            assert runtime.state == "ready" and runtime.startup_error is None
            facade = runtime.facade
            assert type(facade.store) is ModelStore
            assert facade._hosts == {} and not hasattr(facade, "text")
            assert runtime.store_lock._file is not None
            assert runtime.diagnostics["planned"] == runtime.diagnostics["actual"] == {}
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://127.0.0.1",
                headers={"Authorization": "Bearer test-token"},
            ) as client:
                assert (await client.get("/v1/health/ready")).status_code == 200
                capabilities = (await client.get("/v1/capabilities")).json()
                assert capabilities["profile"] == "visual"
                for kind in ("text", "tts", "stt"):
                    assert capabilities["kinds"][kind]["state"] == "unsupported"
                    spec = {"kind": kind, "text": "hello"} if kind != "stt" else {
                        "kind": kind, "input_artifact_id": str(uuid4()),
                    }
                    response = await client.post("/v1/jobs", json=spec)
                    assert response.json()["error"]["code"] == "UNSUPPORTED_KIND"
                assert not runtime.jobs and not runtime.artifacts
                for kind in VISUAL_KINDS:
                    assert capabilities["kinds"][kind]["state"] == (
                        "ready" if available else "unavailable"
                    )
                if not available:
                    response = await client.post("/v1/jobs", json={
                        "kind": "image", "text": "portrait", "seed": 0,
                    })
                    assert response.json()["error"]["code"] == "VISUAL_UNAVAILABLE"
                else:
                    artifact = None
                    for kind in ("image", "image_to_3d", "animate_3d"):
                        spec = {"kind": kind, "seed": 3}
                        spec.update({"text": "portrait"} if kind == "image" else {
                            "input_artifact_id": artifact,
                        })
                        response = await client.post("/v1/jobs", json=spec)
                        assert response.status_code == 202, response.text
                        job = runtime.jobs[response.json()["job_id"]]
                        await asyncio.wait_for(runtime.queues["visual"].join(), 2)
                        assert job.state == "succeeded", job.error
                        artifact = job.result["artifact_id"]
        assert runtime.facade is None and runtime.store_lock._file is None
        isolated_visual.forbidden.assert_not_called()

    asyncio.run(run())


def test_visual_shippable_policy(isolated_visual: SimpleNamespace, tmp_path: Path) -> None:
    async def run() -> None:
        runtime = _Runtime(ServiceConfig(
            profile="visual", visual_config=tmp_path / "visual.json",
            store_root=tmp_path / "models", shippable_only=True,
        ), None, None)
        await runtime.start()
        try:
            await asyncio.wait_for(runtime.startup_task, 2)
            assert runtime.state == "ready"
            assert runtime.capabilities()["kinds"]["image"]["state"] == "unavailable"
            with pytest.raises(ServiceError) as error:
                runtime.submit(JobSpec.parse({"kind": "image", "text": "x", "seed": 0}))
            assert error.value.code == "NOT_SHIPPABLE"
            isolated_visual.forbidden.assert_not_called()
        finally:
            await runtime.stop()

    asyncio.run(run())


@pytest.mark.parametrize("owned_factory", [ServiceClient, ServiceClient.start_owned])
def test_owned_visual_client_accepts_unsupported_text_capabilities(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, owned_factory: object,
) -> None:
    instance = str(uuid4())
    config = tmp_path / "visual.json"
    captured = []

    def spawn(self: ServiceClient, *args: object) -> dict:
        captured.append(args)
        return {"protocol_version": PROTOCOL_VERSION, "service_instance_id": instance,
                "port": 12345}

    def request(self: ServiceClient, method: str, path: str, **kwargs: object) -> dict:
        if path == "/v1/control/shutdown":
            return {}
        assert path == "/v1/capabilities"
        return {"profile": "visual", "probe": {"gpus": []}, "kinds": {
            **{kind: {"state": "unsupported"} for kind in ("text", "tts", "stt")},
            **{kind: {"state": "ready"} for kind in VISUAL_KINDS},
        }}

    monkeypatch.setattr(ServiceClient, "_spawn", spawn)
    monkeypatch.setattr(ServiceClient, "_wait_ready", lambda self: None)
    monkeypatch.setattr(ServiceClient, "_request", request)
    with owned_factory(profile="visual", visual_config=config) as client:
        assert captured[0][0] == "visual" and captured[0][-1] == config
        assert not client.report.has_gpu
        assert client.tts.sample_rate == 0 and not client.stt.is_streaming


def test_spawn_passes_visual_profile_and_config(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    captured = []

    def popen(command: list[str], **kwargs: object) -> None:
        captured.append(command)
        raise OSError("fake launch")

    monkeypatch.setattr("orchestrator.client.subprocess.Popen", popen)
    monkeypatch.setattr("orchestrator.client._WindowsJob", lambda: None)
    client = object.__new__(ServiceClient)
    client._token = "test-token"
    client._windows_job = None
    config = tmp_path / "visual.json"
    with pytest.raises(ServiceClientError) as error:
        client._spawn("visual", None, None, None, None, False, config)
    assert error.value.code == "STARTUP_FAILED"
    assert captured[0][2:] == ["orchestrator.service", "--profile", "visual",
                               "--visual-config", str(config.resolve())]


def test_cli_accepts_visual_profile(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    from orchestrator import service

    captured = []
    monkeypatch.setenv("ECHOCRADLE_SERVICE_TOKEN", "test-token")
    monkeypatch.setattr(service, "StoreWriterLock", lambda root: SimpleNamespace(acquire=lambda: None))

    def create(config: ServiceConfig, **kwargs: object) -> None:
        captured.append(config)
        raise OSError("stop before serving")

    monkeypatch.setattr(service, "create_app", create)
    monkeypatch.setattr(service.sys, "stdout", io.StringIO())
    assert service.main(["--profile", "visual", "--visual-config",
                         str(tmp_path / "visual.json")]) == 1
    assert captured[0].profile == "visual"
    assert service.main(["--profile", "visual"]) == 1
    assert len(captured) == 1