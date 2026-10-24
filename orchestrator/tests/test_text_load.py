"""Transactional text-host loading without downloads or native processes."""

import subprocess
from pathlib import Path
from unittest.mock import Mock

import pytest

from orchestrator.catalog import Catalog
from orchestrator.hosts.base import HostError
from orchestrator.hosts.text import TextHost
from orchestrator.store import InstalledModel


@pytest.fixture
def model(tmp_path: Path) -> InstalledModel:
    path = tmp_path / "model.gguf"
    path.touch()
    return InstalledModel(Catalog.default().get("granite-4.2-8b-q4km"), path)


@pytest.fixture
def host(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> TextHost:
    monkeypatch.setattr(
        "orchestrator.hosts.text._download",
        Mock(side_effect=AssertionError("downloads are forbidden")),
    )
    return TextHost(binary_dir=tmp_path, startup_timeout_s=0)


def test_load_commits_only_after_startup(
    host: TextHost, model: InstalledModel, monkeypatch: pytest.MonkeyPatch,
) -> None:
    def start(candidate: InstalledModel) -> None:
        assert candidate is model
        assert host.loaded_model_id is None

    startup = Mock(side_effect=start)
    monkeypatch.setattr(host, "_start_server", startup)
    host.load(model)
    assert host.loaded_model_id == model.descriptor.id
    host.load(model)
    startup.assert_called_once_with(model)


def test_external_load_does_not_start_server(
    model: InstalledModel, monkeypatch: pytest.MonkeyPatch,
) -> None:
    host = TextHost(base_url="http://fake")
    startup = Mock(side_effect=AssertionError("external server must not be started"))
    monkeypatch.setattr(host, "_start_server", startup)
    host.load(model)
    assert host.loaded_model_id == model.descriptor.id
    host.unload()
    assert host.loaded_model_id is None
    startup.assert_not_called()


@pytest.mark.parametrize("failure", ["binary", "missing", "spawn", "exit", "timeout", "interrupt"])
def test_failed_load_cleans_up_and_same_model_can_retry(
    host: TextHost, model: InstalledModel, monkeypatch: pytest.MonkeyPatch,
    failure: str,
) -> None:
    executable = host.binary_dir / "llama-server.exe"
    ensure = Mock(return_value=executable)
    monkeypatch.setattr(host, "_ensure_binary", ensure)
    process = Mock()
    process.poll.return_value = 1 if failure == "exit" else None
    process.returncode = 1
    spawn = Mock(return_value=process)
    monkeypatch.setattr("orchestrator.hosts.text.subprocess.Popen", spawn)
    error_type: type[BaseException] = HostError
    if failure == "binary":
        ensure.side_effect = HostError("binary unavailable")
    elif failure == "missing":
        model.path.unlink()
    elif failure == "spawn":
        spawn.side_effect = OSError("spawn failed")
        error_type = OSError
    elif failure == "exit":
        host.startup_timeout_s = 10
    elif failure == "interrupt":
        monkeypatch.setattr(host, "_wait_for_health", Mock(side_effect=KeyboardInterrupt))
        error_type = KeyboardInterrupt

    with pytest.raises(error_type):
        host.load(model)

    assert host.loaded_model_id is None
    assert host._process is None
    if failure in {"exit", "timeout", "interrupt"}:
        process.terminate.assert_called_once_with()
        process.wait.assert_called_once_with(timeout=15)
    else:
        process.terminate.assert_not_called()

    ensure.side_effect = None
    spawn.side_effect = None
    model.path.touch()
    monkeypatch.setattr(host, "_wait_for_health", Mock())
    host.load(model)
    assert host.loaded_model_id == model.descriptor.id
    assert host._process is process
    host.unload()


def test_failed_replacement_unloads_old_process_and_kills_stalled_startup(
    host: TextHost, model: InstalledModel, monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    old_model = InstalledModel(Catalog.default().get("granite-4.2-3b-q4km"), tmp_path)
    old_process = Mock()
    host._model = old_model
    host._process = old_process
    process = Mock()
    process.wait.side_effect = [subprocess.TimeoutExpired("llama-server", 15), None]
    monkeypatch.setattr(host, "_ensure_binary", Mock(return_value=tmp_path / "server.exe"))

    def spawn(*args: object, **kwargs: object) -> Mock:
        old_process.terminate.assert_called_once_with()
        old_process.wait.assert_called_once_with(timeout=15)
        assert host.loaded_model_id is None
        assert host._process is None
        return process

    monkeypatch.setattr("orchestrator.hosts.text.subprocess.Popen", spawn)
    with pytest.raises(HostError, match="did not become healthy"):
        host.load(model)

    assert host.loaded_model_id is None
    assert host._process is None
    process.terminate.assert_called_once_with()
    process.kill.assert_called_once_with()
    assert [call.kwargs for call in process.wait.call_args_list] == [
        {"timeout": 15}, {"timeout": 5},
    ]