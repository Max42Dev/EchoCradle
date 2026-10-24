"""Independent GLB reimport gates for every named clip and embedded albedo."""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


def main() -> None:
    import bpy

    parser = argparse.ArgumentParser()
    parser.add_argument("--input", type=Path, required=True)
    parser.add_argument("--clips", type=Path, required=True)
    parser.add_argument("--report", type=Path, required=True)
    args = parser.parse_args(sys.argv[sys.argv.index("--") + 1:])
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from verify_result import inspect_glb

    clips = json.loads(args.clips.read_text())
    inspect_glb(args.input, require_textures=True,
                expected_animations=[clip["id"] for clip in clips])
    bpy.ops.wm.read_factory_settings(use_empty=True)
    scene = bpy.context.scene
    scene.render.fps = 20
    bpy.ops.import_scene.gltf(filepath=str(args.input))
    rigs = [obj for obj in scene.objects if obj.type == "ARMATURE"]
    if len(rigs) != 1:
        raise RuntimeError("Playback requires exactly one rig")
    rig = rigs[0]
    meshes = [obj for obj in scene.objects if obj.type == "MESH"]
    if any(not obj.data.uv_layers for obj in meshes):
        raise RuntimeError("Reimport lost UVs")
    tracks = {track.name: track for track in rig.animation_data.nla_tracks}
    rig.animation_data.action = None
    records = []
    for clip in clips:
        track = tracks.get(clip["id"])
        if track is None or len(track.strips) != 1:
            raise RuntimeError(f"Missing reimported clip: {clip['id']}")
        for item in tracks.values():
            item.mute = True
        strip = track.strips[0]
        rig.animation_data.action = strip.action
        rig.animation_data.action_slot = strip.action_slot
        start, end = [round(v) for v in strip.action.frame_range]
        scene.frame_set(start)
        initial = [[v.co.copy() for v in obj.evaluated_get(
            bpy.context.evaluated_depsgraph_get()).data.vertices] for obj in meshes]
        root = next(b for b in rig.pose.bones if b.parent is None)
        origin = (rig.matrix_world @ root.head).copy()
        length_error = displacement = horizontal_travel = 0.0
        for frame in range(start, end + 1):
            scene.frame_set(frame)
            delta = rig.matrix_world @ root.head - origin
            horizontal_travel = max(horizontal_travel, (delta.x ** 2 + delta.y ** 2) ** 0.5)
            for bone in rig.pose.bones:
                if bone.parent:
                    rest = (bone.bone.head_local - bone.parent.bone.head_local).length
                    current = (bone.head - bone.parent.head).length
                    length_error = max(length_error, abs(rest - current))
            depsgraph = bpy.context.evaluated_depsgraph_get()
            for obj, baseline in zip(meshes, initial):
                evaluated = obj.evaluated_get(depsgraph)
                displacement = max(displacement, max(
                    (v.co - point).length for v, point in zip(evaluated.data.vertices, baseline)))
        passed = (end - start + 1 == clip["frames"] and length_error <= 0.005
                  and displacement > 0.00001
                  and (clip["root_motion"] != "in_place" or horizontal_travel <= 0.005))
        records.append({"id": clip["id"], "frames": end - start + 1,
                        "maximum_joint_offset_length_error_local": length_error,
                        "maximum_vertex_displacement_local": displacement,
                        "maximum_horizontal_root_travel_m": horizontal_travel,
                        "structural_pass": passed})
    report = {"clips": records, "structural_pass": all(r["structural_pass"] for r in records),
              "fps": 20, "anatomical_contact_loop_quality": "not_verified", "unity": "not_run"}
    args.report.write_text(json.dumps(report, indent=2), encoding="utf-8")
    if not report["structural_pass"]:
        raise RuntimeError(f"Independent playback gate rejected clips: {report}")
    print(json.dumps(report))


if __name__ == "__main__":
    main()