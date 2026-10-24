"""Visual debug logging is separate from generation and retains partial evidence."""
import json
from pathlib import Path
import subprocess
import sys

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
import debug_capture
import automatic_pipeline


def fixture_run(tmp_path: Path) -> tuple[dict, Path]:
    output = tmp_path / "run"
    output.mkdir()
    config = {"python": "python", "blender": "blender", "bvh_addon": str(tmp_path),
              "animation_spec": str(ROOT / "animations.json")}
    clips = json.loads((ROOT / "animations.json").read_text())["clips"]
    (output / "motion_set.json").write_text(json.dumps(clips))
    (output / "input_spec.json").write_text('{"id":"mira"}')
    (output / "foreground.png").write_bytes(b"png")
    (output / "reconstruction_input.png").write_bytes(b"png")
    (output / "mesh.glb").write_bytes(b"mesh")
    (output / "rigged.glb").write_bytes(b"rig")
    (output / "SK_mira.glb").write_bytes(b"final")
    (output / "SK_mira.fbx").write_bytes(b"fbx")
    (output / "mira.blend").write_bytes(b"blend")
    for name in ("retarget_report.json", "playback_report.json"):
        (output / name).write_text("{}")
    reference = output / "reference_0"
    reference.mkdir()
    for name in ("reference.png", "foreground.png", "foreground_mask.png"):
        (reference / name).write_bytes(b"png")
    (reference / "framing_qa.json").write_text('{"pass":true}')
    prepared = output / "prepared"
    prepared.mkdir()
    for name in ("T_mira.png", "preview_front.png", "preview_three_quarter.png"):
        (prepared / name).write_bytes(b"png")
    (prepared / "SK_mira.glb").write_bytes(b"smoke")
    for clip in clips:
        motion = output / "motions" / clip["id"]
        motion.mkdir(parents=True)
        (motion / "motion.bvh").write_text("motion")
    return config, output


def test_numbered_bundle_contains_every_stage(tmp_path: Path, monkeypatch) -> None:
    config, output = fixture_run(tmp_path)
    encoder = tmp_path / "ffmpeg"
    encoder.touch()
    monkeypatch.setattr(debug_capture.subprocess, "check_output", lambda *a, **k: str(encoder))
    commands = []

    def render(name, command, cwd, expected, logs, env, timeout, stages) -> None:
        commands.append(command)
        expected.write_bytes(b"preview")

    monkeypatch.setattr(debug_capture, "run_stage", render)
    report = debug_capture.capture_debug(config, output, {"id": "mira"}, {}, 30,
                                         tmp_path / "results")
    assert report["status"] == "completed"
    folder = Path(report["folder"])
    assert folder.parent == tmp_path / "results"
    assert folder.name.startswith("mira_debug_") and folder.name.endswith("Z")
    assert [r["step"] for r in report["steps"]] == list(range(len(report["steps"])))
    labels = [r["label"] for r in report["steps"]]
    assert labels[-1] == "final_video"
    assert "background_removed" in labels and "mesh" in labels and "rig" in labels
    assert len([label for label in labels if label.startswith("motion_")]) == 5
    assert len([label for label in labels if label.startswith("retarget_")]) == 5
    assert len(list(folder.glob("*.mp4"))) == 12
    assert len([c for c in commands if c[-1] == "triposr-z"]) == 1
    assert (folder / "README.md").is_file()
    assert json.loads((folder / "debug_manifest.json").read_text())["required_for_pipeline"] is False


def test_preview_failure_keeps_other_evidence(tmp_path: Path, monkeypatch) -> None:
    config, output = fixture_run(tmp_path)
    encoder = tmp_path / "ffmpeg"
    encoder.touch()
    monkeypatch.setattr(debug_capture.subprocess, "check_output", lambda *a, **k: str(encoder))

    def render(name, command, cwd, expected, logs, env, timeout, stages) -> None:
        expected.write_bytes(b"partial")
        if "mesh" in expected.name:
            raise RuntimeError("Renderer failed")

    monkeypatch.setattr(debug_capture, "run_stage", render)
    report = debug_capture.capture_debug(config, output, {"id": "mira"}, {}, 30,
                                         tmp_path / "results")
    assert report["status"] == "partial"
    mesh = next(r for r in report["steps"] if r["label"] == "mesh")
    assert mesh["status"] == "failed"
    folder = Path(report["folder"])
    assert not (folder / f"step_{mesh['step']}_mesh.png").exists()
    assert report["steps"][-1]["status"] == "completed"
    assert (output / "mesh.glb").read_bytes() == b"mesh"


def test_missing_run_is_non_gating(tmp_path: Path) -> None:
    report = debug_capture.capture_debug({}, tmp_path, {"id": "mira"}, {}, 30,
                                         tmp_path / "results")
    assert report["status"] == "failed"
    assert report["required_for_pipeline"] is False


@pytest.mark.parametrize("enabled", [True, False])
def test_debug_capture_does_not_change_pipeline_success(
    tmp_path: Path, monkeypatch, enabled: bool,
) -> None:
    config = json.loads((ROOT / "automatic.local.json").read_text())
    for key, value in list(config.items()):
        if isinstance(value, str) and ":" in value:
            config[key] = str(ROOT / "animations.json") if key == "animation_spec" else str(ROOT)
    config["debug_capture"] = enabled
    revisions = iter(config[f"{key}_revision"] for key in ("tripo", "rig", "motion"))

    def git_probe(command, **kwargs) -> str:
        return next(revisions) if "rev-parse" in command else ""

    def fake_stage(name, command, cwd, expected, output, env, timeout, stages) -> None:
        expected.parent.mkdir(parents=True, exist_ok=True)
        expected.write_text('{"pass":true}')
        stages.append({"name": name, "status": "completed"})
        if name == "export_render":
            (expected.parent / "rig_inspection.json").write_text('{"deformation_smoke":{}}')
            (expected.parent / "texture_report.json").write_text("{}")

    calls = []

    def failed_capture(*args) -> dict:
        calls.append(args)
        return {"status": "failed", "required_for_pipeline": False}

    monkeypatch.setattr(automatic_pipeline.subprocess, "check_output", git_probe)
    monkeypatch.setattr(automatic_pipeline, "run_stage", fake_stage)
    monkeypatch.setattr(automatic_pipeline, "inspect_glb", lambda *a, **k: {})
    monkeypatch.setattr(automatic_pipeline, "capture_debug", failed_capture)
    spec = json.loads((ROOT / "character.pilot.json").read_text())
    manifest = automatic_pipeline.execute(config, spec, tmp_path)
    assert manifest["status"] == "automatic_structural_candidate_pass"
    assert len(calls) == int(enabled)
    assert ("debug_capture" in manifest) == enabled
    assert (tmp_path / "automatic_manifest.json").is_file()