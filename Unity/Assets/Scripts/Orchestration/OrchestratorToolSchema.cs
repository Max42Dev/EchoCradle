using System.Collections.Generic;
using Newtonsoft.Json.Linq;

namespace EchoCradle.Orchestration
{
    // Deliberately narrow local authority: object roots with bounded primitive properties.
    // Unsupported JSON-schema keywords fail session creation rather than bypass validation.
    internal static class OrchestratorToolSchema
    {
        internal static void ValidateSchema(JObject schema)
        {
            if ((string)schema["type"] != "object" || !(schema["properties"] is JObject properties) ||
                properties.Count > 32 || schema["additionalProperties"]?.Type != JTokenType.Boolean ||
                (bool)schema["additionalProperties"] || !(schema["required"] is JArray required))
                throw new OrchestratorException("INVALID_TOOLS");
            foreach (JProperty keyword in schema.Properties())
                if (keyword.Name != "type" && keyword.Name != "properties" && keyword.Name != "required" &&
                    keyword.Name != "additionalProperties" && keyword.Name != "description" && keyword.Name != "$schema")
                    throw new OrchestratorException("INVALID_TOOLS");
            var names = new HashSet<string>();
            foreach (JToken name in required)
                if (name.Type != JTokenType.String || properties[(string)name] == null || !names.Add((string)name))
                    throw new OrchestratorException("INVALID_TOOLS");
            foreach (JProperty property in properties.Properties())
            {
                if (!(property.Value is JObject rule)) throw new OrchestratorException("INVALID_TOOLS");
                string type = (string)rule["type"];
                if (type != "string" && type != "boolean" && type != "integer" && type != "number")
                    throw new OrchestratorException("INVALID_TOOLS");
                foreach (JProperty keyword in rule.Properties())
                {
                    switch (keyword.Name)
                    {
                        case "type": case "description": break;
                        case "enum":
                            if (!(keyword.Value is JArray values) || values.Count == 0)
                                throw new OrchestratorException("INVALID_TOOLS");
                            break;
                        case "minLength": case "maxLength":
                            if (type != "string") throw new OrchestratorException("INVALID_TOOLS");
                            OrchestratorWire.Integer(keyword.Value, 0, 16384);
                            break;
                        case "minimum": case "maximum":
                            if (type != "integer" && type != "number" ||
                                keyword.Value.Type != JTokenType.Integer && keyword.Value.Type != JTokenType.Float)
                                throw new OrchestratorException("INVALID_TOOLS");
                            break;
                        default: throw new OrchestratorException("INVALID_TOOLS");
                    }
                }
            }
        }

        internal static void ValidateArguments(JObject schema, JToken arguments)
        {
            if (!(arguments is JObject obj)) throw new OrchestratorException("INVALID_TOOL_ARGUMENTS");
            foreach (JToken name in (JArray)schema["required"])
                if (obj[(string)name] == null) throw new OrchestratorException("INVALID_TOOL_ARGUMENTS");
            foreach (JProperty property in obj.Properties())
            {
                JObject rule = schema["properties"][property.Name] as JObject;
                if (rule == null) throw new OrchestratorException("INVALID_TOOL_ARGUMENTS");
                JToken value = property.Value;
                string type = (string)rule["type"];
                bool valid = type == "string" ? value.Type == JTokenType.String :
                    type == "boolean" ? value.Type == JTokenType.Boolean : type == "integer" ?
                    value.Type == JTokenType.Integer : value.Type == JTokenType.Integer || value.Type == JTokenType.Float;
                if (!valid) throw new OrchestratorException("INVALID_TOOL_ARGUMENTS");
                if (type == "string" && (((string)value).Length < ((int?)rule["minLength"] ?? 0) ||
                    ((string)value).Length > ((int?)rule["maxLength"] ?? 16384)))
                    throw new OrchestratorException("INVALID_TOOL_ARGUMENTS");
                if ((type == "number" || type == "integer") &&
                    (rule["minimum"] != null && (double)value < (double)rule["minimum"] ||
                     rule["maximum"] != null && (double)value > (double)rule["maximum"]))
                    throw new OrchestratorException("INVALID_TOOL_ARGUMENTS");
                if (rule["enum"] is JArray values)
                {
                    bool found = false;
                    foreach (JToken option in values) if (JToken.DeepEquals(option, value)) found = true;
                    if (!found) throw new OrchestratorException("INVALID_TOOL_ARGUMENTS");
                }
            }
        }
    }
}