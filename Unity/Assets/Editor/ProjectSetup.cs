using System.IO;
using EchoCradle.Interview;
using UnityEditor;
using UnityEditor.SceneManagement;
using UnityEngine;
using UnityEngine.SceneManagement;

namespace EchoCradle.Editor
{
    [InitializeOnLoad]
    public static class ProjectSetup
    {
        static ProjectSetup() { EditorApplication.delayCall += EnsureProject; }

        public static void BuildWindows()
        {
            EnsureProject();
            Directory.CreateDirectory("Builds/Windows");
            UnityEditor.Build.Reporting.BuildReport report = BuildPipeline.BuildPlayer(new BuildPlayerOptions
            {
                scenes = new[] { "Assets/Scenes/Start.unity" },
                locationPathName = "Builds/Windows/EchoCradle.exe",
                target = BuildTarget.StandaloneWindows64,
                options = BuildOptions.Development
            });
            if (report.summary.result != UnityEditor.Build.Reporting.BuildResult.Succeeded)
                throw new System.InvalidOperationException("Windows build failed.");
        }

        public static void EnsureProject()
        {
            const string scenePath = "Assets/Scenes/Start.unity";
            if (File.Exists(scenePath)) return;
            Directory.CreateDirectory("Assets/Scenes");
            var settings = ScriptableObject.CreateInstance<InterviewSettings>();
            var serializedSettings = new SerializedObject(settings);
            serializedSettings.FindProperty("configuration").objectReferenceValue =
                AssetDatabase.LoadAssetAtPath<TextAsset>("Assets/Data/Interview.json");
            serializedSettings.ApplyModifiedPropertiesWithoutUndo();
            AssetDatabase.CreateAsset(settings, "Assets/Data/InterviewSettings.asset");
            AssetDatabase.SaveAssets();
            settings = AssetDatabase.LoadAssetAtPath<InterviewSettings>("Assets/Data/InterviewSettings.asset");
            Scene scene = EditorSceneManager.NewScene(NewSceneSetup.EmptyScene, NewSceneMode.Single);
            var cameraObject = new GameObject("Camera");
            Camera camera = cameraObject.AddComponent<Camera>();
            camera.clearFlags = CameraClearFlags.SolidColor;
            camera.backgroundColor = Color.black;
            cameraObject.AddComponent<AudioListener>();
            var screen = new GameObject("Start screen");
            screen.AddComponent<AudioSource>();
            InterviewController controller = screen.AddComponent<InterviewController>();
            var serializedController = new SerializedObject(controller);
            serializedController.FindProperty("settings").objectReferenceValue = settings;
            serializedController.ApplyModifiedPropertiesWithoutUndo();
            EditorSceneManager.SaveScene(scene, scenePath);
            EditorBuildSettings.scenes = new[] { new EditorBuildSettingsScene(scenePath, true) };
            PlayerSettings.companyName = "EchoCradle";
            PlayerSettings.productName = "EchoCradle";
            PlayerSettings.defaultScreenWidth = 1280;
            PlayerSettings.defaultScreenHeight = 720;
            PlayerSettings.fullScreenMode = FullScreenMode.Windowed;
            AssetDatabase.SaveAssets();
        }
    }
}