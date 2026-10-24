"""No models or GPU needed: orchestration failure/approval contract tests."""

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
import pipeline


def spec() -> dict:
    return json.loads((ROOT / "character.json").read_text(encoding="utf-8"))


def test_character_spec() -> None:
    pipeline.validate_spec(spec())


@pytest.mark.parametrize("key,value", [("id", "../escape"), ("seed", True),
                                      ("width", 513), ("height_m", float("nan"))])
def test_reject_invalid_spec(key: str, value: object) -> None:
    data = spec()
    data[key] = value
    with pytest.raises(ValueError):
        pipeline.validate_spec(data)


def test_preflight_records_blockers(tmp_path: Path) -> None:
    assert pipeline.main(["--output", str(tmp_path)]) == 0
    manifest = json.loads((tmp_path / "manifest.json").read_text())
    assert manifest["status"] == "blocked"
    assert manifest["stages"] == []
    assert not manifest["character_success"]
    assert "approval_required:licenses_approved" in manifest["preflight"]["issues"]


def test_refuse_overwrite(tmp_path: Path) -> None:
    (tmp_path / "keeper").write_text("existing asset")
    with pytest.raises(SystemExit):
        pipeline.main(["--output", str(tmp_path)])
    assert (tmp_path / "keeper").read_text() == "existing asset"


def test_worker_failure_is_recorded(tmp_path: Path) -> None:
    stages = []
    with pytest.raises(RuntimeError):
        pipeline.run_stage("bad", [sys.executable, "-c", "raise SystemExit(3)"],
                           tmp_path, tmp_path / "missing", tmp_path, dict(os.environ), 10, stages)
    assert stages[0]["status"] == "failed"
    assert stages[0]["returncode"] == 3


def test_worker_success_hashes_output(tmp_path: Path) -> None:
    artifact = tmp_path / "tiny.glb"
    stages = []
    pipeline.run_stage("test", [sys.executable, "-c",
                       "from pathlib import Path; Path('tiny.glb').write_bytes(b'test')"],
                       tmp_path, artifact, tmp_path, dict(os.environ), 10, stages)
    assert stages[0]["sha256"] == pipeline.digest(artifact)
    assert stages[0]["status"] == "completed"


def test_workers_compile() -> None:
    for path in (ROOT / "src").glob("*.py"):
        compile(path.read_text(encoding="utf-8"), str(path), "exec")


def test_timeout_is_recorded(tmp_path: Path) -> None:
    stages = []
    with pytest.raises(subprocess.TimeoutExpired):
        pipeline.run_stage("timeout", [sys.executable, "-c",
                           "import threading; threading.Event().wait()"],
                           tmp_path, tmp_path / "missing", tmp_path, dict(os.environ), 1, stages)
    assert stages[0]["status"] == "failed"


def test_actual_export_has_skin_and_animation() -> None:
    from verify_result import inspect_glb

    asset = ROOT / "results/mira-2026-10-11/SK_courier_mira.glb"
    if not asset.exists():
        pytest.skip("Local generation artifact not present")
    report = inspect_glb(asset)
    assert report["joints"] == [34]
    assert report["vertex_colors"]
    assert "Rig_Deformation_Smoke" in report["animations"]