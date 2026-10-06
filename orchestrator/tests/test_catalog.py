"""Catalog loading and descriptor round-tripping."""

from __future__ import annotations

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
