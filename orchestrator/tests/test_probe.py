"""Probe: tiering, the VRAM budget rule, and fit tests."""

from __future__ import annotations

from orchestrator.probe import (
    FRAGMENTATION_SLACK,
    GpuInfo,
    ProbeReport,
    _clamp01,
    _norm,
    _tier_from_score,
)


def test_clamp_and_norm():
    assert _clamp01(-1.0) == 0.0
    assert _clamp01(2.0) == 1.0
    assert _norm(24.0, 4.0, 48.0) == (24.0 - 4.0) / (48.0 - 4.0)
    assert _norm(0.0, 10.0, 10.0) == 0.0


def test_tier_bands_follow_vram():
    assert _tier_from_score(0.0, 0.0) == 0
    assert _tier_from_score(0.2, 8.0) == 1
    assert _tier_from_score(0.4, 12.0) == 2
    assert _tier_from_score(0.6, 24.0) == 3
    assert _tier_from_score(0.9, 48.0) == 4


def test_usable_vram_applies_reserve_and_slack():
    report = ProbeReport(
        gpus=[GpuInfo("g", 24.0, 2.0)],
        min_free_vram_gb=22.0,
        reserve_gb=3.0,
    )
    expected = (22.0 - 3.0) * (1.0 - FRAGMENTATION_SLACK)
    assert abs(report.usable_vram_gb - expected) < 1e-9


def test_usable_vram_is_zero_when_reserve_exceeds_free():
    report = ProbeReport(gpus=[GpuInfo("g", 24.0, 22.0)], min_free_vram_gb=2.0, reserve_gb=3.0)
    assert report.usable_vram_gb == 0.0


def test_fits_uses_usable_vram_not_total():
    report = ProbeReport(
        gpus=[GpuInfo("g", 24.0, 2.0)],
        min_free_vram_gb=22.0,
        reserve_gb=3.0,
        ram_gb=16.0,
    )
    assert report.fits(10.0)
    assert not report.fits(report.usable_vram_gb + 1.0)


def test_cpu_only_model_fits_by_ram():
    report = ProbeReport(gpus=[], ram_gb=8.0)
    assert report.fits(0.0, ram_gb=1.0)
    assert not report.fits(0.0, ram_gb=32.0)


def test_gpu_model_without_gpu_falls_back_to_ram():
    report = ProbeReport(gpus=[], ram_gb=16.0)
    assert report.fits(6.0, ram_gb=6.0)
    assert not report.fits(6.0, ram_gb=0.0)


def test_to_dict_is_json_safe():
    import json

    report = ProbeReport(gpus=[GpuInfo("g", 24.0, 2.0)], ram_gb=16.0, free_disk_gb=50.0)
    json.dumps(report.to_dict())
