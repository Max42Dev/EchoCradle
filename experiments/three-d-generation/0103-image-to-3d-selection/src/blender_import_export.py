"""Blender-side mesh normalisation step for the EchoCradle Model Orchestrator.

This is the concrete "last mile" of the image-to-3D pipeline: whichever
generator (Stable-Fast-3D, TripoSR, Hunyuan3D-2mini, ...) produced a mesh, we
import it here, report its bounding box and triangle count, recentre it so its
base sits on the origin, scale it to a known size in metres, and re-export it as
a Unity-friendly GLB.

Run headless (no GUI, no MCP socket required)::

    & "C:\\Program Files\\Blender Foundation\\Blender 5.2\\blender.exe" `
        --background --factory-startup `
        --python blender_import_export.py -- `
        <input.glb|input.obj> <output_dir> [--target-size 0.3] [--up-axis Z]

Arguments after ``--`` are passed to this script, not to Blender.

Notes / design decisions
------------------------
* ``--factory-startup`` is recommended so a user's startup file/add-ons cannot
  change behaviour. The script itself also wipes the scene on entry.
* Blender is Z-up and Unity is Y-up; the glTF exporter's ``export_yup=True``
  handles the conversion, so we author in Z-up and export with Y-up.
* The generator's scale is arbitrary, so "target size" is defined as the
  *largest horizontal dimension* (X/Y) OR the height, whichever the caller asks
  for. Default: scale so the largest dimension of the whole bounding box equals
  ``--target-size`` metres. This matches the game-ready convention
  (1 Blender unit = 1 m) used by the Blender asset skill.
* We deliberately do NOT decimate here. Polycount decisions belong with the
  generator (e.g. TripoSG ``--faces``); this step only *verifies* and reports.
"""

from __future__ import annotations

import argparse
import os
import sys

import bpy
from mathutils import Vector

# ---------------------------------------------------------------------------
# Argument parsing (everything after the "--" separator)
# ---------------------------------------------------------------------------


def parse_args(argv: list[str]) -> argparse.Namespace:
    """Parse the args Blender passes through after ``--``."""
    if "--" in argv:
        argv = argv[argv.index("--") + 1 :]
    else:  # pragma: no cover - only hit if launched without the separator
        argv = []

    parser = argparse.ArgumentParser(
        prog="blender_import_export.py",
        description="Import a mesh, report it, recentre/scale it, export GLB.",
    )
    parser.add_argument("input", help="Input mesh file (.glb, .gltf, .obj, .fbx, .ply)")
    parser.add_argument(
        "output_dir",
        nargs="?",
        default=".",
        help="Folder to write the normalised GLB into (default: current dir).",
    )
    parser.add_argument(
        "--target-size",
        type=float,
        default=None,
        help="Scale so the largest bounding-box dimension equals this many metres. "
        "Omit to keep the generator's original scale.",
    )
    parser.add_argument(
        "--output-name",
        default=None,
        help="Output file name (default: <input stem>_unity.glb).",
    )
    parser.add_argument(
        "--up-axis",
        choices=("Y", "Z"),
        default="Y",
        help="Up axis to write in the GLB. Y is the Unity/glTF convention (default).",
    )
    parser.add_argument(
        "--no-recenter-xz",
        action="store_true",
        help="Keep the original X/Z offset instead of centring the object at X=Z=0.",
    )
    return parser.parse_args(argv)


# ---------------------------------------------------------------------------
# Scene helpers
# ---------------------------------------------------------------------------


def reset_scene() -> None:
    """Start from an empty scene so imports are deterministic."""
    bpy.ops.wm.read_factory_settings(use_empty=True)


def import_mesh(path: str) -> list[bpy.types.Object]:
    """Import a mesh by extension and return the imported mesh objects."""
    if not os.path.isfile(path):
        raise FileNotFoundError(f"Input mesh not found: {path}")

    ext = os.path.splitext(path)[1].lower()
    if ext in (".glb", ".gltf"):
        bpy.ops.import_scene.gltf(filepath=path)
    elif ext == ".obj":
        # Blender 4.x+ uses wm.obj_import (the old import_scene.obj is gone).
        bpy.ops.wm.obj_import(filepath=path)
    elif ext == ".fbx":
        bpy.ops.import_scene.fbx(filepath=path)
    elif ext == ".ply":
        bpy.ops.wm.ply_import(filepath=path)
    else:
        raise ValueError(f"Unsupported input extension: {ext!r}")

    return [o for o in bpy.context.scene.objects if o.type == "MESH"]


def world_bounds(objects: list[bpy.types.Object]) -> tuple[Vector, Vector]:
    """Return (min_corner, max_corner) of the world-space AABB of `objects`."""
    mins = Vector((float("inf"),) * 3)
    maxs = Vector((float("-inf"),) * 3)
    for obj in objects:
        for corner in obj.bound_box:  # local-space 8 corners
            world = obj.matrix_world @ Vector(corner)
            for i in range(3):
                mins[i] = min(mins[i], world[i])
                maxs[i] = max(maxs[i], world[i])
    return mins, maxs


