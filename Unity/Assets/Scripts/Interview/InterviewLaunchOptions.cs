using System;
using System.IO;
using Newtonsoft.Json.Linq;

namespace EchoCradle.Interview
{
    /// <summary>Project-relative developer defaults; explicit options work in standalone builds.</summary>
    public sealed class InterviewLaunchOptions
    {
        public bool TextOnly { get; }
        public string Python { get; }
        public string PackageRoot { get; }
        public string VisualConfig { get; }
        public bool Smoke { get; }
        public string SmokeTranscript { get; }
        public string SmokeOutput { get; }

        public InterviewLaunchOptions(JObject config, string projectRoot, string[] arguments)
        {
            TextOnly = Array.IndexOf(arguments, "--text-only") >= 0 || (bool?)config["textOnly"] == true;
            Smoke = Array.IndexOf(arguments, "--character-smoke") >= 0;
            if (Smoke && !TextOnly) throw new ArgumentException("Character smoke requires --text-only.");
            Python = Resolve(projectRoot, Option(arguments, "--orchestrator-python") ?? (string)config["service"]["pythonExecutable"]);
            PackageRoot = Resolve(projectRoot, Option(arguments, "--orchestrator-root") ?? (string)config["service"]["packageRoot"]);
            string visual = Option(arguments, "--visual-config") ?? (string)config["service"]["visualConfig"];
            VisualConfig = string.IsNullOrWhiteSpace(visual) ? null : Resolve(projectRoot, visual);
            if (VisualConfig == null)
            {
                string candidate = Resolve(projectRoot,
                    "../experiments/three-d-generation/0110-text-to-animated-character/automatic.local.json");
                if (File.Exists(candidate)) VisualConfig = candidate;
            }
            SmokeTranscript = Option(arguments, "--character-smoke-transcript");
            if (SmokeTranscript != null) SmokeTranscript = Resolve(projectRoot, SmokeTranscript);
            SmokeOutput = Option(arguments, "--character-smoke-output");
            if (SmokeOutput != null) SmokeOutput = Resolve(projectRoot, SmokeOutput);
        }

        public static string Resolve(string root, string value) =>
            Path.GetFullPath(Path.Combine(root, Environment.ExpandEnvironmentVariables(value)));

        private static string Option(string[] arguments, string name)
        {
            int index = Array.IndexOf(arguments, name);
            if (index < 0) return null;
            if (index + 1 == arguments.Length || arguments[index + 1].StartsWith("--", StringComparison.Ordinal))
                throw new ArgumentException("Missing value for " + name);
            return arguments[index + 1];
        }
    }
}