"""Rig/skin health check for a generated or retargeted mesh.

Placeholder for the concrete "animation pipeline" step of the Model
Orchestrator: before an animated asset ships, verify that it is actually
posable. This script *reads* an imported file and reports; it never rigs.

Run headless::

    & "C:\\Program Files\\Blender Foundation\\Blender 5.2\\blender.exe" `
        --background --factory-startup `
        --python blender_rig_check.py -- <input.glb> [--export-config out.json]

It answers the questions that decide whether animation is feasible at all:

* How many armatures / bones, and are the bones deform bones?
* Does every vertex have skin weights (dead weight = unweighted vertices)?
* How many influences per vertex (UniRig/most engines want <= 4)?
* Which animation clips (actions) are present and how long are they?

The "how to fix it" side lives in ``rig_checklist.md`` next to this file and is
deliberately kept out of code so the manual Blender steps stay readable.
"""

from __future__ import annotations

import argparse
import json
import os
import sys

import bpy
from mathutils import Matrix

# ---------------------------------------------------------------------------
# Args
# ---------------------------------------------------------------------------


def parse_args(argv: list[str]) -> argparse.Namespace:
    if "--" in argv:
        argv = argv[argv.index("--") + 1 :]
    else:  # pragma: no cover
        argv = []
    p = argparse.ArgumentParser(prog="blender_rig_check.py")
    p.add_argument("input", help="Rigged mesh file (.glb, .gltf, .fbx, .obj)")
    p.add_argument("--export-config", default=None,
                   help="Optional path to write the report as JSON (for the orchestrator).")
    return p.parse_args(argv)


# ---------------------------------------------------------------------------
# Import (same extension routing as blender_import_export.py in experiment 0103)
# ---------------------------------------------------------------------------


def import_any(path: str) -> None:
    ext = os.path.splitext(path)[1].lower()
    if ext in (".glb", ".gltf"):
        bpy.ops.import_scene.gltf(filepath=path)
    elif ext == ".fbx":
        bpy.ops.import_scene.fbx(filepath=path)
    elif ext == ".obj":
        bpy.ops.wm.obj_import(filepath=path)
    else:
        raise ValueError(f"Unsupported extension: {ext!r}")


# ---------------------------------------------------------------------------
# Checks
# ---------------------------------------------------------------------------


def check_armatures() -> list[dict]:
    out = []
    for obj in bpy.context.scene.objects:
        if obj.type != "ARMATURE":
            continue
        bones = obj.data.bones
        deform = [b.name for b in bones if b.use_deform]
        out.append(
            {
                "name": obj.name,
                "bones": len(bones),
                "deform_bones": len(deform),
                "non_deform": [b.name for b in bones if not b.use_deform],
                "categories": _classify_bones(obj.name),
            }
        )
        print(f"[armature] {obj.name}: {len(bones)} bones ({len(deform)} deform)")
    return out


def _classify_bones(armature_name: str) -> list[str]:
    """Very rough label hints used to sanity-check a humanoid rig."""
    hints = ("hips", "spine", "chest", "head", "hand", "foot", "arm", "leg", "neck")
    return [h for h in hints if h in armature_name.lower()]


def check_skinning() -> list[dict]:
    """Report weight coverage per mesh object."""
    out = []
    for obj in bpy.context.scene.objects:
        if obj.type != "MESH":
            continue
        groups = {g.index: g.name for g in obj.vertex_groups}
        if not groups:
            out.append({"name": obj.name, "groups": 0, "unweighted": len(obj.data.vertices),
                        "max_influences": 0, "weighted": False})
            print(f"[skin] {obj.name}: NO vertex groups ({len(obj.data.vertices)} verts)")
            continue

        unweighted = 0
        max_influences = 0
        over_four = 0
        for v in obj.data.vertices:
            influences = [g for g in v.groups if g.weight > 1e-4]
            max_influences = max(max_influences, len(influences))
            if len(influences) > 4:
                over_four += 1
            if not influences:
                unweighted += 1

        entry = {
            "name": obj.name,
            "groups": len(groups),
            "verts": len(obj.data.vertices),
            "unweighted": unweighted,
            "max_influences": max_influences,
            "verts_over_4_influences": over_four,
            "weighted": unweighted == 0,
        }
        out.append(entry)
        print(
            f"[skin] {obj.name}: {len(groups)} groups, "
            f"{unweighted}/{len(obj.data.vertices)} unweighted, "
            f"max influences={max_influences}, >4={over_four}"
        )
    return out


def check_clips() -> list[dict]:
    """List actions, their frame ranges, and whether they are stashed on NLA.

    glTF export can only see actions that are active *or* stashed on an NLA
    track, so 'stashed' is the thing to verify before export.
    """
    stashed = set()
    for obj in bpy.context.scene.objects:
        if obj.animation_data and obj.animation_data.nla_tracks:
            for track in obj.animation_data.nla_tracks:
                for strip in track.strips:
                    if strip.action:
                        stashed.add(strip.action.name)

    out = []
    for action in bpy.data.actions:
        start, end = action.frame_range
        entry = {
            "name": action.name,
            "frames": [round(start), round(end)],
            "stashed_on_nla": action.name in stashed,
        }
        out.append(entry)
        print(f"[clip] {action.name}: frames {entry['frames']} stashed={entry['stashed_on_nla']}")
    return out


def rest_pose_ok() -> bool:
    """Warn if the current pose differs from rest (export uses rest position)."""
    ok = True
    identity = Matrix.Identity(4)
    for obj in bpy.context.scene.objects:
        if obj.type != "ARMATURE":
            continue
        for pb in obj.pose.bones:
            deviation = max(
                abs(pb.matrix_basis[row][col] - identity[row][col])
                for row in range(4)
                for col in range(4)
            )
            if deviation > 1e-4:
                print(f"[warn] {obj.name}:{pb.name} is posed away from rest "
                      f"(max deviation {deviation:.4f})")
                ok = False
    return ok


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def main() -> int:
    args = parse_args(sys.argv)
    bpy.ops.wm.read_factory_settings(use_empty=True)
    import_any(args.input)

    report = {
        "input": args.input,
        "armatures": check_armatures(),
        "skinning": check_skinning(),
        "clips": check_clips(),
        "rest_pose_clean": rest_pose_ok(),
    }
    report["ready_for_gltf_export"] = bool(
        report["armatures"]
        and all(s.get("weighted") for s in report["skinning"])
        and all(c["stashed_on_nla"] for c in report["clips"] or [{"stashed_on_nla": True}])
    )
    print(f"[verdict] ready_for_gltf_export={report['ready_for_gltf_export']}")

    if args.export_config:
        with open(args.export_config, "w", encoding="utf-8") as fh:
            json.dump(report, fh, indent=2)
        print(f"[report] wrote {args.export_config}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())