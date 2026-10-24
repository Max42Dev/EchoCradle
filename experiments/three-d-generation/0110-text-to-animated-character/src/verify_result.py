"""Verify exported GLB skin/animation payloads and record artifact hashes."""
from __future__ import annotations

import argparse
import hashlib
import json
import struct
from pathlib import Path


def inspect_glb(
    path: Path, *, require_textures: bool = False, expected_animations: list[str] | None = None,
) -> dict:
    data = path.read_bytes()
    magic, version, length = struct.unpack_from("<4sII", data)
    if magic != b"glTF" or version != 2 or length != len(data):
        raise ValueError("Invalid GLB header")
    chunk_length, chunk_type = struct.unpack_from("<II", data, 12)
    if chunk_type != 0x4E4F534A:
        raise ValueError("Missing JSON chunk")
    document = json.loads(data[20:20 + chunk_length])
    primitives = [p for mesh in document.get("meshes", []) for p in mesh["primitives"]]
    report = {
        "skins": len(document.get("skins", [])),
        "joints": [len(s["joints"]) for s in document.get("skins", [])],
        "animations": [a.get("name") for a in document.get("animations", [])],
        "animation_channels": [len(a["channels"]) for a in document.get("animations", [])],
        "all_primitives_skinned": all("JOINTS_0" in p["attributes"]
                                      and "WEIGHTS_0" in p["attributes"] for p in primitives),
        "vertex_colors": all("COLOR_0" in p["attributes"] for p in primitives),
        "primitives": len(primitives),
        "uvs": bool(primitives) and all("TEXCOORD_0" in p["attributes"] for p in primitives),
        "embedded_images": sum("bufferView" in image for image in document.get("images", [])),
    }
    if report["skins"] != 1 or not report["all_primitives_skinned"] or not report["animations"]:
        raise ValueError(f"Exported candidate failed structural validation: {report}")
    if require_textures:
        materials = document.get("materials", [])
        textures = document.get("textures", [])
        images = document.get("images", [])
        for primitive in primitives:
            material_index = primitive.get("material", -1)
            if not 0 <= material_index < len(materials):
                raise ValueError("Missing primitive material")
            base = materials[material_index].get("pbrMetallicRoughness", {}).get("baseColorTexture")
            if not isinstance(base, dict) or not 0 <= base.get("index", -1) < len(textures):
                raise ValueError("Missing base-colour texture")
            image_index = textures[base["index"]].get("source", -1)
            if not 0 <= image_index < len(images) or "bufferView" not in images[image_index]:
                raise ValueError("Base-colour image is not embedded")
        if not report["uvs"] or not report["embedded_images"]:
            raise ValueError("Missing embedded textures or UVs")
    if expected_animations is not None and sorted(report["animations"]) != sorted(expected_animations):
        raise ValueError(f"Animation names mismatch: {report['animations']}")
    return report


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--directory", type=Path, required=True)
    args = parser.parse_args()
    report = inspect_glb(args.directory / "SK_courier_mira.glb")
    report["artifacts"] = [
        {"file": p.name, "bytes": p.stat().st_size,
         "sha256": hashlib.sha256(p.read_bytes()).hexdigest()}
        for p in sorted(args.directory.iterdir())
        if p.suffix in (".glb", ".fbx", ".png", ".blend")
    ]
    report["unity_playback"] = "not_run"
    report["quality"] = "research_candidate_not_game_ready"
    (args.directory / "verified_artifacts.json").write_text(json.dumps(report, indent=2))
    print(json.dumps(report, indent=2))


if __name__ == "__main__":
    main()