# Character preview (Built-in Render Pipeline, Unity 6)

Independent runtime assembly: `EchoCradle.CharacterPreview`. The parent controller's
asmdef must explicitly reference it (for example add `EchoCradle.CharacterPreview`
to `EchoCradle.Runtime.references`). That existing assembly is deliberately not
edited by this work. Namespace uses C# 9-compatible block syntax.

## Public API

Add `PlayerCharacterPreview` to an enabled GameObject. Invoke **all** APIs on the
Unity main thread after `Awake`; await tasks normally (do not use `Task.Run`,
`.Wait()`, or `ConfigureAwait(false)` around Unity calls).

- `ShowImage(byte[])`, or `Task ShowImageAsync(byte[], CancellationToken)`:
  PNG/JPEG decoding; image texture is published immediately. Async wrapper does
  not move Unity image decoding off the main thread.
- `Task ShowModelAsync(byte[], CancellationToken)`: self-contained GLB 2.0;
  rejects external file/network resources, imported lights/cameras and non-Built-in RP.
- `Texture PreviewTexture`: borrowed image/RenderTexture. Parent draws using
  `GUI.DrawTexture(rect, texture, ScaleMode.ScaleToFit)` or `RawImage.texture`.
  There is intentionally no independent OnGUI panel.
- `bool IsModel`: successful current preview is a model, not merely loading.
- `IReadOnlyList<string> ClipNames`: actual imported legacy animation state names;
  empty for static meshes. `PlayClip(string)` loops one exact name.
- `Clear()`: cancels pending publication and destroys owned resources. Also called
  on disable/destroy. Texture references must not be retained across replacement.

The last successful preview remains visible during replacement. Await/handle
`OperationCanceledException` and decode/import exceptions in the caller. Requests
are serialized; latest request wins. Native importer jobs are allowed to settle
before disposal: cancellation is not an instantaneous memory-release guarantee.
Caller must not mutate model bytes until the returned task settles. Clear or
destroy during import also drains/disposes the in-flight importer safely.

Rendering uses an unnamed user layer reserved per preview, camera + two lights,
and 512x512 RenderTexture. Meshes are framed from actual world renderer bounds,
including depth, at import time. Camera/light/renderers are enabled only inside
the synchronous manual render; game cameras cannot draw preview meshes. The rig
is detached from controller transforms and far from normal scene geometry.
Reserve unused unnamed layers for this system; do not put gameplay objects on
those layers. Animated poses substantially outside the initial bounds may need a
future camera reframe policy. Layer isolation does not override global ambient/
fog settings or exclude game lights whose masks include the reserved layer.

## Verified dependency (2026-10-24)

- Official anonymous registry: <https://packages.unity.com/com.unity.cloud.gltfast>
- Exact pin: **com.unity.cloud.gltfast 6.20.0**, registry minimum Unity **6000.0**;
  project version **6000.6.3f1**. Registry tarball was inspected (~2 MB), not models.
- Apache-2.0 license (commercial/worldwide grant; retain license/notices):
  <https://docs.unity3d.com/Packages/com.unity.cloud.gltfast@6.20/license/LICENSE.html>.
  No cloud AI, accounts, model downloads, Draco/KTX or ShaderGraph added.
- Required transitive registry dependencies: Burst 1.8.30, Collections 2.6.8,
  Mathematics 1.3.3, JSONSerialize and UnityWebRequest modules. UPM resolves these;
  the parent Unity integration pass owns package resolution/lockfile updates.
- Explicit built-in modules added: Animation, ImageConversion,
  UnityWebRequestTexture (for glTFast's embedded PNG/JPEG texture loader).

Exact 6.20.0 archive source and public API verified before manifest edit:
`GltfImport.LoadGltfBinary(byte[], Uri, ImportSettings, CancellationToken)`,
`InstantiateMainSceneAsync(IInstantiator, CancellationToken)`,
`GameObjectInstantiator(..., InstantiationSettings)`,
`ImportSettings.AnimationMethod = AnimationMethod.Legacy`,
`SceneInstance.LegacyAnimation`, `BuiltInMaterialGenerator`, `GltfImport.Dispose()`.

References:
- <https://docs.unity3d.com/Packages/com.unity.cloud.gltfast@6.20/api/GLTFast.GltfImport.html>
- <https://docs.unity3d.com/Packages/com.unity.cloud.gltfast@6.20/api/GLTFast.ImportSettings.html>
- <https://docs.unity3d.com/Packages/com.unity.cloud.gltfast@6.20/manual/ProjectSetup.html>

Resources contains three placeholder materials referencing the exact package
Built-in shader GUIDs so the shader assets are included in players. They retain
base variants, **not every possible runtime keyword combination**. Before shipping
arbitrary normal/alpha/UV-transform material combinations, capture their shader
variants and include a ShaderVariantCollection per the official setup guide;
validate a player with Strict Shader Variant Matching. No global graphics or
player settings are changed here.

## Tests (parent runs Unity once after integration)

PlayMode assembly contains empty/clear, PNG, invalid GLB, unknown clip tests.
Real importer tests are Editor-only and opt-in: set
`ECHOCRADLE_PREVIEW_FIXTURE_TESTS=1` **before launching Unity**. Default fixtures
are the existing `results/orchestrator-smoke-2026-10-24_14-40-11/step_2_mesh.glb`
and `step_3_animated.glb` under experiment 0110. No copying or generation occurs.
Override paths with `ECHOCRADLE_PREVIEW_STATIC_GLB` /
`ECHOCRADLE_PREVIEW_ANIMATED_GLB`. Tests check actual meshes/material shaders,
static import, named clip playback, and cancellation/supersession cleanup.

Unity compilation, PlayMode and player rendering remain for the parent pass;
no concurrent Editor or GPU generation was started by this implementation.