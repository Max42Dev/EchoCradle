"""Reimport exported motion GLB, measure playback and render a sampled GIF."""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


def main() -> None:
    import bpy
    from mathutils import Vector

    parser = argparse.ArgumentParser()
    parser.add_argument("--input", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--diagnostic", action="store_true", help="Render rejected motion for diagnosis")
    args = parser.parse_args(sys.argv[sys.argv.index("--") + 1:])
    bpy.ops.wm.read_factory_settings(use_empty=True)
    bpy.context.scene.render.fps = 20
    bpy.ops.import_scene.gltf(filepath=str(args.input))
    rig = next(o for o in bpy.context.scene.objects if o.type == "ARMATURE")
    meshes = [o for o in bpy.context.scene.objects if o.type == "MESH"]
    scene = bpy.context.scene
    scene.render.fps = 20
    start, end = [int(v) for v in rig.animation_data.action.frame_range]
    scene.frame_set(start)
    initial = [v.co.copy() for v in meshes[0].evaluated_get(
        bpy.context.evaluated_depsgraph_get()).data.vertices]
    root = next(b for b in rig.pose.bones if b.parent is None)
    root_positions = []
    length_error = 0.0
    for frame in range(start, end + 1):
        scene.frame_set(frame)
        root_positions.append((rig.matrix_world @ root.head).copy())
        for bone in rig.pose.bones:
            if bone.parent:
                rest = (rig.data.bones[bone.name].head_local - rig.data.bones[
                    bone.parent.name].head_local).length
                current = (bone.head - bone.parent.head).length
                length_error = max(length_error, abs(rest - current))
    scene.frame_set((start + end) // 2)
    evaluated = meshes[0].evaluated_get(bpy.context.evaluated_depsgraph_get())
    displacement = max((v.co - p).length for v, p in zip(evaluated.data.vertices, initial))
    report = {"frames": end - start + 1, "fps": 20, "duration_seconds": (end - start + 1) / 20,
              "root_travel_m": (root_positions[-1] - root_positions[0]).length,
              "maximum_joint_offset_length_error_local": length_error,
              "midpoint_vertex_displacement_local": displacement,
              "contact_and_anatomical_quality": "not_verified"}
    args.output.mkdir(parents=True, exist_ok=True)
    report["structural_pass"] = displacement > 0 and length_error <= 0.005
    report["status"] = "candidate" if report["structural_pass"] else "rejected"
    (args.output / "playback_report.json").write_text(json.dumps(report, indent=2))
    if not report["structural_pass"] and not args.diagnostic:
        raise RuntimeError(f"Playback structural gate failed: {report}")
    scene.render.engine = "BLENDER_WORKBENCH"
    scene.display.shading.light = "STUDIO"
    scene.display.shading.color_type = "VERTEX"
    scene.display.shading.background_type = "WORLD"
    scene.world = bpy.data.worlds.new("MotionPreview")
    scene.world.color = (0.2, 0.2, 0.2)
    scene.render.resolution_x = 512
    scene.render.resolution_y = 512
    scene.render.resolution_percentage = 100
    bpy.ops.object.camera_add()
    camera = bpy.context.object
    camera.data.type = "ORTHO"
    camera.data.ortho_scale = 2.4
    scene.camera = camera
    for index, frame in enumerate(range(start, end + 1, 6)):
        scene.frame_set(frame)
        center = rig.matrix_world @ root.head + Vector((0, 0, 0.3))
        camera.location = center + Vector((2.2, 3, 1))
        camera.rotation_euler = (center - camera.location).to_track_quat("-Z", "Y").to_euler()
        scene.render.filepath = str(args.output / f"frame_{index:02}.png")
        bpy.ops.render.render(write_still=True)
    print(json.dumps(report))


if __name__ == "__main__":
    main()