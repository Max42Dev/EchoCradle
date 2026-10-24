"""Prepare a Blender scene with one selected motion track and a framed camera."""
from __future__ import annotations

import argparse
import sys
from pathlib import Path


def main() -> None:
    import bpy
    from mathutils import Vector

    parser = argparse.ArgumentParser()
    parser.add_argument("--input", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--clip", default="Walk")
    args = parser.parse_args(sys.argv[sys.argv.index("--") + 1:])
    bpy.ops.wm.read_factory_settings(use_empty=True)
    bpy.ops.import_scene.gltf(filepath=str(args.input))
    scene = bpy.context.scene
    rigs = [obj for obj in scene.objects if obj.type == "ARMATURE"]
    meshes = [obj for obj in scene.objects if obj.type == "MESH"]
    if len(rigs) != 1 or not meshes:
        raise RuntimeError("Expected one skinned character")
    rig = rigs[0]
    if not rig.animation_data or not rig.animation_data.nla_tracks:
        raise RuntimeError("No exported animation tracks")
    tracks = {track.name: track for track in rig.animation_data.nla_tracks}
    if args.clip not in tracks:
        raise RuntimeError(f"Clip not found: {args.clip}; found {sorted(tracks)}")
    rig.animation_data.action = None
    bounds = [obj.matrix_world @ Vector(corner) for obj in meshes for corner in obj.bound_box]
    low = Vector([min(point[i] for point in bounds) for i in range(3)])
    high = Vector([max(point[i] for point in bounds) for i in range(3)])
    center = (low + high) * 0.5
    span = max(high - low)
    if span <= 0:
        raise RuntimeError("Character has degenerate bounds")
    for track in tracks.values():
        track.mute = track.name != args.clip
    selected = tracks[args.clip]
    if len(selected.strips) != 1:
        raise RuntimeError("Expected one strip per animation")
    strip = selected.strips[0]
    scene.render.fps = 20
    scene.frame_start = round(strip.frame_start)
    scene.frame_end = round(strip.frame_end)
    scene.frame_set(scene.frame_start)
    scene.render.resolution_x = 900
    scene.render.resolution_y = 900
    scene.render.resolution_percentage = 100
    scene.render.engine = "BLENDER_EEVEE"
    scene.render.image_settings.file_format = "PNG"
    if scene.world is None:
        scene.world = bpy.data.worlds.new("CharacterPreviewWorld")
    scene.world.color = (0.12, 0.14, 0.18)
    bpy.ops.object.camera_add(location=center + Vector((span * 0.62, span * 2.4, span * 0.06)))
    camera = bpy.context.object
    camera.name = "PreviewCamera"
    camera.data.type = "ORTHO"
    camera.data.ortho_scale = span * 1.25
    camera.rotation_euler = (center - camera.location).to_track_quat("-Z", "Y").to_euler()
    scene.camera = camera
    for name, offset, energy, size in (
        ("KeyLight", (1.6, 2.0, 2.2), 750, 2.0),
        ("FillLight", (-1.8, 1.2, 1.1), 450, 2.5),
        ("RimLight", (0.0, -1.8, 1.8), 900, 1.8),
    ):
        bpy.ops.object.light_add(type="AREA", location=center + Vector(offset) * span)
        light = bpy.context.object
        light.name = name
        light.data.energy = energy
        light.data.shape = "DISK"
        light.data.size = size * span
        light.rotation_euler = (center - light.location).to_track_quat("-Z", "Y").to_euler()
    scene.render.filepath = str(args.output.with_suffix(".png"))
    scene.render.image_settings.file_format = "PNG"
    scene.render.resolution_percentage = 80
    bpy.ops.render.render(write_still=True)
    scene.render.resolution_percentage = 100
    scene.render.filepath = "//"
    scene.frame_set(scene.frame_start)
    for screen in bpy.data.screens:
        for area in screen.areas:
            if area.type == "VIEW_3D":
                area.spaces.active.region_3d.view_perspective = "CAMERA"
                area.spaces.active.shading.type = "MATERIAL"
                area.spaces.active.overlay.show_floor = False
    scene.tool_settings.use_keyframe_insert_auto = False
    args.output.parent.mkdir(parents=True, exist_ok=True)
    bpy.ops.wm.save_as_mainfile(filepath=str(args.output))
    print(f"Prepared {args.clip} preview: {args.output}")


if __name__ == "__main__":
    main()