"""Small Blender regressions; no inference, model downloads or source mutations."""
from __future__ import annotations

import argparse
import json
import math
from pathlib import Path
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]


def test_uv_transfer_in_blender() -> None:
    import pytest

    config = ROOT / "automatic.local.json"
    if not config.exists():
        pytest.skip("Local Blender configuration not present")
    blender = Path(json.loads(config.read_text(encoding="utf-8"))["blender"])
    if not blender.is_file():
        pytest.skip("Configured Blender not installed")
    result = subprocess.run(
        [str(blender), "--background", "--factory-startup", "--python-exit-code", "1",
         "--python", str(Path(__file__).resolve()), "--"],
        capture_output=True, text=True, timeout=120,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "PASS: UV regressions" in result.stdout


def snapshot(obj: object) -> tuple:
    mesh = obj.data
    return (
        [(tuple(v.co), tuple((g.group, g.weight) for g in v.groups)) for v in mesh.vertices],
        [tuple(p.vertices) for p in mesh.polygons],
        [loop.vertex_index for loop in mesh.loops],
        [(a.name, [tuple(c.color) for c in a.data]) for a in mesh.color_attributes],
        [(g.name, g.index) for g in obj.vertex_groups],
        [(m.name, m.type, getattr(m, "object", None)) for m in obj.modifiers],
    )


def check_mesh(obj: object, expected_method: str) -> dict:
    import bpy

    from texture_bake import unwrap_welded_copy

    before = snapshot(obj)
    objects = set(bpy.data.objects)
    meshes = set(bpy.data.meshes)
    obj.data.uv_layers.new(name="UVRegression")
    method = unwrap_welded_copy(obj)
    assert method == expected_method, method
    assert snapshot(obj) == before, "Geometry, weights, colours or binding changed"
    assert set(bpy.data.objects) == objects, "Temporary object leaked"
    assert set(bpy.data.meshes) == meshes, "Temporary mesh leaked"
    assert bpy.context.view_layer.objects.active == obj
    assert obj.select_get() and obj.mode == "OBJECT"
    uv = [tuple(loop.uv) for loop in obj.data.uv_layers.active.data]
    assert len(uv) == len(obj.data.loops)
    assert all(math.isfinite(v) and -0.001 <= v <= 1.001 for pair in uv for v in pair)
    assert any(v > 0 for pair in uv for v in pair)
    if method == "original_copy_fallback":
        # Independent un-welded projection proves exact corner-to-UV correspondence.
        expected_mesh = obj.data.copy()
        expected = bpy.data.objects.new("ExpectedOriginalProjection", expected_mesh)
        bpy.context.collection.objects.link(expected)
        try:
            bpy.ops.object.select_all(action="DESELECT")
            expected.select_set(True)
            bpy.context.view_layer.objects.active = expected
            bpy.ops.object.mode_set(mode="EDIT")
            bpy.ops.mesh.select_all(action="SELECT")
            bpy.ops.uv.smart_project(angle_limit=math.radians(66), island_margin=0.001,
                                     scale_to_bounds=True)
            bpy.ops.object.mode_set(mode="OBJECT")
            assert uv == [tuple(loop.uv) for loop in expected_mesh.uv_layers.active.data]
        finally:
            bpy.data.objects.remove(expected, do_unlink=True)
            bpy.data.meshes.remove(expected_mesh)
    return {"mesh": obj.name, "method": method, "vertices": len(obj.data.vertices),
            "faces": len(obj.data.polygons), "uv_loops": len(uv),
            "geometry_weights_colours_binding_unchanged": True,
            "finite_bounded_uvs": True, "temporary_data_cleaned": True}


def blender_checks() -> None:
    import bpy

    parser = argparse.ArgumentParser()
    parser.add_argument("--input", type=Path)
    parser.add_argument("--report", type=Path)
    args = parser.parse_args(sys.argv[sys.argv.index("--") + 1:])
    sys.path.insert(0, str(ROOT / "src"))
    bpy.ops.wm.read_factory_settings(use_empty=True)
    reports = []
    for degenerate in (False, True):
        mesh = bpy.data.meshes.new("SplitTriangles")
        vertices = [(0, 0, 0), (1, 0, 0), (0, 1, 0),
                    (1, 0, 0), (1, 1, 0), (0, 1, 0)]
        faces = [(0, 1, 2), (3, 4, 5)]
        if degenerate:
            vertices += [(2, 0, 0), (2.0000001, 0, 0), (2, 1, 0)]
            faces.append((6, 7, 8))
        mesh.from_pydata(vertices, [], faces)
        obj = bpy.data.objects.new("Collapse" if degenerate else "Healthy", mesh)
        bpy.context.collection.objects.link(obj)
        group = obj.vertex_groups.new(name="Bone")
        group.add(list(range(len(vertices))), 1.0, "REPLACE")
        colors = mesh.color_attributes.new(name="Color", type="FLOAT_COLOR", domain="CORNER")
        for index, color in enumerate(colors.data):
            color.color = (index / len(colors.data), 0.3, 0.8, 1)
        method = "original_copy_fallback" if degenerate else "welded_copy"
        reports.append(check_mesh(obj, method))
        if not degenerate:
            uv = mesh.uv_layers.active.data
            assert tuple(uv[1].uv) == tuple(uv[3].uv)
            assert tuple(uv[2].uv) == tuple(uv[5].uv)
    if args.input:
        bpy.ops.wm.read_factory_settings(use_empty=True)
        bpy.ops.import_scene.gltf(filepath=str(args.input.resolve()))
        skinned = [o for o in bpy.context.scene.objects if o.type == "MESH" and o.vertex_groups]
        assert skinned, "Retained input contains no skinned meshes"
        for obj in skinned:
            reports.append(check_mesh(obj, "original_copy_fallback"))
    if args.report:
        args.report.write_text(json.dumps(reports, indent=2), encoding="utf-8")
    print("PASS: UV regressions", json.dumps(reports))


if __name__ == "__main__":
    blender_checks()