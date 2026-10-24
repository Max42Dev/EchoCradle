"""Conservative topology-based humanoid mapping and world-rest-delta BVH retarget.

Reject unexpected topology rather than silently assign arbitrary bone names.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


def main() -> None:
    import bpy
    from mathutils import Matrix, Vector

    parser = argparse.ArgumentParser()
    parser.add_argument("--character", type=Path, required=True)
    parser.add_argument("--motion", type=Path)
    parser.add_argument("--motion-set", type=Path)
    parser.add_argument("--asset-id", default="courier_mira_walk")
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--bvh-addon", type=Path)
    args = parser.parse_args(sys.argv[sys.argv.index("--") + 1:] if "--" in sys.argv else None)
    if args.bvh_addon and not hasattr(bpy.ops.import_anim, "bvh"):
        sys.path.insert(0, str(args.bvh_addon))
        import io_anim_bvh

        io_anim_bvh.register()
    bpy.ops.wm.read_factory_settings(use_empty=True)
    bpy.ops.import_scene.gltf(filepath=str(args.character))
    target = next(o for o in bpy.context.scene.objects if o.type == "ARMATURE")
    meshes = [o for o in bpy.context.scene.objects if o.type == "MESH"]
    target.animation_data_clear()
    roots = [b for b in target.data.bones if b.parent is None]
    if len(roots) != 1 or len(roots[0].children) != 3:
        raise RuntimeError("Expected pelvis with exactly three primary body branches")
    pelvis = roots[0]
    world = lambda b: target.matrix_world @ b.head_local
    upper = [b for b in pelvis.children if world(b).z > world(pelvis).z]
    lower = [b for b in pelvis.children if world(b).z < world(pelvis).z]
    if len(upper) != 1 or len(lower) != 2:
        raise RuntimeError("Cannot distinguish spine and two legs from rest positions")
    mapping = {"Hips": pelvis.name}

    def chain(bone: object, count: int) -> list:
        result = [bone]
        for _ in range(count - 1):
            if len(result[-1].children) != 1:
                raise RuntimeError("Ambiguous anatomical limb chain")
            result.append(result[-1].children[0])
        return result

    # Use body-local X after export, not source generic bone numbering.
    for leg in lower:
        side = "Left" if world(leg).x > world(pelvis).x else "Right"
        for role, bone in zip(("UpLeg", "Leg", "Foot", "Toe"), chain(leg, 4)):
            mapping[side + role] = bone.name
    spine = chain(upper[0], 3)
    for role, bone in zip(("Spine", "Spine1", "Spine2"), spine):
        mapping[role] = bone.name
    fork = spine[-1]
    if len(fork.children) != 3:
        raise RuntimeError("Expected upper spine with neck and two shoulder branches")
    neck = max(fork.children, key=lambda b: world(b).z)
    for role, bone in zip(("Neck", "Head"), chain(neck, 2)):
        mapping[role] = bone.name
    for shoulder in [b for b in fork.children if b != neck]:
        side = "Left" if world(shoulder).x > world(fork).x else "Right"
        for role, bone in zip(("Shoulder", "Arm", "ForeArm", "Hand"), chain(shoulder, 4)):
            mapping[side + role] = bone.name
    if len(set(mapping.values())) != len(mapping):
        raise RuntimeError("Anatomical mapping is not one-to-one")
    if (args.motion is None) == (args.motion_set is None):
        raise ValueError("Supply exactly one of --motion and --motion-set")
    for action in list(bpy.data.actions):
        bpy.data.actions.remove(action)
    target.animation_data_create()
    clips = json.loads(args.motion_set.read_text()) if args.motion_set else [
        {"id": "MoMask_Walk_Forward", "bvh": str(args.motion), "root_motion": "travel"}]
    reports = []
    for clip in clips:
        reports.append(retarget_clip(target, mapping, clip))
    target.animation_data.action = None
    bpy.context.scene.frame_set(1)
    for bone in target.pose.bones:
        bone.matrix_basis = Matrix.Identity(4)
    bpy.context.view_layer.update()
    args.output.mkdir(parents=True, exist_ok=True)
    bpy.ops.object.select_all(action="DESELECT")
    for obj in meshes + [target]:
        obj.select_set(True)
    bpy.ops.export_scene.gltf(filepath=str(args.output / f"SK_{args.asset_id}.glb"),
                              export_format="GLB", use_selection=True,
                              export_animation_mode="NLA_TRACKS")
    bpy.ops.export_scene.fbx(filepath=str(args.output / f"SK_{args.asset_id}.fbx"),
                             use_selection=True, add_leaf_bones=False,
                             axis_forward="-Z", axis_up="Y", bake_anim_use_all_actions=False)
    report = {"mapping": mapping, "clips": reports, "fps": 20,
              "method": "world_rest_rotation_delta",
              "contact_loop_quality": "not_verified", "status": "retarget_candidate"}
    (args.output / "retarget_report.json").write_text(json.dumps(report, indent=2))
    bpy.ops.wm.save_as_mainfile(filepath=str(args.output / f"{args.asset_id}.blend"))
    print(json.dumps(report))


def retarget_clip(target: object, mapping: dict, clip: dict) -> dict:
    import bpy
    from mathutils import Matrix

    target.animation_data.action = None
    for track in target.animation_data.nla_tracks:
        track.mute = True
    for bone in target.pose.bones:
        bone.matrix_basis = Matrix.Identity(4)
    bpy.context.view_layer.update()
    bpy.ops.import_anim.bvh(filepath=clip["bvh"], axis_forward="-Z", axis_up="Y",
                            use_fps_scale=False, update_scene_fps=True)
    source = bpy.context.object
    missing = set(mapping) - set(source.data.bones.keys())
    if missing:
        raise RuntimeError(f"Source BVH roles missing: {missing}")
    scene = bpy.context.scene
    scene.render.fps = 20
    start, end = (int(x) for x in source.animation_data.action.frame_range)
    scene.frame_start, scene.frame_end = start, end
    target_rest = {role: target.matrix_world @ target.data.bones[name].matrix_local
                   for role, name in mapping.items()}
    source_rest = {role: source.matrix_world @ source.data.bones[role].matrix_local
                   for role in mapping}
    order = sorted(mapping, key=lambda role: len(target.data.bones[mapping[role]].parent_recursive))
    world = lambda bone: target.matrix_world @ bone.head_local
    source_height = max(b.head_local.z for b in source.data.bones) - min(
        b.head_local.z for b in source.data.bones)
    target_height = max(world(b).z for b in target.data.bones) - min(
        world(b).z for b in target.data.bones)
    if source_height <= 0 or target_height <= 0:
        raise RuntimeError("Degenerate retarget skeleton height")
    ratio = target_height / source_height
    scene.frame_set(start)
    initial_root = (source.matrix_world @ source.pose.bones["Hips"].matrix).translation.copy()
    for frame in range(start, end + 1):
        scene.frame_set(frame)
        for role in order:
            name = mapping[role]
            src_pose = source.matrix_world @ source.pose.bones[role].matrix
            delta = src_pose.to_quaternion() @ source_rest[role].to_quaternion().inverted()
            desired = (delta @ target_rest[role].to_quaternion()).to_matrix().to_4x4()
            desired.translation = target_rest[role].translation
            if role == "Hips":
                travel = (src_pose.translation - initial_root) * ratio
                if clip["root_motion"] == "in_place":
                    travel.x = travel.y = 0
                desired.translation += travel
            else:
                parent = target.data.bones[name].parent
                parent_rest = target.matrix_world @ parent.matrix_local
                parent_pose = target.matrix_world @ target.pose.bones[parent.name].matrix
                desired.translation = parent_pose @ parent_rest.inverted() @ desired.translation
            target.pose.bones[name].matrix = target.matrix_world.inverted() @ desired
            bpy.context.view_layer.update()
            bone = target.pose.bones[name]
            bone.rotation_mode = "QUATERNION"
            bone.scale = (1, 1, 1)
            if role != "Hips":
                bone.location = (0, 0, 0)
            bone.keyframe_insert("rotation_quaternion", frame=frame)
            bone.keyframe_insert("location", frame=frame)
    action = target.animation_data.action
    action.name = clip["id"]
    track = target.animation_data.nla_tracks.new()
    track.name = clip["id"]
    strip = track.strips.new(clip["id"], start, action)
    strip.action_slot = target.animation_data.action_slot
    target.animation_data.action = None
    source_action = source.animation_data.action
    bpy.data.objects.remove(source, do_unlink=True)
    bpy.data.actions.remove(source_action)
    for item in target.animation_data.nla_tracks:
        item.mute = False
    return {"id": clip["id"], "frames": end - start + 1, "fps": 20,
            "scale_ratio": ratio, "root_motion": clip["root_motion"],
            "loop": clip.get("loop", False), "loop_contact_quality": "not_verified"}


if __name__ == "__main__":
    main()