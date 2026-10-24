"""Blender worker: render all exported clips into one labelled, textured MP4."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import subprocess
import sys
import tempfile


def main() -> None:
    import bpy
    from mathutils import Vector

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", type=Path, required=True)
    parser.add_argument("--clips", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--ffmpeg", type=Path, required=True)
    parser.add_argument("--mode", choices=("character", "static", "bvh", "smoke"),
                        default="character")
    parser.add_argument("--clip", help="Only render this exported character clip")
    parser.add_argument("--source-up", choices=("gltf-y", "triposr-z"), default="gltf-y")
    parser.add_argument("--bvh-addon", type=Path)
    parser.add_argument("--bones", action="store_true")
    args = parser.parse_args(sys.argv[sys.argv.index("--") + 1:])
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from animation_spec import validate_clips

    clips = validate_clips({"clips": json.loads(args.clips.read_text(encoding="utf-8"))})
    if args.clip:
        clips = [clip for clip in clips if clip["id"] == args.clip]
        if not clips:
            raise ValueError("Requested clip not found")
    if args.output.exists():
        raise ValueError("Never overwrite an existing review video")
    bpy.ops.wm.read_factory_settings(use_empty=True)
    scene = bpy.context.scene
    scene.render.fps = 20
    if args.mode == "bvh":
        if args.bvh_addon and not hasattr(bpy.ops.import_anim, "bvh"):
            sys.path.insert(0, str(args.bvh_addon))
            import io_anim_bvh

            io_anim_bvh.register()
        bpy.ops.import_anim.bvh(filepath=str(args.input), axis_forward="-Z", axis_up="Y",
                                update_scene_fps=True)
    else:
        bpy.ops.import_scene.gltf(filepath=str(args.input))
        # SkinTokens' unweighted helper sphere is not part of the rendered character.
        for obj in list(scene.objects):
            if obj.type == "MESH" and obj.name == "Icosphere" and not obj.vertex_groups:
                bpy.data.objects.remove(obj, do_unlink=True)
    if args.source_up == "triposr-z":
        import math
        from mathutils import Matrix

        rotation = Matrix.Rotation(-math.pi / 2, 4, "X")
        for obj in [obj for obj in scene.objects if obj.parent is None]:
            obj.matrix_world = rotation @ obj.matrix_world
        bpy.context.view_layer.update()
    rigs = [obj for obj in scene.objects if obj.type == "ARMATURE"]
    meshes = [obj for obj in scene.objects if obj.type == "MESH"]
    if args.mode != "static" and (len(rigs) != 1 or not rigs[0].animation_data):
        raise RuntimeError("Review requires one animated rig and character meshes")
    rig = rigs[0] if rigs else None
    animation = rig.animation_data if rig else None
    original_action = animation.action if animation else None
    original_slot = animation.action_slot if animation else None
    tracks = {track.name: track for track in animation.nla_tracks} if animation else {}
    if animation:
        animation.action = None
    for track in tracks.values():
        track.mute = True

    helpers = []
    if rig and (args.mode == "bvh" or args.bones):
        # Renderable joints/links make a bare rig or raw BVH visible, unlike viewport bones.
        size = max((rig.matrix_world @ b.tail - rig.matrix_world @ b.head).length
                   for b in rig.pose.bones) * 0.07
        material = bpy.data.materials.new("DebugSkeleton")
        material.diffuse_color = (1, 0.2, 0.03, 1)
        for bone in rig.pose.bones:
            bpy.ops.mesh.primitive_uv_sphere_add(segments=8, ring_count=4, radius=size)
            joint = bpy.context.object
            joint.data.materials.append(material)
            bpy.ops.mesh.primitive_cylinder_add(vertices=8, radius=size * 0.5, depth=1)
            link = bpy.context.object
            link.data.materials.append(material)
            helpers.append((bone, joint, link))
        meshes += [obj for _, joint, link in helpers for obj in (joint, link)]

    def set_frame(frame: int) -> None:
        scene.frame_set(frame)
        for bone, joint, link in helpers:
            head, tail = rig.matrix_world @ bone.head, rig.matrix_world @ bone.tail
            joint.location = head
            link.location = (head + tail) / 2
            link.rotation_euler = (tail - head).to_track_quat("Z", "Y").to_euler()
            link.scale.z = (tail - head).length
        bpy.context.view_layer.update()

    def activate(clip: dict) -> tuple[int, int]:
        if args.mode == "static":
            return 1, 1
        if args.mode in ("bvh", "smoke"):
            if original_action:
                animation.action = original_action
                animation.action_slot = original_slot
            elif tracks:
                strip = next(iter(tracks.values())).strips[0]
                animation.action = strip.action
                animation.action_slot = strip.action_slot
            else:
                raise RuntimeError("No source animation")
            return tuple(round(v) for v in animation.action.frame_range)
        track = tracks.get(clip["id"])
        if track is None or len(track.strips) != 1:
            raise RuntimeError(f"Missing exported clip: {clip['id']}")
        strip = track.strips[0]
        rig.animation_data.action = strip.action
        rig.animation_data.action_slot = strip.action_slot
        start, end = (round(v) for v in strip.action.frame_range)
        if end - start + 1 != clip["frames"]:
            raise RuntimeError(f"Unexpected exported frame count: {clip['id']}")
        return start, end

    if args.mode in ("static", "bvh", "smoke"):
        clips = clips[:1]

    # Bound every animated pose before rendering: one fixed camera, no cropped jumps/travel.
    low = Vector((float("inf"),) * 3)
    high = Vector((float("-inf"),) * 3)
    for clip in clips:
        start, end = activate(clip)
        for frame in range(start, end + 1):
            set_frame(frame)
            depsgraph = bpy.context.evaluated_depsgraph_get()
            for obj in meshes:
                evaluated = obj.evaluated_get(depsgraph)
                for corner in evaluated.bound_box:
                    point = evaluated.matrix_world @ Vector(corner)
                    for axis in range(3):
                        low[axis] = min(low[axis], point[axis])
                        high[axis] = max(high[axis], point[axis])
    center = (low + high) / 2
    span = max(high - low)
    if not 0 < span < 100:
        raise RuntimeError("Invalid animated character bounds")
    bpy.ops.object.camera_add(location=center + Vector((1.5, 3, 0.7)) * span)
    camera = bpy.context.object
    camera.rotation_euler = (center - camera.location).to_track_quat("-Z", "Y").to_euler()
    camera.data.type = "ORTHO"
    camera.data.ortho_scale = span * 1.8
    camera.data.clip_end = span * 20
    scene.camera = camera
    scene.render.engine = "BLENDER_EEVEE"
    scene.render.resolution_x = scene.render.resolution_y = 512
    scene.render.resolution_percentage = 100
    scene.render.image_settings.file_format = "PNG"
    scene.world = bpy.data.worlds.new("ReviewWorld")
    scene.world.use_nodes = True
    scene.world.node_tree.nodes["Background"].inputs["Color"].default_value = (
        0.12, 0.12, 0.12, 1,
    )
    for offset, power in [((2, 3, 3), 500), ((-2, 1, 1), 300)]:
        bpy.ops.object.light_add(type="AREA", location=center + Vector(offset) * span)
        light = bpy.context.object
        light.data.energy = power
        light.data.shape = "DISK"
        light.data.size = span * 2
        light.rotation_euler = (center - light.location).to_track_quat("-Z", "Y").to_euler()
    label_material = bpy.data.materials.new("ReviewLabel")
    label_material.use_nodes = True
    nodes = label_material.node_tree.nodes
    nodes.clear()
    emission = nodes.new("ShaderNodeEmission")
    emission.inputs["Color"].default_value = (1, 1, 1, 1)
    label_material.node_tree.links.new(
        emission.outputs[0], nodes.new("ShaderNodeOutputMaterial").inputs["Surface"],
    )
    bpy.ops.object.text_add()
    label = bpy.context.object
    label.parent = camera
    scale = camera.data.ortho_scale
    label.location = (-scale * 0.46, scale * 0.42, -span)
    label.rotation_euler = (0, 0, 0)
    label.data.size = scale * 0.045
    label.data.materials.append(label_material)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    if args.mode == "static":
        label.data.body = args.input.stem
        set_frame(1)
        scene.render.filepath = str(args.output)
        bpy.ops.render.render(write_still=True)
        return
    command = [str(args.ffmpeg), "-nostdin", "-n", "-loglevel", "error",
               "-f", "image2pipe", "-vcodec", "png", "-framerate", "20", "-i", "pipe:0",
               "-an", "-c:v", "libx264", "-preset", "fast", "-crf", "22",
               "-pix_fmt", "yuv420p", "-movflags", "+faststart", str(args.output)]
    # Keep only one intermediate frame on disk; no full PNG sequence or model downloads.
    with tempfile.TemporaryDirectory(prefix="echocradle-review-") as temporary:
        frame_path = Path(temporary) / "frame.png"
        with subprocess.Popen(command, stdin=subprocess.PIPE) as encoder:
            try:
                for index, clip in enumerate(clips, 1):
                    start, end = activate(clip)
                    label.data.body = f"{index}/{len(clips)}  {clip['id']}"
                    for frame in range(start, end + 1):
                        set_frame(frame)
                        scene.render.filepath = str(frame_path)
                        bpy.ops.render.render(write_still=True)
                        encoder.stdin.write(frame_path.read_bytes())
                encoder.stdin.close()
                if encoder.wait(timeout=30) != 0:
                    raise RuntimeError("Local MP4 encoding failed")
            except BaseException:
                encoder.kill()
                encoder.wait()
                args.output.unlink(missing_ok=True)
                raise
    print(json.dumps({"file": str(args.output), "clips": [c["id"] for c in clips],
                      "fps": 20, "frames": sum(c["frames"] for c in clips)}))


if __name__ == "__main__":
    main()