"""Executed Blender regression: UV-only welding must preserve every skinning input."""
from __future__ import annotations

import argparse
from pathlib import Path
import sys


def main() -> None:
    import bpy

    parser = argparse.ArgumentParser()
    parser.add_argument("--input", type=Path, required=True)
    args = parser.parse_args(sys.argv[sys.argv.index("--") + 1:])
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from texture_bake import unwrap_welded_copy

    bpy.ops.wm.read_factory_settings(use_empty=True)
    bpy.ops.import_scene.gltf(filepath=str(args.input.resolve()))
    for obj in [o for o in bpy.context.scene.objects if o.type == "MESH" and o.vertex_groups]:
        vertices = [(tuple(v.co), tuple((g.group, g.weight) for g in v.groups))
                    for v in obj.data.vertices]
        faces = [tuple(p.vertices) for p in obj.data.polygons]
        colours = [tuple(c.color) for c in obj.data.color_attributes[0].data]
        obj.data.uv_layers.new(name="UVTest")
        unwrap_welded_copy(obj)
        assert vertices == [(tuple(v.co), tuple((g.group, g.weight) for g in v.groups))
                            for v in obj.data.vertices]
        assert faces == [tuple(p.vertices) for p in obj.data.polygons]
        assert colours == [tuple(c.color) for c in obj.data.color_attributes[0].data]
        assert len(obj.data.uv_layers.active.data) == len(obj.data.loops)
        assert any(loop.uv.length > 0 for loop in obj.data.uv_layers.active.data)
    print("PASS: coordinates, indices, skin weights and colours unchanged; UV transfer complete")


if __name__ == "__main__":
    main()