using System;
using System.Collections.Generic;
using System.IO;
using System.Threading;
using System.Threading.Tasks;
using GLTFast;
using GLTFast.Materials;
using UnityEngine;
using UnityEngine.Rendering;

namespace EchoCradle.CharacterPreview
{
    /// <summary>
    /// Main-thread-only, Built-in RP image/GLB preview. The caller draws
    /// PreviewTexture (RawImage or GUI.DrawTexture); this component owns it.
    /// Replacing/clearing/disabling cancels pending requests. Await every request
    /// and handle OperationCanceledException and import/decode exceptions.
    /// </summary>
    [DisallowMultipleComponent]
    public sealed class PlayerCharacterPreview : MonoBehaviour
    {
        private const int MaxModelBytes = 128 * 1024 * 1024;
        private const int MaxImageBytes = 16 * 1024 * 1024;
        private static readonly bool[] ReservedLayers = new bool[32];

        [SerializeField, Range(128, 2048), Tooltip("Square model preview resolution.")]
        private int textureSize = 512;

        [SerializeField, Tooltip("Background of the model preview only.")]
        private Color background = new Color(0.08f, 0.09f, 0.12f, 1f);

        private readonly SemaphoreSlim _importGate = new SemaphoreSlim(1, 1);
        private readonly List<string> _clipNames = new List<string>();
        private IReadOnlyList<string> _readOnlyClipNames;
        private int _mainThreadId;
        private int _generation;
        private int _requestCount;
        private int _layer = -1;
        private bool _destroyed;
        private CancellationTokenSource _pending;
        private Texture2D _image;
        private RenderTexture _renderTexture;
        private GameObject _rig;
        private GameObject _model;
        private Camera _camera;
        private Light _keyLight;
        private Light _fillLight;
        private Renderer[] _renderers = Array.Empty<Renderer>();
        private Animation _animation;
        private GltfImport _importer;

        /// <summary>Borrowed texture: do not destroy or retain across Clear/replacement.</summary>
        public Texture PreviewTexture => IsModel ? _renderTexture : _image;
        public bool IsModel => _model != null;
        public IReadOnlyList<string> ClipNames => _readOnlyClipNames ??= _clipNames.AsReadOnly();

        private void Awake()
        {
            _mainThreadId = Thread.CurrentThread.ManagedThreadId;
        }

        /// <summary>Decode PNG/JPEG on the Unity thread; replaces any current request.</summary>
        public void ShowImage(byte[] bytes)
        {
            RequireUsable();
            ValidateBytes(bytes, MaxImageBytes);
            var image = new Texture2D(2, 2, TextureFormat.RGBA32, false);
            try
            {
                if (!ImageConversion.LoadImage(image, bytes, true))
                    throw new InvalidDataException("Preview image is not a supported PNG/JPEG.");
                if (image.width > 8192 || image.height > 8192)
                    throw new InvalidDataException("Preview image dimensions exceed 8192 pixels.");
                image.name = "Character preview image";
                image.hideFlags = HideFlags.HideAndDontSave;
                Clear();
                _image = image;
                image = null;
            }
            finally
            {
                if (image != null) Destroy(image);
            }
        }

        public Task ShowImageAsync(byte[] bytes, CancellationToken cancellationToken = default)
        {
            RequireUsable();
            cancellationToken.ThrowIfCancellationRequested();
            ShowImage(bytes);
            return Task.CompletedTask;
        }

