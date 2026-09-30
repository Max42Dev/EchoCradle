# Rig Checklist — Blender 5.2 auto-rig + retarget

Concrete steps for **approach (ii): template rig + auto-skin + retargeted stock
clips**, from the parent [README](../README.md). Written for headless
(`blender --background --python`) use, and annotated with which parts can be
**scripted** versus which need a human eye.

Legend: ✅ fully scriptable · ✍️ manual/visual judgement · 🔌 external tool.

---

## 0. Preconditions

1. ✅ **Mesh is clean and scaled.** Run
   `../0103-image-to-3d-selection/src/blender_import_export.py` first: apply scale,
   recentre, set the base at Z=0, know the triangle count.
2. ✅ **Mesh is manifold enough to weight.** Generated meshes often aren't. Quick
   check before rigging:

   ```python
   import bpy, bmesh
   bm = bmesh.new(); bm.from_mesh(obj.data)
   loose = [v for v in bm.verts if not v.link_edges]
   print("loose verts:", len(loose))
   ```

   Non-manifold geometry does **not** have to be fixed, but expect weight cleanup.
3. ✍️ **Decide the bone-naming standard now** (e.g. Rigify names, or a custom
   `Hips/Spine_01/...` set). Retargeting only works reliably if clip bones and rig
   bones agree on names + rest pose.

---

## 1. Rig — choose one

### 1a. Template metarig (humanoids) — ✅ mostly scriptable

```python
import bpy, addon_utils
addon_utils.enable("rigify", default_set=True, persistent=True)

# Adds the standard biped metarig (needs a UI context normally):
bpy.ops.object.armature_human_metarig_add()
```

- ✍️ **Align the metarig to the mesh** (move/rotate/scale the metarig so hips,
  knees, elbows, wrists sit inside the mesh). This is the one visually-judged step;
  for a known character class it can be scripted from a *measured* offset.
- ✅ Generate the control rig:

  ```python
  bpy.ops.pose.rigify_generate()
  ```

- ✅ For quadrupeds/props, keep a small set of prepared metarig `.blend` files and
  append them instead of `armature_human_metarig_add()`.

### 1b. Learned rig (arbitrary topology) — 🔌 outside Blender

UniRig predicts a skeleton and skin weights and hands you `.fbx` back:

```powershell
bash launch/inference/generate_skeleton.sh --input item.glb  --output out/item_skel.fbx
bash launch/inference/generate_skin.sh     --input out/item_skel.fbx --output out/item_skin.fbx
bash launch/inference/merge.sh --source out/item_skin.fbx --target item.glb --output out/item_rigged.glb
```

- ✅ then re-import `item_rigged.glb` in Blender and continue from §3.
- ✍️ **Review before trusting.** The released checkpoints are drafts; the skeleton
  is the part most worth fixing by hand.
- ⚠️ Never merge a *skeleton-only* `.fbx` expecting skinning — merge the
  **skin** result.

---

## 2. Parent + auto-skin — ✅ scriptable

```python
bpy.ops.object.select_all(action="DESELECT")
mesh.select_set(True)
armature.select_set(True)
bpy.context.view_layer.objects.active = armature          # armature must be active
bpy.ops.object.parent_set(type="ARMATURE_AUTO")           # bone-heat auto weights
```

`type` accepts `ARMATURE_AUTO` (bone heat), `ARMATURE_ENVELOPE` (fallback when
bone heat fails), `ARMATURE_NAME` (match vertex groups by bone name — used when
weights already exist), `BONE`, `VERTEX`, …

