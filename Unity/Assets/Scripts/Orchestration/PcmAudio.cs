namespace EchoCradle.Orchestration
{
    // Block namespaces intentionally keep this transport compatible with Unity C# 9.
    /// <summary>PCM16 little-endian mono samples. No Unity objects or device ownership.</summary>
    public sealed class PcmAudio
    {
        public byte[] Samples { get; }
        public int SampleRate { get; }

        public PcmAudio(byte[] samples, int sampleRate)
        {
            if (samples == null || samples.Length > 960000 || samples.Length % 2 != 0 ||
                sampleRate < 1 || sampleRate > 192000)
                throw new OrchestratorException("INVALID_PCM");
            Samples = samples;
            SampleRate = sampleRate;
        }
    }
}