        /// <summary>
        /// Loads an embedded GLB 2.0 using glTFast. Invoke on Unity's main thread.
        /// Do not mutate bytes before this task completes. Cancellation is checked
        /// between importer stages, not by disposing resources under running jobs.
        /// A failed/cancelled replacement leaves the last successful preview intact.
        /// </summary>
        public async Task ShowModelAsync(byte[] bytes, CancellationToken cancellationToken = default,
            bool sourceZUp = false)
        {
            RequireUsable();
            ValidateGlb(bytes);
            cancellationToken.ThrowIfCancellationRequested();
            if (GraphicsSettings.currentRenderPipeline != null)
                throw new NotSupportedException("CharacterPreview currently requires the Built-in Render Pipeline.");

            _pending?.Cancel();
            var request = CancellationTokenSource.CreateLinkedTokenSource(cancellationToken);
            _pending = request;
            ++_requestCount;
            int generation = ++_generation;
            CancellationToken token = request.Token;
            GltfImport importer = null;
            GameObject model = null;
            bool entered = false;
            try
            {
                // UnitySynchronizationContext is deliberately captured by all awaits.
                await _importGate.WaitAsync(token);
                entered = true;
                CheckRequest(generation, token);
                EnsureRig();
                model = new GameObject("Imported character");
                model.hideFlags = HideFlags.HideAndDontSave;
                model.transform.SetParent(_rig.transform, false);
                // TripoSR writes Z-up geometry without converting it to glTF Y-up.
                // glTFast flips X (not Z) for Unity handedness: -90 X maps source
                // +Z to up and source +Y/front toward our camera on negative Z.
                // Rotate the wrapper, never imported nodes or animation tracks.
                if (sourceZUp) model.transform.localRotation = Quaternion.Euler(-90f, 0f, 0f);
                model.layer = _layer;
                model.SetActive(false);
                importer = new GltfImport(
                    downloadProvider: new EmbeddedOnlyDownloadProvider(),
                    materialGenerator: new BuiltInMaterialGenerator());
                var settings = new ImportSettings
                {
                    AnimationMethod = AnimationMethod.Legacy,
                    NodeNameMethod = NameImportMethod.OriginalUnique
                };

                // Do not pass cancellation into loading: this release contains
                // cancellation paths around native jobs. Let the stage settle,
                // then dispose safely in finally if this request was superseded.
                bool loaded = await importer.LoadGltfBinary(bytes, importSettings: settings);
                CheckRequest(generation, token);
                if (!loaded) throw new InvalidDataException("glTFast could not load the character GLB.");

                var instantiator = new GameObjectInstantiator(importer, model.transform,
                    settings: new InstantiationSettings
                    {
                        Layer = _layer,
                        Mask = ComponentType.Mesh | ComponentType.Animation,
                        SkinUpdateWhenOffscreen = true
                    });
                bool instantiated = await importer.InstantiateMainSceneAsync(instantiator);
                CheckRequest(generation, token);
                if (!instantiated) throw new InvalidDataException("glTFast could not instantiate the GLB main scene.");

                Renderer[] renderers = model.GetComponentsInChildren<Renderer>(true);
                if (renderers.Length == 0) throw new InvalidDataException("GLB main scene contains no renderable meshes.");
                foreach (Renderer renderer in renderers)
                {
                    renderer.enabled = false;
                    renderer.shadowCastingMode = ShadowCastingMode.Off;
                    renderer.receiveShadows = false;
                }
                Animation animation = instantiator.SceneInstance.LegacyAnimation;
                animation?.Stop();
                model.SetActive(true);
                FrameModel(renderers);
                CheckRequest(generation, token);

                ReleaseContent();
                _model = model;
                _importer = importer;
                _renderers = renderers;
                _animation = animation;
                model = null;
                importer = null;
                if (_animation != null)
                {
                    foreach (AnimationState state in _animation)
                        _clipNames.Add(state.name);
                }
                RenderModel();
            }
            finally
            {
                // Only local request-owned objects are disposed here. Never tear
                // down a newer successful request from an older continuation.
                if (model != null)
                {
                    model.SetActive(false);
                    Destroy(model);
                }
                importer?.Dispose();
                if (entered) _importGate.Release();
                if (ReferenceEquals(_pending, request))
                {
                    _pending = null;
                }
                --_requestCount;
                if (_requestCount == 0 && !IsModel) ReleaseRig();
                request.Dispose();
            }
        }

