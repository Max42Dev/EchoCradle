"""Catalog loading and descriptor round-tripping."""

from __future__ import annotations

import json
import tomllib
from pathlib import Path

import pytest

from orchestrator.catalog import Catalog, ModelDescriptor, Modality


def test_default_catalog_loads_and_is_open_weight():
    catalog = Catalog.default()
    assert len(catalog) >= 6
    for model in catalog:
        assert model.license, f"{model.id} has no licence"
        assert model.url.startswith("https://"), f"{model.id} url is not https"
        assert model.size_gb > 0


def test_by_modality_sorts_by_quality_descending():
    catalog = Catalog.default()
    text = catalog.by_modality(Modality.TEXT)
    assert text, "expected text models"
    qualities = [m.quality for m in text]
    assert qualities == sorted(qualities, reverse=True)


def test_horizon_is_an_optional_official_single_file_model() -> None:
    catalog = Catalog.default()
    model = catalog.get("k2-horizon-7b-q4km")
    assert model.modality is Modality.TEXT
    assert model.license == "Apache-2.0"
    assert model.shippable and model.single_file
    assert model.url == (
        "https://huggingface.co/IFM/K2-Horizon-7B-GGUF/resolve/main/"
        "K2-Horizon-7B-Q4_K_M.gguf"
    )
    assert model.params["enable_thinking"] is False
    assert model.params["context"] == 8192
    assert catalog.preferred_model_id(Modality.TEXT) == "granite-4.2-8b-q4km"
    assert ModelDescriptor.from_dict(model.to_dict()) == model


def test_get_unknown_model_raises():
    catalog = Catalog.default()
    with pytest.raises(KeyError):
        catalog.get("does-not-exist")


def test_descriptor_round_trip():
    original = ModelDescriptor(
        id="x",
        modality=Modality.TTS,
        family="F",
        tier=1,
        quality=0.5,
        license="MIT",
        shippable=True,
        url="https://example.invalid/x",
        size_gb=1.0,
        ram_gb=0.5,
        files=("a.onnx", "tokens.txt"),
        params={"sample_rate": 16000},
    )
    restored = ModelDescriptor.from_dict(original.to_dict())
    assert restored == original


def test_cpu_only_flag():
    catalog = Catalog.default()
    tts = catalog.by_modality(Modality.TTS)[0]
    assert tts.is_cpu_only, "speech models must not claim VRAM"


def test_json_preferences_are_optional_and_data_driven(tmp_path) -> None:
    model = Catalog.default().get("kokoro-en-v0_19")
    path = tmp_path / "catalog.json"
    path.write_text(json.dumps({
        "models": [model.to_dict()], "preferred_models": {"tts": model.id}
    }), encoding="utf-8")
    catalog = Catalog.from_json(path)
    assert catalog.preferred_model_id(Modality.TTS) == model.id
    assert catalog.preferred_model_id(Modality.TEXT) is None
    path.write_text(json.dumps({"models": [model.to_dict()]}), encoding="utf-8")
    assert Catalog.from_json(path).preferred_model_id(Modality.TTS) is None


def test_catalog_copies_preferences() -> None:
    preferences = {Modality.TEXT: "missing"}
    catalog = Catalog([], preferred_models=preferences)
    preferences.clear()
    assert catalog.preferred_model_id(Modality.TEXT) == "missing"


@pytest.mark.parametrize("extra", ["speech", "all"])
def test_speech_extras_include_audio_device_runtime(extra: str) -> None:
    path = Path(__file__).resolve().parents[1] / "pyproject.toml"
    data = tomllib.loads(path.read_text(encoding="utf-8"))
    dependencies = data["project"]["optional-dependencies"][extra]
    assert "sounddevice>=0.5" in dependencies
