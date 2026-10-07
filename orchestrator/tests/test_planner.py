"""Planner: model selection against a synthetic probe report."""

from __future__ import annotations

from dataclasses import replace

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


@pytest.mark.parametrize("modality, expected", [
    (Modality.TEXT, "granite-4.2-8b-q4km"),
    (Modality.TTS, "kokoro-en-v0_19"),
    (Modality.STT, "sensevoice-small"),
])
def test_shipped_preferred_defaults(modality: Modality, expected: str) -> None:
    plan = Planner(Catalog.default(), _report(24.0)).plan(modality)
    assert plan.model.id == expected
    assert "catalog preference" in plan.reason


def test_preference_wins_over_higher_quality_when_eligible() -> None:
    preferred = Catalog.default().get("granite-4.2-8b-q4km")
    better = replace(preferred, id="higher-quality", quality=0.99)
    catalog = Catalog([better, preferred], preferred_models={Modality.TEXT: preferred.id})
    plan = Planner(catalog, _report(24.0)).plan(Modality.TEXT)
    assert plan.model == preferred
    assert plan.candidates_considered == 2


@pytest.mark.parametrize("preference", ["missing", "kokoro-en-v0_19"])
def test_missing_or_wrong_modality_preference_falls_back(preference: str) -> None:
    models = list(Catalog.default())
    catalog = Catalog(models, preferred_models={Modality.TEXT: preference})
    plan = Planner(catalog, _report(24.0)).plan(Modality.TEXT)
    assert plan.model.id == "granite-4.2-8b-q4km"
    assert "best of" in plan.reason


def test_custom_catalog_without_preferences_keeps_quality_selection() -> None:
    preferred = Catalog.default().get("granite-4.2-8b-q4km")
    better = replace(preferred, id="custom", quality=0.99)
    assert Planner(Catalog([preferred, better]), _report(24.0)).plan(
        Modality.TEXT
    ).model == better


def test_preference_below_min_quality_falls_back() -> None:
    preferred = Catalog.default().get("granite-4.2-8b-q4km")
    better = replace(preferred, id="custom", quality=0.99)
    catalog = Catalog([preferred, better], preferred_models={Modality.TEXT: preferred.id})
    assert Planner(catalog, _report(24.0)).plan(Modality.TEXT, min_quality=0.9).model == better


@pytest.mark.parametrize("modality, report, expected", [
    (Modality.TEXT, _report(8.0), "granite-4.2-3b-q4km"),
    (Modality.TTS, _report(0.0, 0.3), "piper-en-lessac-medium"),
    (Modality.STT, _report(0.0, 0.8), "zipformer-en-streaming"),
])
def test_nonfitting_preference_falls_back(
    modality: Modality, report: ProbeReport, expected: str
) -> None:
    plan = Planner(Catalog.default(), report).plan(modality)
    assert plan.model.id == expected
    assert "best of" in plan.reason


def test_disallowed_stt_preference_falls_back() -> None:
    plan = Planner(Catalog.default(), _report(24.0)).plan(Modality.STT, shippable_only=True)
    assert plan.model.id == "whisper-base-en"


def test_explicit_nonshippable_model_is_rejected() -> None:
    with pytest.raises(ModelNotAvailableError, match="shippable"):
        Planner(Catalog.default(), _report(24.0)).plan(
            Modality.STT, model_id="sensevoice-small", shippable_only=True
        )


def test_explicit_model_below_min_quality_is_rejected() -> None:
    with pytest.raises(ModelNotAvailableError, match="minimum quality"):
        Planner(Catalog.default(), _report(24.0)).plan(
            Modality.TEXT, model_id="qwen2.5-1.5b-instruct-q4km", min_quality=0.9
        )
