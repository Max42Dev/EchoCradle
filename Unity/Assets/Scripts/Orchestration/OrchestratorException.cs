using System;

namespace EchoCradle.Orchestration
{
    /// <summary>Validated metadata only; remote prose and transport exceptions are never exposed.</summary>
    public sealed class OrchestratorException : Exception
    {
        public string Code { get; }
        public int? HttpStatus { get; }
        public bool Retryable { get; }
        public string JobId { get; }
        public string Phase { get; }
        public OrchestratorJob Snapshot { get; }

        internal OrchestratorException(string code, int? httpStatus = null, bool retryable = false,
            string jobId = null, string phase = null, OrchestratorJob snapshot = null) : base("Local orchestrator: " + code)
        {
            Code = code;
            HttpStatus = httpStatus;
            Retryable = retryable;
            JobId = jobId;
            Phase = phase;
            Snapshot = snapshot;
        }
    }
}