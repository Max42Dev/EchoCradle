using Newtonsoft.Json.Linq;

namespace EchoCradle.Interview;

// Metadata discovery never calls the player dispatcher. Keep generation independent
// of the previous generated artifact so renaming a tool method cannot break regeneration.
internal static class InterviewToolBindings
{
    internal static JArray Declarations() => throw new NotSupportedException("Metadata-only build.");
    internal static JObject Invoke(InterviewState target, string name, JObject arguments) =>
        throw new NotSupportedException("Metadata-only build.");
}