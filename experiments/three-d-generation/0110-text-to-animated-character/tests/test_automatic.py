"""Unattended runner failure contract and actual-export integration checks."""
import json
import struct
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from automatic_pipeline import execute
from verify_result import inspect_glb
from animation_spec import validate_clips


def test_missing_provisioning_records_failure(tmp_path: Path) -> None:
    spec = json.loads((ROOT / "character.pilot.json").read_text())
    result = execute({}, spec, tmp_path)
    assert result["status"] == "failed"
    assert result["human_interventions"] == 0
    assert not result["production_success"]
    assert result["stages"] == []
    assert (tmp_path / "automatic_manifest.json").is_file()


def test_existing_attempt_is_preserved(tmp_path: Path) -> None:
    keeper = tmp_path / "keeper"
    keeper.write_text("do not replace")
    spec = json.loads((ROOT / "character.pilot.json").read_text())
    with pytest.raises(ValueError, match="fresh"):
        execute({}, spec, tmp_path)
    assert keeper.read_text() == "do not replace"


def test_automatic_real_export() -> None:
    directory = ROOT / "results/mira-automatic-2026-10-11"
    if not directory.exists():
        pytest.skip("Local unattended run not present")
    manifest = json.loads((directory / "automatic_manifest.json").read_text())
    assert manifest["status"] == "automatic_structural_candidate_pass"
    assert manifest["human_interventions"] == 0
    assert all(stage["status"] == "completed" for stage in manifest["stages"])
    report = inspect_glb(directory / "SK_courier_mira.glb")
    assert report["joints"] == [28]
    assert report["animations"] == ["Rig_Deformation_Smoke"]
    assert manifest["deformation"]["moved_vertices"] > 0


def test_default_player_clip_spec() -> None:
    clips = validate_clips(json.loads((ROOT / "animations.json").read_text()))
    assert [c["id"] for c in clips] == ["Idle", "Walk", "Run", "Jump", "Attack"]
    assert all(c["root_motion"] == "in_place" for c in clips)


@pytest.mark.parametrize("key,value", [
    ("id", "../escape"), ("frames", 79), ("frames", True), ("fps", 30),
    ("seed", -1), ("seed", True), ("prompt", ""), ("root_motion", "unknown"),
    ("loop", "true"),
])
def test_invalid_clip_is_rejected(key: str, value: object) -> None:
    document = json.loads((ROOT / "animations.json").read_text())
    document["clips"][0][key] = value
    with pytest.raises(ValueError):
        validate_clips(document)


def test_duplicate_clip_is_rejected() -> None:
    document = json.loads((ROOT / "animations.json").read_text())
    document["clips"][1]["id"] = "idle"
    with pytest.raises(ValueError, match="Duplicate"):
        validate_clips(document)


def test_textured_animated_real_export() -> None:
    directory = ROOT / "results/mira-textured-animated-v3-2026-10-11"
    path = directory / "automatic_manifest.json"
    if not path.exists():
        pytest.skip("Local textured animated run not present")
    manifest = json.loads(path.read_text())
    assert manifest["status"] == "automatic_structural_candidate_pass"
    assert manifest["human_interventions"] == 0
    assert not manifest["production_success"]
    assert manifest["textures"]["structural_pass"]
    assert all(m["nonblack_sample_fraction"] >= 0.1 for m in manifest["textures"]["maps"])
    assert manifest["playback"]["structural_pass"]
    assert len(manifest["playback"]["clips"]) == 5
    assert all(s["status"] == "completed" for s in manifest["stages"])
    report = inspect_glb(directory / "SK_courier_mira.glb", require_textures=True,
                         expected_animations=["Idle", "Walk", "Run", "Jump", "Attack"])
    assert report["embedded_images"] >= 1
    assert not report["vertex_colors"]


@pytest.mark.parametrize("defect", ["uv", "texture", "external_image", "clip"])
def test_export_gate_rejects_missing_payload(tmp_path: Path, defect: str) -> None:
    document = {
        "skins": [{"joints": [0]}],
        "meshes": [{"primitives": [{"attributes": {
            "JOINTS_0": 0, "WEIGHTS_0": 1, "TEXCOORD_0": 2}, "material": 0}]}],
        "materials": [{"pbrMetallicRoughness": {"baseColorTexture": {"index": 0}}}],
        "textures": [{"source": 0}], "images": [{"bufferView": 0}],
        "animations": [{"name": "Idle", "channels": [{}]}],
    }
    if defect == "uv":
        del document["meshes"][0]["primitives"][0]["attributes"]["TEXCOORD_0"]
    elif defect == "texture":
        document["materials"][0]["pbrMetallicRoughness"].clear()
    elif defect == "external_image":
        document["images"] = [{"uri": "missing.png"}]
    else:
        document["animations"][0]["name"] = "Rig_Deformation_Smoke"
    encoded = json.dumps(document).encode()
    encoded += b" " * (-len(encoded) % 4)
    path = tmp_path / "malformed.glb"
    path.write_bytes(struct.pack("<4sII", b"glTF", 2, 20 + len(encoded))
                     + struct.pack("<II", len(encoded), 0x4E4F534A) + encoded)
    with pytest.raises(ValueError):
        inspect_glb(path, require_textures=True, expected_animations=["Idle"])