"""Trusted mesh preparation and structural rig QA; not anatomical/Unity validation."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


def main() -> None:
    import bpy

    parser = argparse.ArgumentParser()
    parser.add_argument("--mode", choices=("prepare", "export"), required=True)
    parser.add_argument("--input", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--report", type=Path, required=True)
    parser.add_argument("--height-m", type=float, required=True)
    args = parser.parse_args(sys.argv[sys.argv.index("--") + 1:])
    bpy.ops.wm.read_factory_settings(use_empty=True)
    bpy.ops.import_scene.gltf(filepath=str(args.input))
    meshes = [obj for obj in bpy.context.scene.objects if obj.type == "MESH"]
    rigs = [obj for obj in bpy.context.scene.objects if obj.type == "ARMATURE"]
    if not meshes:
        raise RuntimeError("No mesh imported")
    if args.mode == "prepare":
        if rigs:
            raise RuntimeError("Preparation must precede rigging")
        from mathutils import Vector

        corners = [obj.matrix_world @ Vector(corner) for obj in meshes for corner in obj.bound_box]
        minimum = min(point.z for point in corners)
        height = max(point.z for point in corners) - minimum
        if height <= 0:
            raise RuntimeError("Degenerate mesh bounds")
        factor = args.height_m / height
        # Transform world geometry once; Blender's importer already handles glTF Y-up.
        for obj in meshes:
            for vertex in obj.data.vertices:
                world = obj.matrix_world @ vertex.co
                world.z -= minimum
                vertex.co = world * factor
            from mathutils import Matrix

            obj.matrix_world = Matrix.Identity(4)
        total = sum(sum(len(face.vertices) - 2 for face in obj.data.polygons) for obj in meshes)
        for obj in meshes:
            bpy.context.view_layer.objects.active = obj
            obj.select_set(True)
            if total > 30000:
                modifier = obj.modifiers.new("NPC_budget", "DECIMATE")
                modifier.ratio = 29000 / total
                bpy.ops.object.modifier_apply(modifier=modifier.name)
            obj.select_set(False)
    triangles = sum(sum(len(face.vertices) - 2 for face in obj.data.polygons) for obj in meshes)
    materials = {slot.material.name for obj in meshes for slot in obj.material_slots if slot.material}
    issues = []
    if triangles > 30000:
        issues.append("triangle_budget")
    if len(materials) > 2:
        issues.append("material_budget")
    if any(not obj.data.uv_layers for obj in meshes):
        issues.append("missing_uvs")
    if args.mode == "export":
        if len(rigs) != 1:
            issues.append("expected_one_armature")
        else:
            rig = rigs[0]
            if len(rig.data.bones) < 15:
                issues.append("insufficient_bones")
            for obj in meshes:
                bindings = [m for m in obj.modifiers if m.type == "ARMATURE" and m.object == rig]
                if not bindings:
                    issues.append(f"{obj.name}:missing_binding")
                bone_groups = {g.index for g in obj.vertex_groups if g.name in rig.data.bones}
                for vertex in obj.data.vertices:
                    weights = [g.weight for g in vertex.groups if g.group in bone_groups
                               and g.weight > 0]
                    if not weights or len(weights) > 4 or abs(sum(weights) - 1) > 0.01:
                        issues.append(f"{obj.name}:invalid_vertex_weights")
                        break
    report = {
        "blender": bpy.app.version_string, "triangles": triangles,
        "materials": sorted(materials), "armatures": len(rigs), "issues": issues,
        "structural_pass": not issues,
        "anatomical_deformation": "not_verified", "unity": "not_run",
        "movement_clips": "not_supplied", "orientation": "upstream_glTF_import_not_reviewed",
    }
    args.report.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    if issues:
        raise RuntimeError(f"QA rejected asset: {issues}")
    if args.mode == "prepare":
        bpy.ops.export_scene.gltf(filepath=str(args.output), export_format="GLB")
    else:
        bpy.ops.export_scene.fbx(
            filepath=str(args.output), object_types={"MESH", "ARMATURE"},
            add_leaf_bones=False, axis_forward="-Z", axis_up="Y", bake_anim=False,
        )


if __name__ == "__main__":
    main()