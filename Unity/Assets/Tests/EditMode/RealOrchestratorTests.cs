using System;
using System.IO;
using System.Threading;
using System.Threading.Tasks;
using EchoCradle.Interview;
using EchoCradle.Orchestration;
using Newtonsoft.Json.Linq;
using NUnit.Framework;
using UnityEngine;

public sealed class RealOrchestratorTests
{
    [Test, Explicit("Runs the production interview prompts against cached local models."), Timeout(420000)]
    public async Task CachedVoiceService_ProductionInterviewRecordsDirectAnswersAndCorrection()
    {
        JObject config = JObject.Parse(File.ReadAllText(Path.Combine(Application.dataPath, "Data/Interview.json")));
        using (var stop = new CancellationTokenSource(TimeSpan.FromMinutes(6)))
        {
            OwnedOrchestrator owner = await OwnedOrchestrator.StartAsync(
                (string)config["service"]["pythonExecutable"], (string)config["service"]["packageRoot"], stop.Token);
            try
            {
                var state = new InterviewState((JObject)config["schema"]);
                await owner.Client.ResetSessionAsync(state.ToolDeclarations(), state.SessionHandler(), stop.Token);
                var history = new JArray();
                string[] replies = { "My name is Alex.", "A medieval fantasy world with floating islands.",
                    "Your name is Echo. I insist on that name.", "Actually, call me Morgan instead of Alex." };
                string[] fields = { "username", "style", "ai_name", "username" };
                string[] values = { "Alex", "medieval", "Echo", "Morgan" };
                for (int i = 0; i < replies.Length; i++)
                {
                    history.Add(InterviewState.Message("user", replies[i]));
                    state.AcceptTools(true);
                    OrchestratorJob response;
                    try
                    {
                        response = await owner.Client.TextAsync(
                            state.Messages(history, (JObject)config["prompts"], false, false),
                            (int)config["generation"]["maxTokens"], (float)config["generation"]["temperature"],
                            (int)config["generation"]["deadlineMs"], null, stop.Token);
                    }
                    finally { state.AcceptTools(false); }
                    Assert.That(response.State, Is.EqualTo("succeeded"));
                    Assert.That((string)state.Snapshot()[fields[i]], values[i] == "" ? Is.EqualTo("") : Does.Contain(values[i]),
                        "Direct answer was acknowledged without being recorded: " + response.Text);
                    Assert.That(response.Calls.Count, Is.GreaterThan(0));
                    foreach (OrchestratorToolCall call in response.Calls)
                    {
                        Assert.That(call.Outcome, Is.EqualTo("completed"));
                        Assert.That(call.LocallyExecuted, Is.True);
                        Assert.That(call.ResultDelivered, Is.True);
                    }
                    history.Add(InterviewState.Message("assistant", response.Text));
                }
                Assert.That(state.Missing().Count, Is.Zero);
                state.ValidateComplete(state.Snapshot());
            }
            finally { await owner.CloseAsync(); }
        }
    }

    [Test, Explicit("Loads cached local voice models; no downloads. Run separately."), Timeout(300000)]
    public async Task CachedVoiceService_NativeUnityClientRunsToolsTtsAndStt()
    {
        JObject config = JObject.Parse(File.ReadAllText(Path.Combine(Application.dataPath, "Data/Interview.json")));
        string root = Path.GetFullPath(Path.Combine(Application.dataPath, "..", (string)config["service"]["packageRoot"]));
        using (var stop = new CancellationTokenSource(TimeSpan.FromMinutes(4)))
        {
            OwnedOrchestrator owner = await OwnedOrchestrator.StartAsync((string)config["service"]["pythonExecutable"], root, stop.Token);
            try
            {
                int calls = 0;
                var state = new InterviewState((JObject)config["schema"]);
                state.AcceptTools(true);
                var handler = state.SessionHandler();
                await owner.Client.OpenSessionAsync(state.ToolDeclarations(), (name, arguments) =>
                {
                    Interlocked.Increment(ref calls);
                    return handler(name, arguments);
                }, stop.Token);
                OrchestratorJob response = await owner.Client.TextAsync(new JArray
                {
                    new JObject { ["role"] = "system", ["content"] = "Use config_set to record the player's name, then say a short greeting." },
                    new JObject { ["role"] = "user", ["content"] = "My name is Ada." }
                }, 128, 0.2f, 120000, null, stop.Token);
                Assert.That(response.State, Is.EqualTo("succeeded"));
                Assert.That(response.Text, Is.Not.Empty);
                Assert.That(calls, Is.GreaterThan(0), "Expected a real native tool round trip.");
                Assert.That((string)state.Snapshot()["username"], Is.EqualTo("Ada"));
                await owner.Client.ResetSessionAsync(new JArray(), null, stop.Token);
                PcmAudio audio = await owner.Client.SynthesizeAsync("The service is ready.", stop.Token);
                Assert.That(audio.Samples.Length, Is.GreaterThan(0));
                // STT's wire contract is fixed 16 kHz; simple deterministic linear resampling.
                int sourceCount = audio.Samples.Length / 2;
                int count = sourceCount * 16000 / audio.SampleRate;
                var pcm = new byte[count * 2];
                for (int i = 0; i < count; i++)
                {
                    double source = i * (double)audio.SampleRate / 16000;
                    int first = Math.Min((int)source, sourceCount - 1);
                    int next = Math.Min(first + 1, sourceCount - 1);
                    short a = (short)(audio.Samples[first * 2] | audio.Samples[first * 2 + 1] << 8);
                    short b = (short)(audio.Samples[next * 2] | audio.Samples[next * 2 + 1] << 8);
                    short value = (short)(a + (b - a) * (source - first));
                    pcm[i * 2] = (byte)value;
                    pcm[i * 2 + 1] = (byte)(value >> 8);
                }
                string transcript = await owner.Client.TranscribeAsync(pcm, stop.Token);
                Assert.That(transcript.ToLowerInvariant(), Does.Contain("ready"));
            }
            finally { await owner.CloseAsync(); }
        }
    }
}