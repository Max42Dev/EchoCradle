using System;
using System.Collections.Generic;
using Newtonsoft.Json.Linq;

namespace EchoCradle.Interview
{
    /// <summary>Thread-safe, bounded domain authority for the single configured tool.</summary>
    public sealed class InterviewState
    {
        private readonly object _sync = new object();
        private readonly JObject _schema;
        private readonly JObject _values = new JObject();
        private bool _accepting;
        private int _generation;

        public InterviewState(JObject schema)
        {
            _schema = (JObject)schema.DeepClone();
            if ((string)_schema["type"] != "object" || (bool?)_schema["additionalProperties"] != false ||
                !(_schema["required"] is JArray) || !(_schema["properties"] is JObject))
                throw new ArgumentException("Unsupported interview schema.");
            foreach (JProperty field in ((JObject)_schema["properties"]).Properties())
            {
                if ((string)field.Value["type"] != "string" || (int?)field.Value["maxLength"] == null ||
                    (int)field.Value["maxLength"] < 1 || (int)field.Value["maxLength"] > 2000)
                    throw new ArgumentException("Unsupported field schema.");
                foreach (JProperty rule in ((JObject)field.Value).Properties())
                    if (rule.Name != "type" && rule.Name != "minLength" && rule.Name != "maxLength")
                        throw new ArgumentException("Unsupported field rule.");
            }
            foreach (JToken field in (JArray)_schema["required"])
                if (field.Type != JTokenType.String || _schema["properties"][(string)field] == null)
                    throw new ArgumentException("Unknown required field.");
        }

        public void AcceptTools(bool accepting) { lock (_sync) _accepting = accepting; }
        public Func<string, JObject, JObject> SessionHandler()
        {
            int generation;
            lock (_sync) generation = ++_generation;
            return (name, arguments) =>
            {
                lock (_sync)
                    return generation == _generation ? Invoke(name, arguments) :
                        new JObject { ["error"] = "stale_session" };
            };
        }
        public JObject Snapshot() { lock (_sync) return (JObject)_values.DeepClone(); }

        public JArray Missing()
        {
            lock (_sync)
            {
                var missing = new JArray();
                foreach (JToken field in (JArray)_schema["required"])
                    if (string.IsNullOrWhiteSpace((string)_values[(string)field])) missing.Add(field.DeepClone());
                return missing;
            }
        }

        public JObject Invoke(string name, JObject arguments)
        {
            lock (_sync)
            {
                if (!_accepting) return new JObject { ["error"] = "turn_cancelled" };
                if (name != "config_set" || arguments.Count != 2 || arguments["field"]?.Type != JTokenType.String ||
                    arguments["value"]?.Type != JTokenType.String)
                    return new JObject { ["error"] = "invalid_arguments" };
                string field = (string)arguments["field"];
                string value = ((string)arguments["value"]).Trim();
                if (!Valid(field, value)) return new JObject { ["error"] = "invalid_field_value" };
                _values[field] = value;
                return new JObject { ["recorded"] = Snapshot(), ["missing"] = Missing() };
            }
        }

        private bool Valid(string field, string value)
        {
            JToken rule = _schema["properties"][field];
            if (rule == null || value.Length < ((int?)rule["minLength"] ?? 0) || value.Length > (int)rule["maxLength"])
                return false;
            foreach (char character in value) if (char.IsControl(character)) return false;
            return true;
        }

        public void ValidateComplete(JObject values)
        {
            foreach (JToken field in (JArray)_schema["required"])
                if (values[(string)field]?.Type != JTokenType.String || string.IsNullOrWhiteSpace((string)values[(string)field]))
                    throw new InvalidOperationException("Incomplete interview.");
            foreach (JProperty field in values.Properties())
                if (field.Value.Type != JTokenType.String || !Valid(field.Name, (string)field.Value))
                    throw new InvalidOperationException("Invalid interview value.");
        }

        public JArray Messages(JArray history, JObject prompts, bool interrupted, bool stalled)
        {
            JArray missing = Missing();
            string question = missing.Count == 0 ? (string)prompts["closingQuestion"] :
                (string)prompts["questions"][(string)missing[0]];
            string context = ((string)prompts["state"]).Replace("{recorded}", Snapshot().ToString(Newtonsoft.Json.Formatting.None))
                .Replace("{missing}", missing.ToString(Newtonsoft.Json.Formatting.None)).Replace("{question}", question);
            var messages = new JArray { Message("system", (string)prompts["system"]) };
            foreach (JToken turn in history) messages.Add(turn.DeepClone());
            if (history.Count == 0) messages.Add(Message("system", (string)prompts["opening"]));
            messages.Add(Message("system", context));
            if (interrupted) messages.Add(Message("system", (string)prompts["interrupted"]));
            if (stalled) messages.Add(Message("system", (string)prompts["stalled"]));
            return messages;
        }

        public static JObject Message(string role, string text) => new JObject { ["role"] = role, ["content"] = text };
    }
}