"""Controlled Blender comparison: same rig, camera and lights; no model inference."""
from __future__ import annotations

import argparse
import json
import math
from pathlib import Path
import sys


def main() -> None:
    import bpy
    from mathutils import Matrix, Vector

    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from texture_bake import bake_albedo

    parser = argparse.ArgumentParser()
    parser.add_argument("--input", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(sys.argv[sys.argv.index("--") + 1:])
    args.input = args.input.resolve()
    args.output = args.output.resolve()
    args.output.mkdir(parents=True, exist_ok=True)
    reports = []
    for variant in ("vertex_control", "baseline_bake", "welded_uv_bake"):
        directory = args.output / variant
        directory.mkdir(exist_ok=True)
        bpy.ops.wm.read_factory_settings(use_empty=True)
        bpy.ops.import_scene.gltf(filepath=str(args.input))
        for obj in list(bpy.context.scene.objects):
            if obj.type == "MESH" and obj.name == "Icosphere" and not obj.vertex_groups:
                bpy.data.objects.remove(obj, do_unlink=True)
        for obj in [o for o in bpy.context.scene.objects if o.parent is None]:
            obj.matrix_world = Matrix.Rotation(-math.pi / 2, 4, "X") @ obj.matrix_world
        bpy.context.view_layer.update()
        meshes = [o for o in bpy.context.scene.objects if o.type == "MESH"]
        report = {"variant": variant, "meshes": []}
        for obj in meshes:
            unique = len({tuple(round(c, 6) for c in v.co) for v in obj.data.vertices})
            report["meshes"].append({"vertices": len(obj.data.vertices),
                                      "unique_positions_6dp": unique,
                                      "triangles": len(obj.data.polygons)})
        corners = [o.matrix_world @ Vector(c) for o in meshes for c in o.bound_box]
        low = Vector([min(p[i] for p in corners) for i in range(3)])
        high = Vector([max(p[i] for p in corners) for i in range(3)])
        center = (low + high) / 2
        span = max(high - low)
        if variant != "vertex_control":
            report["bake"] = bake_albedo(meshes, directory, "mira", 1024,
                                         welded_uv=variant == "welded_uv_bake")
        else:
            for obj in meshes:
                mat = bpy.data.materials.new("VertexControl")
                mat.use_nodes = True
                bsdf = mat.node_tree.nodes.get("Principled BSDF")
                bsdf.inputs["Roughness"].default_value = 0.8
                attribute = mat.node_tree.nodes.new("ShaderNodeVertexColor")
                attribute.layer_name = obj.data.color_attributes[0].name
                mat.node_tree.links.new(attribute.outputs["Color"], bsdf.inputs["Base Color"])
                obj.data.materials.clear()
                obj.data.materials.append(mat)
        for obj in meshes:
            for polygon in obj.data.polygons:
                polygon.use_smooth = True
        scene = bpy.context.scene
        scene.render.engine = "BLENDER_EEVEE"
        scene.render.resolution_x = scene.render.resolution_y = 768
        scene.render.resolution_percentage = 100
        scene.world = bpy.data.worlds.new("ComparisonWorld")
        scene.world.color = (0.22, 0.22, 0.22)
        for offset, power in [((2, 3, 3), 500), ((-2, 1, 1), 300)]:
            bpy.ops.object.light_add(type="AREA", location=center + Vector(offset) * span)
            light = bpy.context.object
            light.data.energy = power
            light.data.shape = "DISK"
            light.data.size = 4
            light.rotation_euler = (center - light.location).to_track_quat("-Z", "Y").to_euler()
        bpy.ops.object.camera_add()
        camera = bpy.context.object
        camera.data.type = "ORTHO"
        scene.camera = camera
        for view, target, scale, offset in [
            ("front", center, span * 1.25, Vector((0, 2, 0.1))),
            ("quarter", center, span * 1.25, Vector((1.5, 1.5, 0.1))),
            ("face", center + Vector((0, 0, span * 0.35)), span * 0.32,
             Vector((0.2, 2, 0.1))),
        ]:
            camera.location = target + offset * span
            camera.rotation_euler = (target - camera.location).to_track_quat("-Z", "Y").to_euler()
            camera.data.ortho_scale = scale
            scene.render.filepath = str(directory / f"{view}.png")
            bpy.ops.render.render(write_still=True)
        if variant == "welded_uv_bake":
            bpy.ops.object.select_all(action="DESELECT")
            for obj in meshes + [o for o in scene.objects if o.type == "ARMATURE"]:
                obj.select_set(True)
            bpy.ops.export_scene.gltf(filepath=str(directory / "SK_mira.glb"),
                                      export_format="GLB", use_selection=True)
        reports.append(report)
    (args.output / "comparison_report.json").write_text(json.dumps(reports, indent=2))
    print(json.dumps(reports))


if __name__ == "__main__":
    main()