        /// <summary>Play a named legacy clip in a loop. Unknown names are errors.</summary>
        public void PlayClip(string clipName)
        {
            RequireUsable();
            if (_animation == null || string.IsNullOrEmpty(clipName) || _animation[clipName] == null)
                throw new ArgumentException("The preview has no animation with that name.", nameof(clipName));
            _animation[clipName].wrapMode = WrapMode.Loop;
            _animation.Play(clipName);
        }

        /// <summary>Cancel pending imports, clear borrowed textures and release owned resources.</summary>
        public void Clear()
        {
            RequireMainThread();
            ++_generation;
            _pending?.Cancel();
            ReleaseContent();
            // An in-flight importer may still be using a staging child of the rig.
            // Its finally block releases the rig once the stage has completed.
            if (_requestCount == 0) ReleaseRig();
        }

        private void LateUpdate()
        {
            if (IsModel) RenderModel();
        }

        private void OnDisable() => Clear();

        private void OnDestroy()
        {
            Clear();
            _destroyed = true;
        }

        private void RequireMainThread()
        {
            if (_mainThreadId == 0 || Thread.CurrentThread.ManagedThreadId != _mainThreadId)
                throw new InvalidOperationException("CharacterPreview APIs must be called on Unity's main thread after Awake.");
        }

        private void RequireUsable()
        {
            RequireMainThread();
            if (_destroyed || !isActiveAndEnabled)
                throw new InvalidOperationException("CharacterPreview must be alive and enabled.");
        }

        private void CheckRequest(int generation, CancellationToken token)
        {
            token.ThrowIfCancellationRequested();
            if (_destroyed || generation != _generation || !isActiveAndEnabled)
                throw new OperationCanceledException("Character preview request was superseded.", token);
        }

        private static void ValidateBytes(byte[] bytes, int maximum)
        {
            if (bytes == null) throw new ArgumentNullException(nameof(bytes));
            if (bytes.Length == 0 || bytes.Length > maximum)
                throw new InvalidDataException("Preview byte count is empty or exceeds its limit.");
        }

        private static void ValidateGlb(byte[] bytes)
        {
            ValidateBytes(bytes, MaxModelBytes);
            if (bytes.Length < 20 || bytes[0] != 0x67 || bytes[1] != 0x6c ||
                bytes[2] != 0x54 || bytes[3] != 0x46 || ReadUInt32(bytes, 4) != 2 ||
                ReadUInt32(bytes, 8) != (uint)bytes.Length)
                throw new InvalidDataException("Preview requires a complete GLB 2.0 file.");
        }

        private static uint ReadUInt32(byte[] bytes, int offset)
        {
            return (uint)(bytes[offset] | bytes[offset + 1] << 8 |
                bytes[offset + 2] << 16 | bytes[offset + 3] << 24);
        }

        private void EnsureRig()
        {
            if (_rig != null) return;
            // Reserve an unnamed user layer per component, without changing
            // TagManager or other cameras' masks. Renderers stay disabled outside
            // our synchronous Camera.Render, so game cameras never see them.
            for (int layer = 31; layer >= 8; --layer)
            {
                if (!ReservedLayers[layer] && string.IsNullOrEmpty(LayerMask.LayerToName(layer)))
                {
                    _layer = layer;
                    ReservedLayers[layer] = true;
                    break;
                }
            }
            if (_layer < 0) throw new InvalidOperationException("No unused unnamed user layer is available for preview rendering.");
            _rig = new GameObject("Character preview rig") { hideFlags = HideFlags.HideAndDontSave, layer = _layer };
            _rig.transform.position = new Vector3(10000f + _layer * 100f, 10000f, 10000f);
            _renderTexture = new RenderTexture(Mathf.Clamp(textureSize, 128, 2048),
                Mathf.Clamp(textureSize, 128, 2048), 24, RenderTextureFormat.ARGB32)
            {
                name = "Character model preview", hideFlags = HideFlags.HideAndDontSave
            };
            if (!_renderTexture.Create()) throw new InvalidOperationException("Could not create preview RenderTexture.");
            _camera = CreateChild("Preview camera").AddComponent<Camera>();
            _camera.enabled = false;
            _camera.cullingMask = 1 << _layer;
            _camera.clearFlags = CameraClearFlags.SolidColor;
            _camera.backgroundColor = background;
            _camera.targetTexture = _renderTexture;
            _camera.fieldOfView = 35f;
            _camera.aspect = 1f;
            _camera.allowHDR = false;
            _camera.allowMSAA = false;
            _keyLight = CreateLight("Preview key", new Vector3(35f, -35f, 0f), 1.1f);
            _fillLight = CreateLight("Preview fill", new Vector3(15f, 145f, 0f), 0.65f);
        }