Practical notes:
- Bone heat **fails on non-manifold/self-intersecting meshes** ("Bone Heat
  Weighting: failed to find solution"). Fall back to `ARMATURE_ENVELOPE`, then fix.
- Objects scaled non-uniformly weight badly — apply scale before parenting.

---

## 3. Weight cleanup — ✅ scriptable

```python
bpy.context.view_layer.objects.active = mesh
bpy.ops.object.mode_set(mode="WEIGHT_PAINT")
bpy.ops.object.vertex_group_clean(group_select_mode="ALL", limit=0.01)
bpy.ops.object.vertex_group_limit_total(limit=4)   # engines want <= 4 influences
bpy.ops.object.mode_set(mode="OBJECT")
```

- ✅ Optional smoothing: enable *Weight Paint → Weights → Smooth*, or
  `bpy.ops.object.vertex_group_smooth(...)`.
- ✍️ **Inspect hotspots** (armpits, between fingers, thin spikes) with the Weight
  Paint view. Generated blobby meshes almost always need some paint-in here.

---

## 4. Verify — ✅ scriptable (this is the gate)

```powershell
& "C:\Program Files\Blender Foundation\Blender 5.2\blender.exe" --background `
  --factory-startup --python ..\src\blender_rig_check.py -- out\item_rigged.glb
```

It reports armatures/bones, **unweighted vertices**, **max influences**, clips and
whether the pose is clean. Treat `ready_for_gltf_export == False` as a build error.

---

## 5. Retarget stock clips — 🔌 author once, ✅ apply per asset

Clips are an **offline authoring asset** (see README). Generate/model them once and
ship them; they are tiny.

- 🔌 **Source options:** hand-authored in Blender; Mixamo-style libraries
  (check licence); or generated once with MDM/MotionGPT *if* a licence-clean body
  model is adopted (⚠️ SMPL licence).
- ✅ **Apply in Blender** with the standard retarget flow:

  ```python
  # 1. Append/import the source (rigged + animated) clip file.
  # 2. Add a Copy Transforms constraint from our bones to the source bones,
  #    matching names:
  for pb in our_rig.pose.bones:
      src = src_rig.pose.bones.get(pb.name)
      if src:
          c = pb.constraints.new("COPY_TRANSFORMS")
          c.target = src_rig
          c.subtarget = src.name
  # 3. Bake the constrained pose to our rig's own action:
  bpy.ops.nla.bake(frame_start=..., frame_end=...,
                   only_selected=False, visual_keying=True, clear_constraints=True)
  ```

  `nla.bake(..., clear_constraints=True)` is the key call: it bakes the retargeted
  motion onto our rig and removes the constraints so the clip is self-contained.
- ✅ **Name and stash the clip** so the glTF exporter can see it (see §6).
- ✍️ Watch for foot-sliding and root-motion sign/axis differences between clips;
  a fixed convention (metres, Z-up in Blender, Y-up at export) prevents most of it.

---

## 6. Stash clips for glTF export — ✅ scriptable

The glTF exporter only sees actions that are **active or stashed on an NLA track**.
Headless, use the API (the `nla.action_pushdown` *operator* needs a UI context and
will fail in `--background`):

```python
action = rig.animation_data.action      # the clip you just baked
action.name = "Attack"
track = rig.animation_data.nla_tracks.new()
track.name = "Attack"                   # glTF animation name comes from the track/action
track.strips.new("Attack", int(action.frame_range[0]), action)
rig.animation_data.action = None        # stash, don't leave it active
```

---

## 7. Export for Unity — ✅ scriptable

```python
bpy.ops.export_scene.gltf(
    filepath="out/item_animated.glb",
    export_format="GLB",
    export_animations=True,
    export_animation_mode="ACTIONS",        # one glTF animation per action
    export_skins=True,
    export_influence_nb=4,                  # <= 4 bone influences
    export_yup=True,                        # Unity convention
    export_apply=True,                      # apply modifiers (not armatures)
    export_rest_position_armature=True,     # joints use the rest pose
    export_extras=True,                     # keep attribution/licence props
)
```

Then import in Unity and confirm the **Animator** sees the clips and the avatar's
root/hips sit correctly.

---

## What is scriptable, summarised

| Step | Scriptable | Needs a human |
|---|---|---|
| Clean + scale mesh | ✅ | |
| Add humanoid metarig | ✅ | ✍️ align to mesh |
| Generate control rig | ✅ | |
| UniRig skeleton/skin | ✅ via CLI (🔌) | ✍️ review skeleton |
| Auto-skin (`ARMATURE_AUTO`) | ✅ | |
| Weight cleanup | ✅ | ✍️ paint hotspots |
| Rig verification | ✅ (`blender_rig_check.py`) | |
| Retarget clip (`nla.bake`) | ✅ after clips exist | |
| Author the clips | | ✍️ / 🔌 (offline, once) |
| Stash clips on NLA | ✅ | |
| Export GLB | ✅ | |
| Unity import check | | ✍️ one-time |

**Net:** the per-asset pipeline (clean → rig → skin → clean weights → verify →
retarget → stash → export) is fully scriptable **once a rig template and a clip
library exist**. Those two are the assets to invest in.