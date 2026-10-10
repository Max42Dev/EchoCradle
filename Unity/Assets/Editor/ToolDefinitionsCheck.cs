using System;
using System.IO;
using System.Security.Cryptography;
using System.Text;
using UnityEditor;
using UnityEditor.Build;
using UnityEditor.Build.Reporting;

namespace EchoCradle.Editor
{
    /// <summary>Fail play/build early if source-defined tool bindings were not regenerated.</summary>
    [InitializeOnLoad]
    public sealed class ToolDefinitionsCheck : IPreprocessBuildWithReport
    {
        public int callbackOrder => 0;

        static ToolDefinitionsCheck()
        {
            EditorApplication.playModeStateChanged += state =>
            {
                if (state != PlayModeStateChange.ExitingEditMode) return;
                try { Validate(); }
                catch (BuildFailedException error)
                {
                    EditorApplication.isPlaying = false;
                    UnityEngine.Debug.LogError(error.Message);
                }
            };
        }

        public void OnPreprocessBuild(BuildReport report) => Validate();

        [MenuItem("EchoCradle/Validate generated MCP tools")]
        public static void Validate()
        {
            const string source = "Assets/Scripts/Interview/InterviewState.cs";
            const string generated = "Assets/Scripts/Interview/InterviewToolBindings.g.cs";
            string hash;
            using (SHA256 sha = SHA256.Create())
                hash = BitConverter.ToString(sha.ComputeHash(Encoding.UTF8.GetBytes(
                    File.ReadAllText(source).Replace("\r\n", "\n")))).Replace("-", "");
            if (!File.Exists(generated) || !File.ReadAllText(generated).Contains("// Input SHA256: " + hash))
                throw new BuildFailedException("MCP tool bindings are stale. From the repository root run " +
                    "dotnet run --project tools/ToolDefinitions/ToolDefinitions.csproj, then reimport.");
        }
    }
}