"""Executed motion/retarget artifact checks; no inference during tests."""
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from verify_result import inspect_glb


def test_generated_motion_report() -> None:
    path = ROOT / "results/motion-walk-2026-10-11/motion_report.json"
    if not path.exists():
        pytest.skip("Local motion artifacts absent")
    report = json.loads(path.read_text())
    assert report["frames"] == 120 and report["joints"] == 22
    assert report["fps"] == 20
    assert report["bvh_fit_rmse"] < 0.05


def test_corrected_retarget_playback() -> None:
    directory = ROOT / "results/mira-walk-corrected-2026-10-11"
    if not directory.exists():
        pytest.skip("Local retarget artifacts absent")
    report = json.loads((directory / "playback/playback_report.json").read_text())
    assert report["structural_pass"] and report["frames"] == 120
    assert report["maximum_joint_offset_length_error_local"] < 0.005
    exported = inspect_glb(directory / "SK_courier_mira_walk.glb")
    assert "MoMask_Walk_Forward" in exported["animations"]