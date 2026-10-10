using System;
using EchoCradle.Orchestration;
using Newtonsoft.Json.Linq;
using UnityEngine;

namespace EchoCradle.Interview
{
    [CreateAssetMenu(menuName = "EchoCradle/Interview settings")]
    public sealed class InterviewSettings : ScriptableObject
    {
        [SerializeField, Tooltip("Prompts, schema, UI strings and local runtime settings.")]
        private TextAsset configuration;

        public JObject Read()
        {
            if (configuration == null) throw new InvalidOperationException("Missing interview configuration.");
            JObject data = OrchestratorWire.Decode(System.Text.Encoding.UTF8.GetBytes(configuration.text));
            // Reject unsafe timing/buffer values before starting devices or model jobs.
            JObject mic = (JObject)data["microphone"];
            Require((int)mic["sampleRate"] == 16000 && (int)mic["blockSamples"] == 320);
            JObject vad = (JObject)data["vad"];
            Require((float)vad["threshold"] > 0 && (float)vad["threshold"] < 1);
            Require((float)vad["targetRms"] > 0 && (float)vad["peakLimit"] > 0 && (float)vad["peakLimit"] <= 1);
            Require((float)vad["maxGain"] >= 1 && (float)vad["maxGain"] <= 100);
            Require((float)vad["gainTimeSeconds"] > 0);
            Require((float)data["completion"]["quietSeconds"] >= 0.5f && (float)data["completion"]["quietSeconds"] <= 3);
            Require((float)mic["onsetSeconds"] >= 0.04f && (float)mic["onsetSeconds"] <= 1);
            Require((float)mic["silenceSeconds"] >= 0.2f && (float)mic["silenceSeconds"] <= 3);
            Require((float)mic["preRollSeconds"] >= 0 && (float)mic["preRollSeconds"] <= 1);
            Require((float)mic["maxUtteranceSeconds"] >= 2 && (float)mic["maxUtteranceSeconds"] <= 30);
            Require((int)mic["idleTimeoutSeconds"] >= 10 && (int)mic["idleTimeoutSeconds"] <= 600);
            Require((int)data["speech"]["chunkCharacters"] >= 40 && (int)data["speech"]["chunkCharacters"] <= 300);
            Require((int)data["speech"]["maxCharacters"] >= 300 && (int)data["speech"]["maxCharacters"] <= 2400);
            Require((int)data["generation"]["historyPairs"] >= 1 && (int)data["generation"]["historyPairs"] <= 12);
            Require((int)data["generation"]["maxTurns"] >= 1 && (int)data["generation"]["maxTurns"] <= 100);
            Require((int)data["generation"]["maxTokens"] >= 1 && (int)data["generation"]["maxTokens"] <= 512);
            Require((int)data["generation"]["deadlineMs"] >= 1000 && (int)data["generation"]["deadlineMs"] <= 120000);
            float temperature = (float)data["generation"]["temperature"];
            Require(!float.IsNaN(temperature) && temperature >= 0 && temperature <= 2);
            Require(data["tools"] is JArray && data["prompts"] is JObject && data["ui"] is JObject);
            return data;
        }

        private static void Require(bool valid)
        {
            if (!valid) throw new InvalidOperationException("Invalid interview configuration.");
        }
    }
}