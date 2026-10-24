using System.Collections.Generic;
using Newtonsoft.Json.Linq;

namespace EchoCradle.Orchestration
{
    /// <summary>Canonical server facts. HostPending may remain true after logical cancellation.</summary>
    public sealed class OrchestratorJob
    {
        public string JobId { get; }
        public string SessionId { get; }
        public string TurnId { get; }
        public string Kind { get; }
        public string State { get; }
        public int Revision { get; }
        public bool HostPending { get; }
        public bool IsTerminal => State == "succeeded" || State == "failed" || State == "cancelled";
        public string Text { get; }
        public string PartialText { get; }
        public IReadOnlyList<OrchestratorToolCall> Calls { get; }
        public IReadOnlyList<string> LocallyExecutedDispatchIds { get; }
        public OrchestratorException Error { get; }
        public JToken SelectedModel => _selectedModel.DeepClone();
        private readonly JToken _selectedModel;

        internal OrchestratorJob(JObject data, ISet<string> executed = null, string partialText = "")
        {
            PartialText = partialText;
            LocallyExecutedDispatchIds = new List<string>(executed ?? new HashSet<string>()).AsReadOnly();
            OrchestratorWire.Integer(data["v"], 1, 1);
            OrchestratorWire.Integer(data["protocol_version"], 1, 1);
            OrchestratorWire.Id(data["service_instance_id"]);
            JobId = OrchestratorWire.Id(data["job_id"]);
            SessionId = OrchestratorWire.NullableId(data["session_id"]);
            TurnId = OrchestratorWire.NullableId(data["turn_id"]);
            Kind = OrchestratorWire.Text(data["kind"], 32);
            State = OrchestratorWire.Text(data["state"], 32);
            if (Kind != "text" && Kind != "tts" && Kind != "stt" &&
                Kind != "image" && Kind != "image_to_3d" && Kind != "animate_3d" ||
                !IsTerminal && State != "queued" && State != "running" && State != "waiting_tool")
                throw new OrchestratorException("PROTOCOL_ERROR");
            Revision = OrchestratorWire.Integer(data["revision"], 1, int.MaxValue);
            HostPending = OrchestratorWire.Boolean(data["host_pending"]);
            _selectedModel = data["selected_model"]?.DeepClone() ?? throw new OrchestratorException("PROTOCOL_ERROR");
            if (_selectedModel.Type != JTokenType.Null && !(_selectedModel is JObject))
                throw new OrchestratorException("PROTOCOL_ERROR");
            if (data["error"] == null) throw new OrchestratorException("PROTOCOL_ERROR");
            if (data["error"].Type != JTokenType.Null)
                Error = OrchestratorWire.Error(data["error"] as JObject, null, "inference", JobId);
            var calls = new List<OrchestratorToolCall>();
            Text = "";
            if (data["result"] == null) throw new OrchestratorException("PROTOCOL_ERROR");
            if (data["result"].Type != JTokenType.Null)
            {
                if (!(data["result"] is JObject result)) throw new OrchestratorException("PROTOCOL_ERROR");
                if (Kind == "text")
                {
                    Text = OrchestratorWire.Text(result["text"]);
                    if (!(result["calls"] is JArray attempts) || attempts.Count > 9)
                        throw new OrchestratorException("PROTOCOL_ERROR");
                    var dispatches = new HashSet<string>();
                    foreach (JToken attempt in attempts)
                    {
                        var call = new OrchestratorToolCall(attempt as JObject, executed);
                        if (call.DispatchId != null && !dispatches.Add(call.DispatchId))
                            throw new OrchestratorException("PROTOCOL_ERROR");
                        calls.Add(call);
                    }
                }
            }
            else if (State == "succeeded") throw new OrchestratorException("PROTOCOL_ERROR");
            Calls = calls.AsReadOnly();
        }
    }
}