"""Bake reconstructed vertex colour into modest UV albedo maps; no invented PBR detail."""
from __future__ import annotations

import json
import math
from pathlib import Path


def bake_albedo(
    meshes: list, output: Path, asset_id: str, resolution: int, *, welded_uv: bool = True,
) -> dict:
    import bpy

    if type(resolution) is not int or resolution not in (512, 1024, 2048):
        raise ValueError("Texture resolution must be 512, 1024 or 2048")
    if not 1 <= len(meshes) <= 2:
        raise RuntimeError("Texture material budget requires one or two meshes")
    scene = bpy.context.scene
    scene.render.engine = "CYCLES"
    scene.cycles.device = "CPU"
    scene.cycles.samples = 1
    records = []
    for index, obj in enumerate(meshes):
        if not obj.data.color_attributes:
            raise RuntimeError(f"{obj.name}: reconstructed vertex colours missing")
        bpy.ops.object.select_all(action="DESELECT")
        obj.select_set(True)
        bpy.context.view_layer.objects.active = obj
        while obj.data.uv_layers:
            obj.data.uv_layers.remove(obj.data.uv_layers[0])
        obj.data.uv_layers.new(name="UVMap")
        uv_method = "original_disconnected"
        if welded_uv:
            uv_method = unwrap_welded_copy(obj)
        else:
            bpy.ops.object.mode_set(mode="EDIT")
            bpy.ops.mesh.select_all(action="SELECT")
            bpy.ops.uv.smart_project(angle_limit=math.radians(66), island_margin=0.001,
                         scale_to_bounds=True)
            bpy.ops.object.mode_set(mode="OBJECT")
        uv = obj.data.uv_layers.active
        if welded_uv:
            smooth_split_normals(obj)
        if not uv.data or any(not math.isfinite(v) or not -0.001 <= v <= 1.001
                              for loop in uv.data for v in loop.uv):
            raise RuntimeError(f"{obj.name}: invalid UV coordinates")
        uv_area = sum(abs(
            (uv.data[p.loop_start + 1].uv - uv.data[p.loop_start].uv).cross(
                uv.data[p.loop_start + 2].uv - uv.data[p.loop_start].uv)) * 0.5
                      for p in obj.data.polygons if p.loop_total == 3)
        name = f"T_{asset_id}_{index}_BaseColor"
        image = bpy.data.images.new(name, width=resolution, height=resolution, alpha=False)
        image.colorspace_settings.name = "sRGB"
        material = bpy.data.materials.new(f"M_{asset_id}_{index}")
        material.use_nodes = True
        nodes = material.node_tree.nodes
        nodes.clear()
        links = material.node_tree.links
        attribute = nodes.new("ShaderNodeVertexColor")
        attribute.layer_name = obj.data.color_attributes[0].name
        emission = nodes.new("ShaderNodeEmission")
        sink = nodes.new("ShaderNodeOutputMaterial")
        links.new(attribute.outputs["Color"], emission.inputs["Color"])
        links.new(emission.outputs[0], sink.inputs["Surface"])
        texture = nodes.new("ShaderNodeTexImage")
        texture.image = image
        nodes.active = texture
        obj.data.materials.clear()
        obj.data.materials.append(material)
        for polygon in obj.data.polygons:
            polygon.material_index = 0
        scene.render.bake.margin = 8
        bpy.ops.object.bake(type="EMIT", use_clear=True)
        pixels = list(image.pixels)
        samples = [max(pixels[i:i + 3]) for i in range(0, len(pixels), 64)]
        if not samples or not all(math.isfinite(v) for v in samples) or max(samples) <= 0:
            raise RuntimeError(f"{obj.name}: empty/nonfinite albedo bake")
        coverage = sum(v > 0.01 for v in samples) / len(samples)
        if coverage < 0.1:
            raise RuntimeError(f"{obj.name}: albedo coverage too low ({coverage:.4f})")
        image.filepath_raw = str(output / f"{name}.png")
        image.file_format = "PNG"
        image.save()
        image.pack()
        nodes.remove(emission)
        nodes.remove(attribute)
        bsdf = nodes.new("ShaderNodeBsdfPrincipled")
        bsdf.inputs["Roughness"].default_value = 0.8
        bsdf.inputs["Metallic"].default_value = 0.0
        links.new(texture.outputs["Color"], bsdf.inputs["Base Color"])
        links.new(bsdf.outputs[0], sink.inputs["Surface"])
        # glTF multiplies COLOR_0 by the base texture: don't apply the colour twice.
        while obj.data.color_attributes:
            obj.data.color_attributes.remove(obj.data.color_attributes[0])
        records.append({"mesh": obj.name, "image": f"{name}.png",
                        "resolution": resolution, "uv_loops": len(uv.data),
                        "uv_method": uv_method,
                        "uv_triangle_area": uv_area,
                        "nonblack_sample_fraction": coverage,
                        "sample_range": [min(samples), max(samples)]})
    report = {"structural_pass": True, "method": "vertex_colour_to_uv_emission_bake",
              "maps": records, "roughness": 0.8, "metallic": 0.0,
              "normal_roughness_detail": "not_generated"}
    (output / "texture_report.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
    return report


def unwrap_welded_copy(obj: object) -> str:
    """Connect split glTF vertices for UV layout only; preserve rig geometry and weights."""
    import bmesh
    import bpy

    original = obj.data
    mesh = original.copy()
    temporary = bpy.data.objects.new("UVLayoutOnly", mesh)
    bpy.context.collection.objects.link(temporary)
    try:
        bm = bmesh.new()
        try:
            bm.from_mesh(mesh)
            source = bm.loops.layers.int.new("source_loop")
            index = 0
            for face in bm.faces:
                for loop in face.loops:
                    loop[source] = index
                    index += 1
            bmesh.ops.remove_doubles(bm, verts=list(bm.verts), dist=0.000001)
            bm.to_mesh(mesh)
        finally:
            bm.free()
        fallback = (len(mesh.loops) != len(original.loops)
                    or len(mesh.polygons) != len(original.polygons))
        if fallback:
            # Welding can collapse near-degenerate triangles. Never guess their UV
            # correspondence or change the skinned source: unwrap a fresh copy.
            welded_mesh = mesh
            mesh = original.copy()
            temporary.data = mesh
            bpy.data.meshes.remove(welded_mesh)
        bpy.ops.object.select_all(action="DESELECT")
        temporary.select_set(True)
        bpy.context.view_layer.objects.active = temporary
        bpy.ops.object.mode_set(mode="EDIT")
        bpy.ops.mesh.select_all(action="SELECT")
        # Disconnected triangles need the existing tighter margin to retain coverage.
        bpy.ops.uv.smart_project(angle_limit=math.radians(66),
                    island_margin=0.001 if fallback else 0.004,
                                scale_to_bounds=True)
        bpy.ops.object.mode_set(mode="OBJECT")
        if fallback:
            if (len(mesh.vertices) != len(original.vertices)
                    or [tuple(p.vertices) for p in mesh.polygons]
                    != [tuple(p.vertices) for p in original.polygons]
                    or [loop.vertex_index for loop in mesh.loops]
                    != [loop.vertex_index for loop in original.loops]):
                raise RuntimeError("UV fallback changed original face/corner order")
            destination = original.uv_layers.active
            for index, loop in enumerate(mesh.uv_layers.active.data):
                destination.data[index].uv = loop.uv
            return "original_copy_fallback"
        bm = bmesh.new()
        try:
            bm.from_mesh(mesh)
            source = bm.loops.layers.int["source_loop"]
            uv = bm.loops.layers.uv.active
            destination = original.uv_layers.active
            seen = set()
            for face in bm.faces:
                for loop in face.loops:
                    index = loop[source]
                    if index in seen or not 0 <= index < len(destination.data):
                        raise RuntimeError("UV transfer lost original corner identity")
                    seen.add(index)
                    destination.data[index].uv = loop[uv].uv
            if len(seen) != len(original.loops):
                raise RuntimeError("Incomplete UV transfer")
        finally:
            bm.free()
        return "welded_copy"
    finally:
        if temporary.mode != "OBJECT":
            bpy.ops.object.mode_set(mode="OBJECT")
        bpy.data.objects.remove(temporary, do_unlink=True)
        bpy.data.meshes.remove(mesh)
        bpy.ops.object.select_all(action="DESELECT")
        obj.select_set(True)
        bpy.context.view_layer.objects.active = obj


def smooth_split_normals(obj: object) -> None:
    """Average normals at identical positions without merging skinning vertices."""
    from mathutils import Vector

    sums = {}
    keys = [tuple(round(c, 6) for c in vertex.co) for vertex in obj.data.vertices]
    for polygon in obj.data.polygons:
        normal = polygon.normal * polygon.area
        for index in polygon.vertices:
            key = keys[index]
            sums[key] = sums.get(key, Vector((0, 0, 0))) + normal
        polygon.use_smooth = True
    normals = [sums[key].normalized() for key in keys]
    obj.data.normals_split_custom_set_from_vertices(normals)