        private GameObject CreateChild(string childName)
        {
            var child = new GameObject(childName) { hideFlags = HideFlags.HideAndDontSave, layer = _layer };
            child.transform.SetParent(_rig.transform, false);
            return child;
        }

        private Light CreateLight(string lightName, Vector3 angles, float intensity)
        {
            Light light = CreateChild(lightName).AddComponent<Light>();
            light.type = LightType.Directional;
            light.intensity = intensity;
            light.cullingMask = 1 << _layer;
            light.shadows = LightShadows.None;
            light.transform.localRotation = Quaternion.Euler(angles);
            light.enabled = false;
            return light;
        }

        private void FrameModel(Renderer[] renderers)
        {
            Bounds bounds = renderers[0].bounds;
            for (int i = 1; i < renderers.Length; ++i) bounds.Encapsulate(renderers[i].bounds);
            float radius = bounds.extents.magnitude;
            if (float.IsNaN(radius) || float.IsInfinity(radius) || radius <= 0.00001f ||
                !IsFinite(bounds.center))
                throw new InvalidDataException("Character has empty or non-finite renderer bounds.");
            float distance = radius * 1.15f / Mathf.Sin(_camera.fieldOfView * Mathf.Deg2Rad * 0.5f);
            _camera.transform.position = bounds.center + new Vector3(0f, 0f, -distance);
            _camera.transform.rotation = Quaternion.identity;
            _camera.nearClipPlane = Mathf.Max(0.001f, distance - radius * 1.5f);
            _camera.farClipPlane = distance + radius * 3f;
        }

        private static bool IsFinite(Vector3 value)
        {
            return !float.IsNaN(value.x) && !float.IsInfinity(value.x) &&
                !float.IsNaN(value.y) && !float.IsInfinity(value.y) &&
                !float.IsNaN(value.z) && !float.IsInfinity(value.z);
        }

        private void RenderModel()
        {
            RenderTexture previous = RenderTexture.active;
            try
            {
                foreach (Renderer renderer in _renderers) renderer.enabled = true;
                _keyLight.enabled = true;
                _fillLight.enabled = true;
                _camera.Render();
            }
            finally
            {
                foreach (Renderer renderer in _renderers) renderer.enabled = false;
                _keyLight.enabled = false;
                _fillLight.enabled = false;
                RenderTexture.active = previous;
            }
        }

        private void ReleaseContent()
        {
            _animation?.Stop();
            _animation = null;
            _clipNames.Clear();
            _renderers = Array.Empty<Renderer>();
            if (_model != null)
            {
                _model.SetActive(false);
                Destroy(_model);
                _model = null;
            }
            _importer?.Dispose();
            _importer = null;
            if (_image != null) Destroy(_image);
            _image = null;
        }

        private void ReleaseRig()
        {
            if (_camera != null) _camera.targetTexture = null;
            if (_rig != null)
            {
                _rig.SetActive(false);
                Destroy(_rig);
            }
            _rig = null;
            _camera = null;
            _keyLight = null;
            _fillLight = null;
            if (_renderTexture != null)
            {
                _renderTexture.Release();
                Destroy(_renderTexture);
                _renderTexture = null;
            }
            if (_layer >= 0) ReservedLayers[_layer] = false;
            _layer = -1;
        }
    }
}