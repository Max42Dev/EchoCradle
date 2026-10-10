using System;

namespace EchoCradle.Orchestration
{
    /// <summary>Only locally selected codes are exposed; never remote messages or inner exceptions.</summary>
    public sealed class OrchestratorException : Exception
    {
        public string Code { get; }

        internal OrchestratorException(string code) : base("Local orchestrator: " + code)
        {
            Code = code;
        }
    }
}