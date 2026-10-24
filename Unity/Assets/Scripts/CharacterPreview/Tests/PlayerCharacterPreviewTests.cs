using System;
using System.Collections;
using System.IO;
using System.Threading;
using System.Threading.Tasks;
using NUnit.Framework;
using UnityEngine;
using UnityEngine.TestTools;

namespace EchoCradle.CharacterPreview.Tests
{
    public sealed class PlayerCharacterPreviewTests
    {
        private GameObject _owner;
        private PlayerCharacterPreview _preview;

        [SetUp]
        public void SetUp()
        {
            _owner = new GameObject("Preview test owner");
            _preview = _owner.AddComponent<PlayerCharacterPreview>();
        }

        [TearDown]
        public void TearDown()
        {
            if (_owner != null) UnityEngine.Object.DestroyImmediate(_owner);
        }

        [Test]
        public void Clear_Empty_IsIdempotent()
        {
            _preview.Clear();
            _preview.Clear();
            Assert.IsNull(_preview.PreviewTexture);
            Assert.IsFalse(_preview.IsModel);
            Assert.IsEmpty(_preview.ClipNames);
        }

        [Test]
        public void ShowImage_ValidPng_OwnsAndClearsTexture()
        {
            var source = new Texture2D(2, 2);
            byte[] bytes;
            try { bytes = source.EncodeToPNG(); }
            finally { UnityEngine.Object.DestroyImmediate(source); }
            _preview.ShowImage(bytes);
            Assert.IsNotNull(_preview.PreviewTexture);
            Assert.AreEqual(2, _preview.PreviewTexture.width);
            Assert.IsFalse(_preview.IsModel);
            _preview.Clear();
            Assert.IsNull(_preview.PreviewTexture);
        }

        [Test]
        public void PlayClip_NoModel_RejectsUnknownClip()
        {
            Assert.Throws<ArgumentException>(() => _preview.PlayClip("Idle"));
        }

        [UnityTest]
        public IEnumerator ShowModel_InvalidHeader_FaultsWithoutPublishing()
        {
            Task task = _preview.ShowModelAsync(new byte[] { 1, 2, 3 });
            yield return WaitFor(task);
            Assert.IsTrue(task.IsFaulted);
            Assert.IsInstanceOf<InvalidDataException>(task.Exception.GetBaseException());
            Assert.IsNull(_preview.PreviewTexture);
        }

        [UnityTest]
        public IEnumerator ShowModel_ExistingStaticOutput_ZUpIsUprightAndFacesCamera()
        {
            byte[] bytes = ReadFixture("ECHOCRADLE_PREVIEW_STATIC_GLB", "step_2_mesh.glb");
            Task task = _preview.ShowModelAsync(bytes, sourceZUp: true);
            yield return WaitFor(task);
            AssertCompleted(task);
            Assert.IsTrue(_preview.IsModel);
            Assert.IsInstanceOf<RenderTexture>(_preview.PreviewTexture);
            Assert.IsEmpty(_preview.ClipNames);
            AssertMaterialShaders();
            Transform model = FindPreviewModel();
            Assert.That(Vector3.Dot(model.TransformDirection(Vector3.forward), Vector3.up),
                Is.GreaterThan(0.999f), "Source +Z must become Unity up, not upside down.");
            Assert.That(Vector3.Dot(model.TransformDirection(Vector3.up), Vector3.back),
                Is.GreaterThan(0.999f), "Source +Y/front must face the negative-Z preview camera.");
            Renderer[] renderers = model.GetComponentsInChildren<Renderer>();
            Bounds bounds = renderers[0].bounds;
            for (int i = 1; i < renderers.Length; ++i) bounds.Encapsulate(renderers[i].bounds);
            Assert.That(bounds.size.y, Is.GreaterThan(bounds.size.z),
                "The actual TripoSR fixture must be taller than it is deep after correction.");
            _preview.Clear();
            Assert.IsNull(_preview.PreviewTexture);
        }

        [UnityTest]
        public IEnumerator ShowModel_ExistingAnimatedOutput_ExposesPlayableLegacyClips()
        {
            byte[] bytes = ReadFixture("ECHOCRADLE_PREVIEW_ANIMATED_GLB", "step_3_animated.glb");
            Task task = _preview.ShowModelAsync(bytes);
            yield return WaitFor(task);
            AssertCompleted(task);
            Assert.IsNotEmpty(_preview.ClipNames);
            Assert.That(Quaternion.Angle(FindPreviewModel().localRotation, Quaternion.identity),
                Is.LessThan(0.001f), "Already corrected animated glTF must not be rotated again.");
            _preview.PlayClip(_preview.ClipNames[0]);
            yield return null;
            Assert.IsTrue(_preview.IsModel);
            AssertMaterialShaders();
        }

