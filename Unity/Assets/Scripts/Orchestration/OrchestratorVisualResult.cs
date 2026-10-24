namespace EchoCradle.Orchestration
{
    /// <summary>Verified visual bytes. Retained server artifacts require explicit release (or expire by TTL).</summary>
    public sealed class OrchestratorVisualResult
    {
        private readonly byte[] _payload;
        public byte[] Payload => (byte[])_payload.Clone();
        public string ArtifactId { get; }
        public string MediaType { get; }
        public string Sha256 { get; }
        public uint Seed { get; }
        public int ByteLength => _payload.Length;

        internal OrchestratorVisualResult(byte[] payload, string artifactId, string mediaType, string sha256, uint seed)
        {
            _payload = (byte[])payload.Clone();
            ArtifactId = artifactId;
            MediaType = mediaType;
            Sha256 = sha256;
            Seed = seed;
        }
    }
}