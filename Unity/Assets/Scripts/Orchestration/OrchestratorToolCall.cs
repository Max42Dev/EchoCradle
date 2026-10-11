using System.Collections.Generic;
using Newtonsoft.Json.Linq;

namespace EchoCradle.Orchestration
{
    public sealed class OrchestratorToolCall
    {
        public string ModelCallId { get; }
        public string DispatchId { get; }
        public string Name { get; }
        public string Outcome { get; }
        public bool ResultDelivered { get; }
        public bool LocallyExecuted { get; }
        private readonly JObject _arguments;
        private readonly JToken _result;
        public JObject Arguments => (JObject)_arguments.DeepClone();
        public JToken Result => _result.DeepClone();

        internal OrchestratorToolCall(JObject data, ISet<string> executed)
        {
            if (data == null || data["id"] == null) throw new OrchestratorException("PROTOCOL_ERROR");
            ModelCallId = data["id"].Type == JTokenType.Null ? null : OrchestratorWire.Text(data["id"], 256);
            DispatchId = OrchestratorWire.NullableId(data["dispatch_id"]);
            Name = OrchestratorWire.Text(data["name"], 64);
            Outcome = OrchestratorWire.Text(data["outcome"], 16);
            ResultDelivered = OrchestratorWire.Boolean(data["result_delivered"]);
            if (Outcome != "rejected" && Outcome != "completed" && Outcome != "unknown" ||
                Outcome == "unknown" && DispatchId == null || Outcome == "rejected" && DispatchId != null)
                throw new OrchestratorException("PROTOCOL_ERROR");
            if (!(data["arguments"] is JObject arguments) || data["result"] == null)
                throw new OrchestratorException("PROTOCOL_ERROR");
            OrchestratorWire.Encode(arguments, OrchestratorWire.ToolLimit);
            OrchestratorWire.Encode(data["result"], OrchestratorWire.ToolLimit);
            _arguments = (JObject)arguments.DeepClone();
            _result = data["result"].DeepClone();
            LocallyExecuted = DispatchId != null && executed != null && executed.Contains(DispatchId);
        }
    }
}