        [UnityTest]
        public IEnumerator ShowModel_ClearDuringImport_DoesNotPublishStaleResult()
        {
            byte[] bytes = ReadFixture("ECHOCRADLE_PREVIEW_STATIC_GLB", "step_2_mesh.glb");
            using (var cancellation = new CancellationTokenSource())
            {
                Task task = _preview.ShowModelAsync(bytes, cancellation.Token);
                cancellation.Cancel();
                _preview.Clear();
                yield return WaitFor(task);
                Assert.IsTrue(task.IsCanceled);
                Assert.IsFalse(_preview.IsModel);
                Assert.IsNull(_preview.PreviewTexture);
            }
        }

        [UnityTest]
        public IEnumerator ShowModel_TwoRequestsThenClear_DrainsBothSafely()
        {
            byte[] bytes = ReadFixture("ECHOCRADLE_PREVIEW_STATIC_GLB", "step_2_mesh.glb");
            Task first = _preview.ShowModelAsync(bytes);
            Task second = _preview.ShowModelAsync(bytes);
            _preview.Clear();
            yield return WaitFor(first);
            yield return WaitFor(second);
            Assert.IsTrue(first.IsCanceled);
            Assert.IsTrue(second.IsCanceled);
            Assert.IsNull(_preview.PreviewTexture);
        }

        private static IEnumerator WaitFor(Task task)
        {
            float deadline = Time.realtimeSinceStartup + 90f;
            while (!task.IsCompleted && Time.realtimeSinceStartup < deadline) yield return null;
            Assert.IsTrue(task.IsCompleted, "Preview operation did not settle within 90 seconds.");
        }

        private static void AssertCompleted(Task task)
        {
            Assert.IsFalse(task.IsCanceled);
            if (task.IsFaulted) Assert.Fail(task.Exception.ToString());
        }

        private static byte[] ReadFixture(string variable, string defaultFile)
        {
#if UNITY_EDITOR
            if (Environment.GetEnvironmentVariable("ECHOCRADLE_PREVIEW_FIXTURE_TESTS") != "1")
                Assert.Ignore("Opt-in: set ECHOCRADLE_PREVIEW_FIXTURE_TESTS=1 before starting Unity.");
            string file = Environment.GetEnvironmentVariable(variable);
            if (string.IsNullOrEmpty(file))
            {
                string root = Path.GetFullPath(Path.Combine(Application.dataPath, "..", ".."));
                file = Path.Combine(root, "experiments", "three-d-generation",
                    "0110-text-to-animated-character", "results",
                    "orchestrator-smoke-2026-10-24_14-40-11", defaultFile);
            }
            Assert.IsTrue(File.Exists(file), "Missing existing fixture: " + file);
            return File.ReadAllBytes(file);
#else
            Assert.Ignore("Local generated-output filesystem fixtures are Editor-only.");
            return null;
#endif
        }

        private static Transform FindPreviewModel()
        {
            foreach (Renderer renderer in Resources.FindObjectsOfTypeAll<Renderer>())
            {
                Transform root = renderer.transform.root;
                if (root.name != "Character preview rig") continue;
                Transform model = root.Find("Imported character");
                if (model != null && model.gameObject.activeInHierarchy) return model;
            }
            Assert.Fail("No active imported preview model found.");
            return null;
        }

        private static void AssertMaterialShaders()
        {
            // The runtime rig deliberately uses HideAndDontSave, which normal
            // scene-wide FindObjectsByType excludes.
            Renderer[] renderers = Resources.FindObjectsOfTypeAll<Renderer>();
            int found = 0;
            foreach (Renderer renderer in renderers)
            {
                if (renderer.transform.root.name != "Character preview rig") continue;
                foreach (Material material in renderer.sharedMaterials)
                {
                    Assert.IsNotNull(material);
                    Assert.IsNotNull(material.shader);
                    Assert.IsTrue(material.shader.isSupported, material.shader.name);
                    Assert.IsTrue(material.shader.name.StartsWith("glTF/", StringComparison.Ordinal));
                    ++found;
                }
                Assert.IsFalse(renderer.enabled, "Preview renderers must be hidden from game cameras.");
            }
            Assert.Greater(found, 0);
        }
    }
}