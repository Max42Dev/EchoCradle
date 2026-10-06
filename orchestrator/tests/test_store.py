"""Store: install detection, archive extraction, and the shared path."""

from __future__ import annotations

import tarfile
from pathlib import Path

import pytest

from orchestrator.catalog import ModelDescriptor, Modality
from orchestrator.errors import ModelDownloadError
from orchestrator.store import ModelStore


def _model(**overrides) -> ModelDescriptor:
    base = dict(
        id="test-model",
        modality=Modality.TEXT,
        family="Test",
        tier=0,
        quality=0.5,
        license="MIT",
        shippable=True,
        url="https://example.invalid/x",
        size_gb=0.1,
    )
    base.update(overrides)
    return ModelDescriptor(**base)


def test_single_file_install_detection(tmp_path: Path):
    store = ModelStore(tmp_path)
    model = _model(single_file=True)
    assert not store.is_installed(model)
    store.path_for(model).write_bytes(b"weights")
    assert store.is_installed(model)


def test_archive_install_requires_all_files(tmp_path: Path):
    store = ModelStore(tmp_path)
    model = _model(files=("model.onnx", "tokens.txt"))
    target = store.path_for(model)
    target.mkdir(parents=True)
    (target / "model.onnx").write_bytes(b"x")
    assert not store.is_installed(model)
    (target / "tokens.txt").write_bytes(b"x")
    assert store.is_installed(model)


def test_installed_returns_none_when_missing(tmp_path: Path):
    store = ModelStore(tmp_path)
    assert store.installed(_model(single_file=True)) is None


def test_extract_flattens_single_top_level_dir(tmp_path: Path):
    store = ModelStore(tmp_path)
    archive = tmp_path / "a.tar.gz"
    payload = tmp_path / "payload"
    (payload / "inner").mkdir(parents=True)
    (payload / "inner" / "model.onnx").write_bytes(b"weights")
    (payload / "inner" / "tokens.txt").write_bytes(b"tok")
    with tarfile.open(archive, "w:gz") as tar:
        tar.add(payload / "inner", arcname="top/inner")

    target = tmp_path / "out"
    store._extract(archive, target)
    assert (target / "inner" / "model.onnx").exists()
    assert (target / "inner" / "tokens.txt").exists()


def test_extract_rejects_corrupt_archive(tmp_path: Path):
    store = ModelStore(tmp_path)
    bad = tmp_path / "bad.tar.bz2"
    bad.write_bytes(b"not an archive")
    with pytest.raises(ModelDownloadError):
        store._extract(bad, tmp_path / "out")


def test_remove_deletes_files(tmp_path: Path):
    store = ModelStore(tmp_path)
    model = _model(single_file=True)
    store.path_for(model).write_bytes(b"weights")
    store.remove(model)
    assert not store.is_installed(model)


def test_disk_usage_counts_files(tmp_path: Path):
    store = ModelStore(tmp_path)
    (tmp_path / "a.bin").write_bytes(b"x" * 1024)
    assert store.disk_usage_gb() > 0
