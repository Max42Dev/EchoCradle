"""Check the executed quality candidate's structural export and comparison evidence."""
import json
from pathlib import Path
import sys

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from verify_result import inspect_glb


def test_quality_candidate_retains_clips_and_textures() -> None:
    directory = ROOT / "results/mira-quality-texture-v2-2026-10-11"
    if not (directory / "SK_courier_mira.glb").is_file():
        pytest.skip("Executed local quality comparison not present")
    report = inspect_glb(directory / "SK_courier_mira.glb", require_textures=True,
                         expected_animations=["Idle", "Walk", "Run", "Jump", "Attack"])
    assert report["joints"] == [28]
    assert not report["vertex_colors"]
    playback = json.loads((directory / "playback_report.json").read_text())
    assert playback["structural_pass"]
    assert len(playback["clips"]) == 5
    texture = json.loads((directory / "prepared/texture_report.json").read_text())
    assert texture["maps"][0]["uv_method"] == "welded_copy"
    for variant in ("vertex_control", "baseline_bake", "welded_uv_bake"):
        assert (directory / variant / "face.png").is_file()