def triangle_count(objects: list[bpy.types.Object]) -> int:
    """Count triangles after modifiers, the way the exporter will see them."""
    deps = bpy.context.evaluated_depsgraph_get()
    total = 0
    for obj in objects:
        eval_obj = obj.evaluated_get(deps)
        mesh = eval_obj.to_mesh()
        mesh.calc_loop_triangles()
        total += len(mesh.loop_triangles)
        eval_obj.to_mesh_clear()
    return total


def report(label: str, objs: list[bpy.types.Object]) -> dict:
    """Print and return the geometry summary for one state of the scene."""
    mins, maxs = world_bounds(objs)
    dims = maxs - mins
    tris = triangle_count(objs)
    verts = sum(len(o.data.vertices) for o in objs)
    print(f"[{label}] objects={len(objs)} verts={verts} tris={tris}")
    print(
        f"[{label}] bbox min=({mins.x:.4f}, {mins.y:.4f}, {mins.z:.4f}) "
        f"max=({maxs.x:.4f}, {maxs.y:.4f}, {maxs.z:.4f})"
    )
    print(f"[{label}] size=({dims.x:.4f}, {dims.y:.4f}, {dims.z:.4f})")
    return {
        "objects": len(objs),
        "verts": verts,
        "tris": tris,
        "dims": (dims.x, dims.y, dims.z),
        "min": (mins.x, mins.y, mins.z),
        "max": (maxs.x, maxs.y, maxs.z),
    }


# ---------------------------------------------------------------------------
# Transform
# ---------------------------------------------------------------------------


def normalise(
    objects: list[bpy.types.Object],
    target_size: float | None,
    center_xz: bool = True,
) -> None:
    """Centre on X/Z, drop the base to Z=0, then scale to `target_size` if given.

    We bake the transform into a parent Empty rather than editing each mesh, so
    multiple imported objects move together and their relative layout is kept.
    """
    mins, maxs = world_bounds(objects)
    dims = maxs - mins
    center = (mins + maxs) * 0.5

    # --- position: move so X/Z are centred and the base sits on Z = 0 --------
    offset = Vector((0.0, 0.0, 0.0))
    if center_xz:
        offset.x = -center.x
        offset.y = -center.y
    offset.z = -mins.z  # base down to the floor

    for obj in objects:
        obj.location += offset

    if target_size and target_size > 0:
        largest = max(dims.x, dims.y, dims.z)
        if largest > 0:
            factor = target_size / largest
            # Scale about the world origin the objects now sit on.
            for obj in objects:
                obj.location *= factor
                obj.scale *= factor
            print(
                f"[normalise] scaled x{factor:.5f} "
                f"({largest:.4f} m -> {target_size:.4f} m)"
            )

    # Apply transforms so the exported mesh data itself is clean (a scale left
    # on the object is a classic cause of wrong sizes in Unity).
    bpy.ops.object.select_all(action="DESELECT")
    for obj in objects:
        obj.select_set(True)
    if objects:
        bpy.context.view_layer.objects.active = objects[0]
        bpy.ops.object.transform_apply(location=True, rotation=True, scale=True)


# ---------------------------------------------------------------------------
# Export
# ---------------------------------------------------------------------------


def export_glb(
    objects: list[bpy.types.Object],
    output_dir: str,
    output_name: str,
    up_axis: str = "Y",
) -> str:
    """Export exactly `objects` to a single-binary .glb and return its path."""
    os.makedirs(output_dir, exist_ok=True)
    filepath = os.path.join(output_dir, output_name)

    bpy.ops.object.select_all(action="DESELECT")
    for obj in objects:
        obj.select_set(True)
    bpy.context.view_layer.objects.active = objects[0]

    bpy.ops.export_scene.gltf(
        filepath=filepath,
        export_format="GLB",          # single binary file, best for Unity import
        use_selection=True,           # only what we just imported
        export_apply=True,            # apply modifiers
        export_yup=(up_axis == "Y"),  # Unity/glTF want +Y up
        export_texcoords=True,
        export_normals=True,
        export_materials="EXPORT",
        export_image_format="AUTO",
        export_animations=True,       # harmless for static meshes; keeps clips if present
        export_skins=True,            # keep rigs for the animation experiment
        export_extras=True,           # keep attribution/licence custom props
        export_vertex_color="MATERIAL",
    )
    print(f"[export] wrote {filepath}")
    return filepath


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def main() -> int:
    args = parse_args(sys.argv)
    print(f"[args] input={args.input!r} output_dir={args.output_dir!r} "
          f"target_size={args.target_size} up={args.up_axis}")

    reset_scene()
    objects = import_mesh(args.input)
    if not objects:
        print("[error] import produced no mesh objects", file=sys.stderr)
        return 2

    report("raw", objects)
    normalise(objects, args.target_size, center_xz=not args.no_recenter_xz)
    report("normalised", objects)  # triangle count is unchanged; bbox should be tidy

    name = args.output_name or (
        os.path.splitext(os.path.basename(args.input))[0] + "_unity.glb"
    )
    export_glb(objects, args.output_dir, name, up_axis=args.up_axis)

    mins, maxs = world_bounds(objects)
    dims = maxs - mins
    print(
        f"[done] {name}: {triangle_count(objects)} tris, "
        f"size {dims.x:.3f} x {dims.y:.3f} x {dims.z:.3f} m"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())