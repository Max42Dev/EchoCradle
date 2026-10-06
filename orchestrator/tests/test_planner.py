"""Planner: model selection against a synthetic probe report."""

from __future__ import annotations

import pytest

from orchestrator.catalog import Catalog, Modality
from orchestrator.errors import ModelNotAvailableError
from orchestrator.planner import Planner
from orchestrator.probe import GpuInfo, ProbeReport


def _report(vram_gb: float, ram_gb: float = 16.0) -> ProbeReport:
    gpus = [GpuInfo("test", vram_gb, 0.5)] if vram_gb > 0 else []
    return ProbeReport(
        gpus=gpus,
        ram_gb=ram_gb,
        free_disk_gb=100.0,
        tier=3,
        min_free_vram_gb=vram_gb - 0.5 if gpus else 0.0,
    )


def test_picks_best_quality_that_fits():
    catalog = Catalog.default()
    planner = Planner(catalog, _report(24.0))
    plan = planner.plan(Modality.TEXT)
    # The highest-quality text model that fits the budget. Granite 4.2-8B is
    # preferred over the older, larger Qwen2.5-14B because it scores higher.
    assert plan.model.id == "granite-4.2-8b-q4km"
    assert plan.model.quality == max(
        m.quality for m in catalog.by_modality(Modality.TEXT) if m.vram_gb <= _report(24.0).usable_vram_gb
    )


def test_small_gpu_gets_smaller_model():
    catalog = Catalog.default()
    planner = Planner(catalog, _report(8.0))
    plan = planner.plan(Modality.TEXT)
    assert plan.model.vram_gb <= _report(8.0).usable_vram_gb
    assert plan.model.tier <= 2


def test_no_gpu_falls_back_to_ram():
    catalog = Catalog.default()
    planner = Planner(catalog, _report(0.0, ram_gb=16.0))
    plan = planner.plan(Modality.TEXT)
    assert plan.model.ram_gb <= 16.0


def test_no_gpu_and_tiny_ram_fails_cleanly():
    catalog = Catalog.default()
    planner = Planner(catalog, _report(0.0, ram_gb=1.0))
    with pytest.raises(ModelNotAvailableError):
        planner.plan(Modality.TEXT)


def test_speech_always_fits_even_without_gpu():
    catalog = Catalog.default()
    planner = Planner(catalog, _report(0.0, ram_gb=8.0))
    assert planner.plan(Modality.TTS).model.modality is Modality.TTS
    assert planner.plan(Modality.STT).model.modality is Modality.STT


def test_shippable_only_excludes_non_commercial():
    catalog = Catalog.default()
    planner = Planner(catalog, _report(24.0))
    plan = planner.plan(Modality.TEXT, shippable_only=True)
    assert plan.model.shippable


def test_explicit_model_id_is_honoured():
    catalog = Catalog.default()
    planner = Planner(catalog, _report(24.0))
    plan = planner.plan(Modality.TEXT, model_id="qwen2.5-1.5b-instruct-q4km")
    assert plan.model.id == "qwen2.5-1.5b-instruct-q4km"


def test_explicit_model_of_wrong_modality_is_rejected():
    catalog = Catalog.default()
    planner = Planner(catalog, _report(24.0))
    with pytest.raises(ModelNotAvailableError):
        planner.plan(Modality.TEXT, model_id="piper-en-amy-low")


def test_explicit_model_that_does_not_fit_is_rejected():
    catalog = Catalog.default()
    planner = Planner(catalog, _report(6.0))
    with pytest.raises(ModelNotAvailableError):
        planner.plan(Modality.TEXT, model_id="qwen2.5-14b-instruct-q4km")
