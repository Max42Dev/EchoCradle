"""Runtime configuration and provisioning tests without network or native inference."""

from __future__ import annotations

import json
import zipfile
from dataclasses import replace
from pathlib import Path
from unittest.mock import Mock

import pytest

from orchestrator.catalog import Catalog, Modality
from orchestrator.hosts.text import TextHost
from orchestrator.runtimes import RuntimeCatalog
from orchestrator.service import CachedTextHost, ServiceConfig, _Runtime
from orchestrator.service_protocol import ServiceError


def write_runtime(path: Path, **changes: object) -> Path:
    entry = {
        "id": "llama.cpp", "version": "b12345", "variant": "win-cpu-x64",
        "executable": "custom-server.exe",
        "archives": ["https://example.com/runtime.zip"],
        **changes,
    }
    path.write_text(json.dumps({"runtimes": [entry]}), encoding="utf-8")
    return path


def test_packaged_runtime_is_pinned() -> None:
    runtime = RuntimeCatalog.default().get("llama.cpp")
    assert runtime.version == "b11471"
    assert runtime.variant == "win-cuda-12.4-x64"
    assert len(runtime.archives) == 2


def test_runtime_override_precedence(monkeypatch, tmp_path: Path) -> None:
    environment = write_runtime(tmp_path / "environment.json")
    explicit = write_runtime(tmp_path / "explicit.json", version="b54321")
    monkeypatch.setenv("ECHOCRADLE_RUNTIME_CONFIG", str(environment))
    assert RuntimeCatalog.default().get("llama.cpp").version == "b12345"
    assert RuntimeCatalog.default(explicit).get("llama.cpp").version == "b54321"
    host = TextHost(base_url="http://fake")
    assert host.runtime.version == "b12345"
    assert host.binary_dir.parts[-3:] == ("llama.cpp", "b12345", "win-cpu-x64")


def test_invalid_override_does_not_fall_back(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("ECHOCRADLE_RUNTIME_CONFIG", str(tmp_path / "missing.json"))
    with pytest.raises(FileNotFoundError):
        RuntimeCatalog.default()


@pytest.mark.parametrize("changes", [
    {"version": "../escape"}, {"version": "latest"}, {"variant": "/absolute"},
    {"executable": "../server.exe"}, {"archives": []}, {"archives": "wrong"},
    {"archives": [42]}, {"archives": ["http://example.com/runtime.zip"]},
    {"archives": ["https://user:secret@example.com/runtime.zip"]},
    {"archives": ["https://example.com/runtime.tar"]},
    {"archives": ["https://example.com/a.zip", "https://other.example/a.zip"]},
    {"unknown": True},
])
def test_invalid_runtime_config_is_rejected(tmp_path: Path, changes: dict) -> None:
    with pytest.raises(ValueError):
        RuntimeCatalog.from_json(write_runtime(tmp_path / "runtimes.json", **changes))


def test_duplicate_runtime_ids_are_rejected() -> None:
    runtime = RuntimeCatalog.default().get("llama.cpp")
    with pytest.raises(ValueError, match="Duplicate"):
        RuntimeCatalog([runtime, runtime])


def test_cache_isolated_by_version_and_variant(tmp_path: Path) -> None:
    runtime = RuntimeCatalog.default().get("llama.cpp")
    assert runtime.cache_dir(tmp_path) != replace(runtime, version="b12345").cache_dir(tmp_path)
    assert runtime.cache_dir(tmp_path) != replace(runtime, variant="win-cpu-x64").cache_dir(tmp_path)


def test_provisioning_uses_configured_archives_and_executable(monkeypatch, tmp_path: Path) -> None:
    runtime = RuntimeCatalog.from_json(write_runtime(tmp_path / "config.json")).get("llama.cpp")
    downloads = []

    def download(url: str, destination: Path) -> None:
        downloads.append(url)
        with zipfile.ZipFile(destination, "w") as archive:
            archive.writestr("nested/custom-server.exe", b"test executable")

    monkeypatch.setattr("orchestrator.hosts.text._download", download)
    host = TextHost(runtime=runtime, binary_dir=tmp_path / "binaries")
    assert host._ensure_binary().name == "custom-server.exe"
    assert downloads == list(runtime.archives)
    assert not list(host.binary_dir.glob("*.zip"))
    host._ensure_binary()
    assert downloads == list(runtime.archives)


def test_cached_host_never_downloads(monkeypatch, tmp_path: Path) -> None:
    runtime = RuntimeCatalog.from_json(write_runtime(tmp_path / "config.json")).get("llama.cpp")
    download = Mock()
    monkeypatch.setattr("orchestrator.hosts.text._download", download)
    host = CachedTextHost(runtime=runtime, binary_dir=tmp_path / "binaries")
    with pytest.raises(ServiceError) as error:
        host._ensure_binary()
    assert error.value.code == "MODEL_NOT_PROVISIONED"
    host.binary_dir.mkdir()
    executable = host.binary_dir / runtime.executable
    executable.touch()
    assert host._ensure_binary() == executable
    download.assert_not_called()


def test_model_catalog_override_precedence(monkeypatch, tmp_path: Path) -> None:
    def write_catalog(name: str, preference: str) -> Path:
        path = tmp_path / name
        path.write_text(json.dumps({
            "models": [], "preferred_models": {"text": preference},
        }), encoding="utf-8")
        return path

    environment = write_catalog("environment.json", "environment-model")
    explicit = write_catalog("explicit.json", "explicit-model")
    monkeypatch.setenv("ECHOCRADLE_MODEL_CATALOG", str(environment))
    assert Catalog.default().preferred_model_id(Modality.TEXT) == "environment-model"
    assert Catalog.default(explicit).preferred_model_id(Modality.TEXT) == "explicit-model"


def test_service_profile_uses_external_configs_and_store_root(monkeypatch, tmp_path: Path) -> None:
    runtime_path = write_runtime(tmp_path / "runtime.json")
    model_path = tmp_path / "models.json"
    model_path.write_text('{"models": []}', encoding="utf-8")
    facade = Mock()
    facade.runtimes = RuntimeCatalog.from_json(runtime_path)
    facade._hosts = {}
    facade.planner.plan.side_effect = RuntimeError("stop before model loading")
    factory = Mock(return_value=facade)
    monkeypatch.setattr("orchestrator.orchestrator.ModelOrchestrator", factory)
    root = tmp_path / "store"
    runtime = _Runtime(ServiceConfig(
        store_root=root, runtime_config=runtime_path, model_catalog=model_path,
    ), None, Mock())
    runtime.loop = Mock()
    try:
        with pytest.raises(RuntimeError, match="stop before model loading"):
            runtime._load_profile()
        assert len(factory.call_args.kwargs["catalog"]) == 0
        assert factory.call_args.kwargs["runtimes"].get("llama.cpp").version == "b12345"
        assert facade.text.binary_dir == root / "llama.cpp" / "b12345" / "win-cpu-x64"
        assert facade._hosts[Modality.TEXT] is facade.text
    finally:
        for executor in runtime.executors.values():
            executor.shutdown(wait=True)