"""Inspect an actual learned rig and render it without changing the source."""
from __future__ import annotations

import argparse
import json
import math
import sys
from pathlib import Path


def main() -> None:
    import bpy
    from mathutils import Matrix, Vector

    parser = argparse.ArgumentParser()
    parser.add_argument("--input", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--spec", type=Path)
    parser.add_argument("--texture-resolution", type=int, default=1024)
    parser.add_argument("--source-up", choices=("triposr-z", "gltf-y"), default="triposr-z")
    args = parser.parse_args(sys.argv[sys.argv.index("--") + 1:])
    spec = json.loads(args.spec.read_text()) if args.spec else {"id": "courier_mira", "height_m": 1.7}
    bpy.ops.wm.read_factory_settings(use_empty=True)
    bpy.ops.import_scene.gltf(filepath=str(args.input))
    # SkinTokens exports a non-rendered helper sphere; it is not character geometry.
    for obj in list(bpy.context.scene.objects):
        if obj.type == "MESH" and obj.name == "Icosphere" and not obj.vertex_groups:
            bpy.data.objects.remove(obj, do_unlink=True)
    # TripoSR geometry is Z-up but its generic GLB writer leaves glTF Y-up semantics.
    # Import therefore presents the character along -Y; correct the entire rig once.
    roots = [o for o in bpy.context.scene.objects if o.parent is None]
    rotation = (Matrix.Rotation(-math.pi / 2, 4, "X")
                if args.source_up == "triposr-z" else Matrix.Identity(4))
    for obj in roots:
        obj.matrix_world = rotation @ obj.matrix_world
    bpy.context.view_layer.update()
    meshes = [o for o in bpy.context.scene.objects if o.type == "MESH"]
    rigs = [o for o in bpy.context.scene.objects if o.type == "ARMATURE"]
    if not meshes or len(rigs) != 1:
        raise RuntimeError("Expected rendered meshes and exactly one learned armature")
    rig = rigs[0]
    issues = []
    for obj in meshes:
        bone_groups = {g.index for g in obj.vertex_groups if g.name in rig.data.bones}
        if not any(m.type == "ARMATURE" and m.object == rig for m in obj.modifiers):
            issues.append(f"{obj.name}:missing_armature_binding")
        for vertex in obj.data.vertices:
            weights = [g.weight for g in vertex.groups if g.group in bone_groups and g.weight > 0]
            if not weights or len(weights) > 4 or abs(sum(weights) - 1) > 0.01:
                issues.append(f"{obj.name}:invalid_weights")
                break
    if sum(len(f.vertices) - 2 for o in meshes for f in o.data.polygons) > 30000:
        issues.append("triangle_budget")
    if issues:
        raise RuntimeError(f"Structural rig QA failed: {issues}")
    report = {"bones": [{"name": b.name, "parent": b.parent.name if b.parent else None,
                          "head": list(b.head_local), "tail": list(b.tail_local)}
                         for r in rigs for b in r.data.bones],
              "meshes": [{"name": o.name, "vertices": len(o.data.vertices),
                           "triangles": sum(len(f.vertices) - 2 for f in o.data.polygons),
                           "colors": list(o.data.color_attributes.keys()),
                           "materials": [s.material.name for s in o.material_slots if s.material],
                           "unweighted": sum(not v.groups for v in o.data.vertices)}
                          for o in meshes]}
    args.output.mkdir(parents=True, exist_ok=True)
    (args.output / "rig_inspection.json").write_text(json.dumps(report, indent=2))
    corners = [o.matrix_world @ Vector(c) for o in meshes for c in o.bound_box]
    low_z = min(p.z for p in corners)
    height = max(p.z for p in corners) - low_z
    if height <= 0:
        raise RuntimeError("Degenerate mesh height")
    transform = Matrix.Scale(spec["height_m"] / height, 4) @ Matrix.Translation((0, 0, -low_z))
    for obj in roots:
        obj.matrix_world = transform @ obj.matrix_world
    bpy.context.view_layer.update()
    corners = [o.matrix_world @ Vector(c) for o in meshes for c in o.bound_box]
    low = Vector([min(p[i] for p in corners) for i in range(3)])
    high = Vector([max(p[i] for p in corners) for i in range(3)])
    center = (low + high) / 2
    span = max(high - low)
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from texture_bake import bake_albedo

    report["textures"] = bake_albedo(meshes, args.output, spec["id"], args.texture_resolution)
    for o in meshes:
        for polygon in o.data.polygons:
            polygon.use_smooth = True
        for slot in o.material_slots:
            mat = slot.material
            if not mat:
                continue
            mat.use_nodes = True
            bsdf = mat.node_tree.nodes.get("Principled BSDF")
            if o.data.color_attributes and bsdf:
                attribute = mat.node_tree.nodes.new("ShaderNodeVertexColor")
                attribute.layer_name = o.data.color_attributes[0].name
                mat.node_tree.links.new(attribute.outputs["Color"], bsdf.inputs["Base Color"])
    bpy.ops.object.camera_add(location=center + Vector((0, span * 2, span * 0.1)))
    camera = bpy.context.object
    camera.rotation_euler = (center - camera.location).to_track_quat("-Z", "Y").to_euler()
    camera.data.type = "ORTHO"
    camera.data.ortho_scale = span * 1.25
    scene = bpy.context.scene
    scene.camera = camera
    scene.render.engine = "CYCLES"
    scene.cycles.samples = 24
    scene.render.resolution_x = 768
    scene.render.resolution_y = 768
    scene.render.resolution_percentage = 100
    scene.world = bpy.data.worlds.new("PreviewWorld")
    scene.world.color = (0.22, 0.22, 0.22)
    for offset, power, size in [((2, 3, 3), 500, 4), ((-2, 1, 1), 300, 3)]:
        bpy.ops.object.light_add(type="AREA", location=center + Vector(offset) * span)
        light = bpy.context.object
        light.data.energy = power
        light.data.shape = "DISK"
        light.data.size = size
        light.rotation_euler = (center - light.location).to_track_quat("-Z", "Y").to_euler()
    scene.render.filepath = str(args.output / "preview_front.png")
    bpy.ops.render.render(write_still=True)
    camera.location = center + Vector((span * 1.5, span * 1.5, span * 0.1))
    camera.rotation_euler = (center - camera.location).to_track_quat("-Z", "Y").to_euler()
    scene.render.filepath = str(args.output / "preview_three_quarter.png")
    bpy.ops.render.render(write_still=True)
    if len(rigs) == 1:
        rig = rigs[0]
        # Select a non-root weighted joint, never a pilot-specific bone name.
        influence = {b.name: 0 for b in rig.data.bones if b.parent}
        for obj in meshes:
            names = {g.index: g.name for g in obj.vertex_groups}
            for vertex in obj.data.vertices:
                for group in vertex.groups:
                    name = names[group.group]
                    if name in influence and group.weight > 0.1:
                        influence[name] += 1
        candidates = [name for name, count in influence.items() if count > 0]
        if not candidates:
            raise RuntimeError("No weighted non-root joint available for deformation check")
        bone = rig.pose.bones[max(candidates, key=lambda name: (influence[name], name))]
        if bone:
            scene.frame_set(1)
            depsgraph = bpy.context.evaluated_depsgraph_get()
            baseline = [v.co.copy() for v in meshes[0].evaluated_get(depsgraph).data.vertices]
            bone.rotation_mode = "XYZ"
            for frame, angle in ((1, 0.0), (16, 0.35), (31, 0.0)):
                bone.rotation_euler.z = angle
                bone.keyframe_insert(data_path="rotation_euler", frame=frame)
            rig.animation_data.action.name = "Rig_Deformation_Smoke"
            scene.frame_end = 31
            scene.frame_set(16)
            evaluated = meshes[0].evaluated_get(bpy.context.evaluated_depsgraph_get())
            displacements = [(v.co - p).length for v, p in zip(evaluated.data.vertices, baseline)]
            report["deformation_smoke"] = {
                "bone": bone.name, "angle_radians": 0.35,
                "moved_vertices": sum(d > 0.00001 for d in displacements),
                "max_local_displacement": max(displacements),
                "authored_test_not_generated_motion": True,
            }
            if report["deformation_smoke"]["moved_vertices"] == 0:
                raise RuntimeError("Skinning failed: rotating weighted joint moves no vertices")
            report["source_up_contract"] = args.source_up
            report["height_m"] = spec["height_m"]
            scene.render.filepath = str(args.output / "preview_bend.png")
            bpy.ops.render.render(write_still=True)
            scene.frame_set(1)
        bpy.ops.object.select_all(action="DESELECT")
        for obj in meshes + rigs:
            obj.select_set(True)
        bpy.ops.export_scene.gltf(filepath=str(args.output / f"SK_{spec['id']}.glb"),
                                  export_format="GLB", use_selection=True)
        bpy.ops.export_scene.fbx(filepath=str(args.output / f"SK_{spec['id']}.fbx"),
                                 use_selection=True, object_types={"MESH", "ARMATURE"},
                                 add_leaf_bones=False, axis_forward="-Z", axis_up="Y")
    bpy.ops.wm.save_as_mainfile(filepath=str(args.output / f"{spec['id']}.blend"))
    (args.output / "rig_inspection.json").write_text(json.dumps(report, indent=2))
    print(json.dumps(report))


if __name__ == "__main__":
    main()