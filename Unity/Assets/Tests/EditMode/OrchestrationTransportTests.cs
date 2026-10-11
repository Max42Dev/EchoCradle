using System;
using System.Collections.Generic;
using System.IO;
using System.Net;
using System.Net.WebSockets;
using System.Reflection;
using System.Security.Cryptography;
using System.Text;
using System.Threading;
using System.Threading.Tasks;
using EchoCradle.Interview;
using EchoCradle.Orchestration;
using Newtonsoft.Json.Linq;
using NUnit.Framework;

public sealed class OrchestrationTransportTests
{
    private static object Wire(string name, params object[] args)
    {
        Type type = typeof(OrchestratorClient).Assembly.GetType("EchoCradle.Orchestration.OrchestratorWire");
        try { return type.GetMethod(name, BindingFlags.NonPublic | BindingFlags.Static).Invoke(null, args); }
        catch (TargetInvocationException error) { throw error.InnerException; }
    }

    [TestCase("http://127.0.0.1:5010")]
    [TestCase("https://[::1]:5010/")]
    [TestCase("http://localhost:5010")]
    public void Origin_Loopback_IsAccepted(string url)
    {
        Assert.That(((Uri)Wire("Origin", url)).IsLoopback, Is.True);
    }

    [TestCase("http://example.com:5010")]
    [TestCase("http://127.0.0.1.evil.test:5010")]
    [TestCase("http://user:secret@127.0.0.1:5010")]
    [TestCase("http://127.0.0.1:5010/?token=secret")]
    [TestCase("http://127.0.0.1:5010/#secret")]
    [TestCase("http://127.0.0.1:5010/v1")]
    [TestCase("file:///localhost/")]
    public void Origin_RemoteOrCredentialBearing_IsRejected(string url)
    {
        Assert.Throws<OrchestratorException>(() => Wire("Origin", url));
    }

    [TestCase("{\"a\":1,\"a\":2}")]
    [TestCase("{\"a\":NaN}")]
    [TestCase("{\"a\":Infinity}")]
    [TestCase("{\"a\":1e999}")]
    [TestCase("{'a':1}")]
    [TestCase("{a:1}")]
    [TestCase("{\"a\":1,}")]
    [TestCase("{\"a\":[1,]}")]
    [TestCase("{\"a\":01}")]
    [TestCase("{\"a\":0x10}")]
    [TestCase("{/*comment*/\"a\":1}")]
    [TestCase("{} {}")]
    [TestCase("[]")]
    public void Json_InvalidSyntax_IsRejected(string json)
    {
        Assert.Throws<OrchestratorException>(() => Wire("Decode", Encoding.UTF8.GetBytes(json)));
    }

    [Test]
    public void Json_UnicodeFiniteNumbersAndDates_RoundTrip()
    {
        var value = new JObject { ["text"] = "hello 世界", ["date"] = "2026-10-07", ["n"] = 1.25e2 };
        byte[] encoded = (byte[])Wire("Encode", value, 65536);
        Assert.That(JToken.DeepEquals(value, (JObject)Wire("Decode", encoded)), Is.True);
    }

    [Test]
    public void Json_DepthUtf8AndByteCaps_AreEnforced()
    {
        string deep = new string('[', 17) + "0" + new string(']', 17);
        Assert.Throws<OrchestratorException>(() => Wire("Decode", Encoding.UTF8.GetBytes("{\"x\":" + deep + "}")));
        Assert.Throws<OrchestratorException>(() => Wire("Decode", new byte[] { 0xff }));
        Assert.Throws<OrchestratorException>(() => Wire("Decode", new byte[65537]));
        Assert.Throws<OrchestratorException>(() => Wire("Encode", new JObject { ["x"] = new string('世', 22000) }, 65536));
    }

    [Test]
    public void Pcm_ParityRateAndCap_AreEnforced()
    {
        Assert.Throws<OrchestratorException>(() => new PcmAudio(new byte[1], 16000));
        Assert.Throws<OrchestratorException>(() => new PcmAudio(new byte[960002], 16000));
        Assert.Throws<OrchestratorException>(() => new PcmAudio(new byte[2], 0));
        Assert.That(new PcmAudio(new byte[960000], 16000).Samples.Length, Is.EqualTo(960000));
    }

    [Test]
    public async Task Connect_ErrorBody_NeverExposesToken()
    {
        using (var service = new FakeService())
        {
            service.Reject = true;
            try
            {
                await OrchestratorClient.ConnectAsync(service.Url, "private-test-token", CancellationToken.None);
                Assert.Fail("Expected safe rejection.");
            }
            catch (OrchestratorException error)
            {
                Assert.That(error.ToString(), Does.Not.Contain("private-test-token"));
                Assert.That(error.InnerException, Is.Null);
            }
        }
    }

    [Test]
    public async Task Tts_VerifiedArtifact_IsReturnedAndDeleted()
    {
        using (var service = new FakeService())
        {
            OrchestratorClient client = await OrchestratorClient.ConnectAsync(service.Url, "test-token");
            try
            {
                PcmAudio audio = await client.SynthesizeAsync("hello");
                Assert.That(audio.SampleRate, Is.EqualTo(24000));
                Assert.That(audio.Samples, Is.EqualTo(service.Pcm));
                Assert.That(service.Deletes, Is.EqualTo(1));
                Assert.That(service.BadAuthorization, Is.False);
            }
            finally { await client.CloseAsync(); }
            Assert.That(service.Shutdowns, Is.Zero, "Attached transport must not shut down its server.");
        }
    }

    [Test]
    public async Task Tts_BadDigest_IsRejectedAndArtifactDeleted()
    {
        using (var service = new FakeService())
        {
            service.BadDigest = true;
            OrchestratorClient client = await OrchestratorClient.ConnectAsync(service.Url, "test-token");
            try
            {
                try { await client.SynthesizeAsync("hello"); Assert.Fail("Expected integrity rejection."); }
                catch (OrchestratorException error) { Assert.That(error.Code, Is.EqualTo("ARTIFACT_INVALID")); }
                Assert.That(service.Deletes, Is.EqualTo(1));
            }
            finally { await client.CloseAsync(); }
        }
    }

    [Test]
    public async Task Stt_UploadAndConsumedInput_UsesExactContract()
    {
        using (var service = new FakeService())
        {
            OrchestratorClient client = await OrchestratorClient.ConnectAsync(service.Url, "test-token");
            try
            {
                Assert.That(await client.TranscribeAsync(new byte[320]), Is.EqualTo("transcribed"));
                Assert.That(service.UploadValid, Is.True);
                Assert.That(service.Deletes, Is.Zero, "Admitted STT consumes its input.");
            }
            finally { await client.CloseAsync(); }
        }
    }

    [Test]
    public async Task Stt_RejectedSubmission_DeletesUploadedInput()
    {
        using (var service = new FakeService())
        {
            service.RejectJobs = true;
            OrchestratorClient client = await OrchestratorClient.ConnectAsync(service.Url, "test-token");
            try
            {
                try { await client.TranscribeAsync(new byte[320]); Assert.Fail("Expected rejection."); }
                catch (OrchestratorException) { }
                Assert.That(service.Deletes, Is.EqualTo(1));
            }
            finally { await client.CloseAsync(); }
        }
    }

    [Test]
    public async Task Tts_DeleteFailure_DoesNotDiscardVerifiedAudio()
    {
        using (var service = new FakeService { FailDelete = true })
        using (OrchestratorClient client = await OrchestratorClient.ConnectAsync(service.Url, "test-token"))
        {
            Assert.That((await client.SynthesizeAsync("hello")).Samples, Is.EqualTo(service.Pcm));
            Assert.That(service.Deletes, Is.EqualTo(1));
        }
    }

    [Test]
    public async Task Stt_AmbiguousAdmission_DoesNotDeleteUpload()
    {
        using (var service = new FakeService { AmbiguousAdmission = true })
        using (OrchestratorClient client = await OrchestratorClient.ConnectAsync(service.Url, "test-token"))
        {
            try { await client.TranscribeAsync(new byte[320]); Assert.Fail("Expected failure."); }
            catch (OrchestratorException error) { Assert.That(error.HttpStatus, Is.EqualTo(500)); }
            Assert.That(service.Deletes, Is.Zero);
        }
    }

    [Test]
    public async Task Admission_ErrorMetadata_IsSafeAndMachineReadable()
    {
        using (var service = new FakeService { RejectJobs = true })
        using (OrchestratorClient client = await OrchestratorClient.ConnectAsync(service.Url, "test-token"))
        {
            try { await client.TranscribeAsync(new byte[320]); Assert.Fail("Expected failure."); }
            catch (OrchestratorException error)
            {
                Assert.That(error.Code, Is.EqualTo("RESOURCE_BUSY"));
                Assert.That(error.HttpStatus, Is.EqualTo(429));
                Assert.That(error.Retryable, Is.True);
                Assert.That(error.Phase, Is.EqualTo("admission"));
                Assert.That(error.JobId, Is.Null);
                Assert.That(error.ToString(), Does.Not.Contain("private-test-token"));
            }
            Assert.That(service.Submissions, Is.Zero, "Never retry a definite admission rejection.");
        }
    }

    [Test]
    public async Task CancelAndGet_CanonicalPartialSnapshot_PreservesHostAndToolFacts()
    {
        using (var service = new FakeService { Pending = true })
        using (OrchestratorClient client = await OrchestratorClient.ConnectAsync(service.Url, "test-token"))
        using (var stop = new CancellationTokenSource())
        {
            Task<string> pending = client.TranscribeAsync(new byte[320], stop.Token);
            await service.Submitted.Task;
            OrchestratorJob cancelled = await client.CancelAsync(service.JobId);
            Assert.That(cancelled.State, Is.EqualTo("cancelled"));
            Assert.That(cancelled.HostPending, Is.True);
            Assert.That(cancelled.Error.Code, Is.EqualTo("CANCELLED"));
            Assert.That((await client.GetJobAsync(service.JobId)).State, Is.EqualTo("cancelled"));
            try { await pending; Assert.Fail("Expected failed STT."); } catch (OrchestratorException) { }
        }
    }

    private static JArray Tools() => new JArray(new JObject
    {
        ["type"] = "function", ["function"] = new JObject
        {
            ["name"] = "record", ["parameters"] = new JObject
            {
                ["type"] = "object", ["properties"] = new JObject { ["value"] = new JObject { ["type"] = "string" } },
                ["required"] = new JArray("value"), ["additionalProperties"] = false
            }
        }
    });

    [Test]
    public async Task Offline_TextToolsTtsStt_EndToEnd_UsesRevisedContract()
    {
        using (var service = new FakeService())
        using (OrchestratorClient client = await OrchestratorClient.ConnectAsync(service.Url, "test-token"))
        {
            int calls = 0;
            await client.OpenSessionAsync(Tools(), (name, arguments) =>
            {
                Interlocked.Increment(ref calls);
                Assert.That(name, Is.EqualTo("record"));
                return new JObject { ["ok"] = true };
            });
            var deltas = new StringBuilder();
            OrchestratorJob result = await client.TextAsync(new JArray(new JObject
                { ["role"] = "user", ["content"] = "hello" }), 64, 0.2f, 10000, text => deltas.Append(text));
            Assert.That(result.State, Is.EqualTo("succeeded"));
            Assert.That(result.Text, Is.EqualTo("hello"));
            Assert.That(deltas.ToString(), Is.EqualTo("partial hello"));
            Assert.That(calls, Is.EqualTo(1));
            Assert.That(result.Calls.Count, Is.EqualTo(2), "Server rejected attempts are retained.");
            Assert.That(result.Calls[0].Outcome, Is.EqualTo("rejected"));
            Assert.That(result.Calls[0].DispatchId, Is.Null);
            Assert.That(result.Calls[0].LocallyExecuted, Is.False);
            Assert.That(result.Calls[1].ModelCallId, Is.EqualTo("model-call-7"));
            Assert.That(result.Calls[1].DispatchId, Is.EqualTo(service.DispatchId));
            Assert.That(result.Calls[1].LocallyExecuted, Is.True);
            Assert.That(result.Calls[1].ResultDelivered, Is.True);
            Assert.That((await client.SynthesizeAsync(result.Text)).Samples, Is.EqualTo(service.Pcm));
            Assert.That(await client.TranscribeAsync(new byte[320]), Is.EqualTo("transcribed"));
            Assert.That(service.SessionToolsOnly, Is.True);
            Assert.That(service.ToolResultValid, Is.True);
        }
    }

    [Test]
    public async Task Text_CancelAfterLocalSideEffect_DoesNotInferDeliveryOrModelId()
    {
        using (var service = new FakeService())
        using (OrchestratorClient client = await OrchestratorClient.ConnectAsync(service.Url, "test-token"))
        using (var stop = new CancellationTokenSource())
        {
            await client.OpenSessionAsync(Tools(), (name, args) =>
            {
                stop.Cancel();
                return new JObject { ["ok"] = true };
            });
            OrchestratorJob result = await client.TextAsync(new JArray(new JObject
                { ["role"] = "user", ["content"] = "hello" }), 64, 0.2f, 10000, null, stop.Token);
            Assert.That(result.State, Is.EqualTo("cancelled"));
            Assert.That(result.Calls[1].ModelCallId, Is.Null);
            Assert.That(result.Calls[1].ResultDelivered, Is.False);
            Assert.That(result.Calls[1].Outcome, Is.EqualTo("unknown"));
            Assert.That(result.PartialText, Is.EqualTo("partial "));
            Assert.That(result.LocallyExecutedDispatchIds, Does.Contain(service.DispatchId));
        }
    }

    [Test]
    public async Task Snapshot_WrongInstance_IsRejected()
    {
        using (var service = new FakeService { Pending = true })
        using (OrchestratorClient client = await OrchestratorClient.ConnectAsync(service.Url, "test-token"))
        using (var stop = new CancellationTokenSource())
        {
            Task<string> pending = client.TranscribeAsync(new byte[320], stop.Token);
            await service.Submitted.Task;
            service.ChangeInstance = true;
            try { await client.GetJobAsync(service.JobId); Assert.Fail("Expected identity rejection."); }
            catch (OrchestratorException error) { Assert.That(error.Code, Is.EqualTo("INSTANCE_CHANGED")); }
            stop.Cancel();
            try { await pending; } catch (Exception) { }
        }
    }

    [Test]
    public async Task Artifact_StalledBody_CallerTokenCoversRead()
    {
        using (var service = new FakeService { StallArtifact = true })
        using (OrchestratorClient client = await OrchestratorClient.ConnectAsync(service.Url, "test-token"))
        using (var stop = new CancellationTokenSource())
        {
            Task<PcmAudio> operation = client.SynthesizeAsync("hello", stop.Token);
            await service.ArtifactRead.Task;
            stop.Cancel();
            Assert.That(await Task.WhenAny(operation, Task.Delay(2000)), Is.SameAs(operation));
            try { await operation; Assert.Fail("Expected cancellation."); } catch (OperationCanceledException) { }
        }
    }

    [Test]
    public async Task Tool_InvalidArguments_NeverReachLocalAuthority()
    {
        using (var service = new FakeService { InvalidArguments = true })
        using (OrchestratorClient client = await OrchestratorClient.ConnectAsync(service.Url, "test-token"))
        {
            int calls = 0;
            await client.OpenSessionAsync(Tools(), (name, args) => { calls++; return new JObject(); });
            try
            {
                await client.TextAsync(new JArray(new JObject { ["role"] = "user", ["content"] = "hello" }),
                    64, 0.2f, 5000, null);
                Assert.Fail("Expected local schema rejection.");
            }
            catch (OrchestratorException error) { Assert.That(error.Code, Is.EqualTo("INVALID_TOOL_ARGUMENTS")); }
            Assert.That(calls, Is.Zero);
        }
    }

    [Test]
    public async Task Text_Deadline_PreservesPrimaryErrorAndStopsCallbacks()
    {
        using (var service = new FakeService { StallText = true })
        using (OrchestratorClient client = await OrchestratorClient.ConnectAsync(service.Url, "test-token"))
        {
            await client.OpenSessionAsync(new JArray(), null);
            var clock = System.Diagnostics.Stopwatch.StartNew();
            try
            {
                await client.TextAsync(new JArray(new JObject { ["role"] = "user", ["content"] = "hello" }),
                    64, 0.2f, 1000, null);
                Assert.Fail("Expected deadline failure.");
            }
            catch (OrchestratorException error)
            {
                Assert.That(error.Code, Is.EqualTo("DEADLINE_EXCEEDED"));
                Assert.That(error.Phase, Is.EqualTo("operation"));
                Assert.That(error.Snapshot.State, Is.EqualTo("running"));
            }
            Assert.That(clock.ElapsedMilliseconds, Is.LessThan(2500));
        }
    }

    [Test]
    public async Task Submit_CanonicalSnapshot_IsValidatedWithoutWaitingForCompletion()
    {
        using (var service = new FakeService { Pending = true })
        using (OrchestratorClient client = await OrchestratorClient.ConnectAsync(service.Url, "test-token"))
        {
            OrchestratorJob job = await client.SubmitAsync(new JObject { ["kind"] = "tts", ["text"] = "hello", ["deadline_ms"] = 1000 });
            Assert.That(job.State, Is.EqualTo("running"));
            Assert.That(job.JobId, Is.EqualTo(service.JobId));
            Assert.That((await client.CancelAsync(job.JobId)).State, Is.EqualTo("cancelled"));
            service.InvalidSnapshot = true;
            try { await client.GetJobAsync(job.JobId); Assert.Fail("Expected malformed snapshot rejection."); }
            catch (OrchestratorException error) { Assert.That(error.Code, Is.EqualTo("PROTOCOL_ERROR")); }
        }
    }

    [Test]
    public async Task Cancellation_AdmittedJob_IsCancelled()
    {
        using (var service = new FakeService())
        using (var cancel = new CancellationTokenSource())
        {
            service.Pending = true;
            OrchestratorClient client = await OrchestratorClient.ConnectAsync(service.Url, "test-token");
            try
            {
                Task<string> operation = client.TranscribeAsync(new byte[320], cancel.Token);
                await service.Submitted.Task;
                cancel.Cancel();
                try { await operation; Assert.Fail("Expected cancellation."); }
                catch (OperationCanceledException) { }
                Assert.That(service.Cancels, Is.GreaterThanOrEqualTo(1));
            }
            finally { await client.CloseAsync(); }
        }
    }

    [Test]
    public async Task Replay_OnsetDuringScriptedText_CancelsProductionTransport()
    {
        using (var service = new FakeService())
        using (OrchestratorClient client = await OrchestratorClient.ConnectAsync(service.Url, "test-token"))
        using (var stop = new CancellationTokenSource())
        {
            var handoff = new CaptureHandoff();
            var processor = new SpeechCaptureProcessor(320, 320, 3200, 0.02f, 0.02f, block => true, handoff);
            var deltas = new StringBuilder();
            // The scripted tool request follows the known text delta, so no wall-clock race is required.
            await client.OpenSessionAsync(Tools(), (name, args) =>
            {
                handoff.QueueSamples(320);
                processor.ProcessBlock(new float[320]);
                Assert.That(handoff.TryTake(out byte[] onset), Is.True);
                Assert.That(onset, Is.Null);
                stop.Cancel();
                return new JObject { ["ok"] = true };
            });
            OrchestratorJob result = await client.TextAsync(new JArray(new JObject
                { ["role"] = "user", ["content"] = "scripted control, not a recognizer transcript" }),
                64, 0.2f, 10000, text => deltas.Append(text), stop.Token);
            Assert.That(result.State, Is.EqualTo("cancelled"));
            Assert.That(deltas.ToString(), Is.EqualTo("partial "));
            Assert.That(result.PartialText, Is.EqualTo("partial "));
            Assert.That(service.Cancels, Is.GreaterThanOrEqualTo(1));
            Assert.That(processor.IsSpeaking, Is.True);
        }
    }

    [Test]
    public async Task Replay_OnsetDuringStalledTtsArtifact_CancelsBeforeAudioIsReturned()
    {
        using (var service = new FakeService { StallArtifact = true })
        using (OrchestratorClient client = await OrchestratorClient.ConnectAsync(service.Url, "test-token"))
        using (var stop = new CancellationTokenSource())
        {
            Task<PcmAudio> synthesis = client.SynthesizeAsync("fixed synthetic response", stop.Token);
            Assert.That(await Task.WhenAny(service.ArtifactRead.Task, synthesis, Task.Delay(2000)),
                Is.SameAs(service.ArtifactRead.Task));
            var handoff = new CaptureHandoff();
            var processor = new SpeechCaptureProcessor(320, 320, 3200, 0.02f, 0.02f, block => true, handoff);
            handoff.QueueSamples(320);
            await Task.Run(() => processor.ProcessBlock(new float[320]));
            Assert.That(handoff.TryTake(out byte[] onset), Is.True);
            Assert.That(onset, Is.Null);
            stop.Cancel();
            Assert.That(await Task.WhenAny(synthesis, Task.Delay(2000)), Is.SameAs(synthesis));
            try { await synthesis; Assert.Fail("Cancelled synthesis must not return audio."); }
            catch (OperationCanceledException) { }
        }
    }

    // Loopback-only fake: no models, downloads, Unity objects or external services.
    private sealed class FakeService : IDisposable
    {
        private readonly System.Net.Sockets.TcpListener _listener = new System.Net.Sockets.TcpListener(IPAddress.Loopback, 0);
        private readonly CancellationTokenSource _stop = new CancellationTokenSource();
        private readonly string _instance = Guid.NewGuid().ToString("D");
        private readonly string _artifact = Guid.NewGuid().ToString("D");
        private readonly string _job = Guid.NewGuid().ToString("D");
        private JObject _spec;
        private WebSocket _socket;
        private long _sequence;
        private string _state;
        private JArray _calls = new JArray();
        private readonly string _session = Guid.NewGuid().ToString("D");
        public string DispatchId { get; } = Guid.NewGuid().ToString("D");
        public string JobId => _job;
        private readonly Task _loop;
        public readonly byte[] Pcm = { 1, 0, 2, 0 };
        public readonly TaskCompletionSource<bool> Submitted = new TaskCompletionSource<bool>(TaskCreationOptions.RunContinuationsAsynchronously);
        public readonly TaskCompletionSource<bool> ArtifactRead = new TaskCompletionSource<bool>(TaskCreationOptions.RunContinuationsAsynchronously);
        public string Url { get; }
        public bool Reject, RejectJobs, BadDigest, Pending, UploadValid, BadAuthorization;
        public bool FailDelete, AmbiguousAdmission, SessionToolsOnly, ToolResultValid, ChangeInstance, StallArtifact;
        public bool InvalidArguments, StallText, InvalidSnapshot;
        public int Submissions;
        public int Deletes, Cancels, Shutdowns;

        public FakeService()
        {
            _listener.Start();
            int port = ((IPEndPoint)_listener.LocalEndpoint).Port;
            Url = "http://127.0.0.1:" + port;
            _loop = Task.Run(ServeAsync);
        }

        private JObject Identity() => new JObject { ["protocol_version"] = 1, ["service_instance_id"] = _instance };

        private JObject Snapshot()
        {
            JObject snapshot = Identity();
            snapshot["job_id"] = _job;
            snapshot["v"] = 1;
            snapshot["revision"] = 1;
            snapshot["host_pending"] = Pending || _state == "cancelled";
            snapshot["selected_model"] = JValue.CreateNull();
            if (InvalidSnapshot) snapshot["host_pending"] = "false";
            if (ChangeInstance) snapshot["service_instance_id"] = Guid.NewGuid().ToString("D");
            snapshot["kind"] = _spec["kind"];
            snapshot["session_id"] = _spec["session_id"]?.DeepClone() ?? JValue.CreateNull();
            snapshot["turn_id"] = _spec["turn_id"]?.DeepClone() ?? JValue.CreateNull();
            snapshot["state"] = _state ?? (Pending ? "running" : "succeeded");
            snapshot["error"] = _state == "cancelled" ? Error("CANCELLED", false, _job)["error"] : JValue.CreateNull();
            string hash;
            using (SHA256 sha = SHA256.Create())
                hash = BitConverter.ToString(sha.ComputeHash(Pcm)).Replace("-", "").ToLowerInvariant();
            snapshot["result"] = (string)_spec["kind"] == "stt" ?
                new JObject { ["text"] = "transcribed" } : new JObject
                {
                    ["artifact_id"] = _artifact, ["sample_rate"] = 24000,
                    ["sample_count"] = 2, ["sha256"] = hash
                };
            if ((string)_spec["kind"] == "text") snapshot["result"] = new JObject
                { ["text"] = _state == "succeeded" ? "hello" : "", ["calls"] = _calls.DeepClone() };
            return snapshot;
        }

        private static JObject Error(string code, bool retryable, string job = null) => new JObject
        {
            ["error"] = new JObject { ["code"] = code, ["message"] = "private-test-token",
                ["retryable"] = retryable, ["job_id"] = job }
        };

        private async Task SendAsync(string type, JObject payload)
        {
            byte[] bytes = Encoding.UTF8.GetBytes(new JObject
            {
                ["v"] = 1, ["seq"] = ++_sequence, ["type"] = type, ["payload"] = payload,
                ["service_instance_id"] = _instance, ["session_id"] = _session,
                ["job_id"] = _spec?["job_id"] ?? (_spec == null ? JValue.CreateNull() : new JValue(_job)),
                ["turn_id"] = _spec?["turn_id"]?.DeepClone() ?? JValue.CreateNull()
            }.ToString(Newtonsoft.Json.Formatting.None));
            await _socket.SendAsync(new ArraySegment<byte>(bytes), WebSocketMessageType.Text, true, _stop.Token);
        }

        private async Task ReceiveSocketAsync()
        {
            byte[] buffer = new byte[65536];
            try
            {
                while (!_stop.IsCancellationRequested)
                {
                    WebSocketReceiveResult received = await _socket.ReceiveAsync(new ArraySegment<byte>(buffer), _stop.Token);
                    if (received.MessageType != WebSocketMessageType.Text) return;
                    JObject message = JObject.Parse(Encoding.UTF8.GetString(buffer, 0, received.Count));
                    if ((string)message["type"] == "hello")
                        await SendAsync("hello.accepted", new JObject { ["protocol_version"] = 1 });
                    else if ((string)message["type"] == "tool.result")
                    {
                        ToolResultValid = (string)message["payload"]["call_id"] == DispatchId &&
                            message["payload"]["model_call_id"] == null && (string)message["job_id"] == _job;
                        _calls[1]["outcome"] = "completed";
                        _calls[1]["result_delivered"] = true;
                        _calls[1]["id"] = "model-call-7";
                        _calls[1]["result"] = message["payload"]["result"].DeepClone();
                        await SendAsync("text.delta", new JObject { ["text"] = "hello" });
                        _state = "succeeded";
                        await SendAsync("job.state", Snapshot());
                    }
                    else if ((string)message["type"] == "ping") await SendAsync("pong", new JObject());
                }
            }
            catch (Exception) when (_stop.IsCancellationRequested || _socket.State != WebSocketState.Open) { }
        }

        private async Task StartTextAsync()
        {
            if (StallText) return;
            _calls = new JArray(new JObject
            {
                ["id"] = "model-rejected", ["name"] = "forbidden", ["arguments"] = new JObject(),
                ["result"] = new JObject { ["error"] = "TOOL_NOT_PERMITTED" }, ["dispatch_id"] = JValue.CreateNull(),
                ["outcome"] = "rejected", ["result_delivered"] = true
            }, new JObject
            {
                ["id"] = JValue.CreateNull(), ["name"] = "record", ["arguments"] = new JObject { ["value"] = "Ada" },
                ["result"] = JValue.CreateNull(), ["dispatch_id"] = DispatchId, ["outcome"] = "unknown", ["result_delivered"] = false
            });
            _state = "waiting_tool";
            await SendAsync("job.state", Snapshot());
            await SendAsync("text.delta", new JObject { ["text"] = "partial " });
            await SendAsync("tool.call", new JObject { ["call_id"] = DispatchId, ["name"] = "record",
                ["arguments"] = new JObject { ["value"] = InvalidArguments ? new JValue(7) : new JValue("Ada") }, ["timeout_ms"] = 5000 });
        }

        private async Task ServeAsync()
        {
            try
            {
                while (!_stop.IsCancellationRequested)
                {
                    var connection = await _listener.AcceptTcpClientAsync();
                    TcpContext context = await TcpContext.ReadAsync(connection, Url);
                    TcpRequest request = context.Request;
                    TcpResponse response = context.Response;
                    if (request.Headers["Authorization"] != "Bearer test-token" && !Reject) BadAuthorization = true;
                    string path = request.Url.AbsolutePath;
                    if (request.IsWebSocketRequest)
                    {
                        _socket = await context.AcceptWebSocketAsync();
                        _ = ReceiveSocketAsync();
                        continue;
                    }
                    JObject body = Identity();
                    byte[] raw = null;
                    response.StatusCode = 200;
                    if (Reject || RejectJobs && path == "/v1/jobs" || AmbiguousAdmission && path == "/v1/jobs")
                    {
                        response.StatusCode = RejectJobs ? 429 : AmbiguousAdmission ? 500 : 403;
                        body = Error(RejectJobs ? "RESOURCE_BUSY" : "INTERNAL_ERROR", RejectJobs);
                    }
                    else if (path == "/v1/health/ready") { body["ready"] = true; body["state"] = "ready"; }
                    else if (path == "/v1/capabilities") { body["kinds"] = new JObject(); body["probe"] = new JObject(); }
                    else if (path == "/v1/sessions" && request.HttpMethod == "POST")
                    {
                        using (var reader = new StreamReader(request.InputStream))
                        {
                            JObject spec = JObject.Parse(await reader.ReadToEndAsync());
                            SessionToolsOnly = spec.Count == 1 && spec["tools"] is JArray;
                        }
                        body["session_id"] = _session;
                    }
                    else if (path == "/v1/jobs" && request.HttpMethod == "POST")
                    {
                        Submissions++;
                        using (var reader = new StreamReader(request.InputStream)) _spec = JObject.Parse(await reader.ReadToEndAsync());
                        _state = (string)_spec["kind"] == "text" ? "running" : null;
                        body = Snapshot();
                        response.StatusCode = 202;
                        Submitted.TrySetResult(true);
                    }
                    else if (path.EndsWith("/cancel")) { Cancels++; _state = "cancelled"; body = Snapshot(); }
                    else if (path.StartsWith("/v1/jobs/")) body = Snapshot();
                    else if (path == "/v1/artifacts")
                    {
                        using (var input = new MemoryStream())
                        {
                            await request.InputStream.CopyToAsync(input);
                            UploadValid = input.Length == 320 && request.ContentType == "application/octet-stream" &&
                                request.Headers["X-Sample-Rate"] == "16000";
                        }
                        body = new JObject { ["artifact_id"] = _artifact };
                        response.StatusCode = 201;
                    }
                    else if (path.StartsWith("/v1/artifacts/") && request.HttpMethod == "DELETE")
                    {
                        Deletes++;
                        body = new JObject { ["artifact_id"] = _artifact, ["state"] = "deleted" };
                        if (FailDelete) { response.StatusCode = 500; body = Error("INTERNAL_ERROR", false); }
                    }
                    else if (path.StartsWith("/v1/artifacts/"))
                    {
                        raw = Pcm;
                        if (StallArtifact)
                        {
                            response.ContentLength64 = Pcm.Length;
                            await response.OutputStream.WriteAsync(Pcm, 0, 1);
                            await response.OutputStream.FlushAsync();
                            ArtifactRead.TrySetResult(true);
                            continue;
                        }
                        response.Headers["X-Sample-Rate"] = "24000";
                        response.Headers["X-Sample-Count"] = "2";
                        response.Headers["X-Content-SHA256"] = BadDigest ? "wrong" : (string)Snapshot()["result"]["sha256"];
                    }
                    else if (path == "/v1/control/shutdown") Shutdowns++;
                    raw = raw ?? Encoding.UTF8.GetBytes(body.ToString(Newtonsoft.Json.Formatting.None));
                    response.ContentLength64 = raw.Length;
                    await response.OutputStream.WriteAsync(raw, 0, raw.Length);
                    response.Close();
                    if (path == "/v1/jobs" && response.StatusCode == 202 && (string)_spec?["kind"] == "text") await StartTextAsync();
                }
            }
            catch (Exception) when (_stop.IsCancellationRequested) { }
        }

        public void Dispose()
        {
            _stop.Cancel();
            _listener.Stop();
            _socket?.Abort();
            // The task observes listener cancellation; no synchronous wait on Unity's main thread.
            _ = _loop;
        }

        private sealed class TcpRequest
        {
            public string HttpMethod;
            public Uri Url;
            public readonly System.Collections.Specialized.NameValueCollection Headers = new System.Collections.Specialized.NameValueCollection();
            public Stream InputStream;
            public string ContentType => Headers["Content-Type"];
            public bool IsWebSocketRequest => Headers["Upgrade"] == "websocket";
        }

        private sealed class TcpResponse
        {
            private readonly System.Net.Sockets.TcpClient _client;
            private bool _sent;
            public int StatusCode = 200;
            public long ContentLength64;
            public readonly System.Collections.Specialized.NameValueCollection Headers = new System.Collections.Specialized.NameValueCollection();
            public TcpResponse(System.Net.Sockets.TcpClient client) { _client = client; }
            public Stream OutputStream
            {
                get
                {
                    if (!_sent)
                    {
                        _sent = true;
                        var text = new StringBuilder("HTTP/1.1 " + StatusCode + " Response\r\nConnection: close\r\nContent-Length: " + ContentLength64 + "\r\n");
                        foreach (string key in Headers) text.Append(key).Append(": ").Append(Headers[key]).Append("\r\n");
                        byte[] bytes = Encoding.ASCII.GetBytes(text.Append("\r\n").ToString());
                        _client.GetStream().Write(bytes, 0, bytes.Length);
                    }
                    return _client.GetStream();
                }
            }
            public void Close() { _client.Dispose(); }
        }

        private sealed class TcpContext
        {
            private System.Net.Sockets.TcpClient _client;
            public TcpRequest Request;
            public TcpResponse Response;
            public static async Task<TcpContext> ReadAsync(System.Net.Sockets.TcpClient client, string origin)
            {
                Stream stream = client.GetStream();
                var bytes = new List<byte>();
                var one = new byte[1];
                while (bytes.Count < 16384)
                {
                    if (await stream.ReadAsync(one, 0, 1) == 0) throw new IOException("Closed request");
                    bytes.Add(one[0]);
                    int n = bytes.Count;
                    if (n >= 4 && bytes[n - 4] == 13 && bytes[n - 3] == 10 && bytes[n - 2] == 13 && bytes[n - 1] == 10) break;
                }
                string[] lines = Encoding.ASCII.GetString(bytes.ToArray()).Split(new[] { "\r\n" }, StringSplitOptions.None);
                string[] first = lines[0].Split(' ');
                var request = new TcpRequest { HttpMethod = first[0], Url = new Uri(origin + first[1]) };
                foreach (string line in lines)
                {
                    int colon = line.IndexOf(':');
                    if (colon > 0) request.Headers.Add(line.Substring(0, colon), line.Substring(colon + 1).Trim());
                }
                if (request.Headers["Expect"] == "100-continue")
                {
                    byte[] interim = Encoding.ASCII.GetBytes("HTTP/1.1 100 Continue\r\n\r\n");
                    await stream.WriteAsync(interim, 0, interim.Length);
                }
                int length = int.Parse(request.Headers["Content-Length"] ?? "0");
                byte[] body = new byte[length];
                for (int offset = 0; offset < length;)
                {
                    int read = await stream.ReadAsync(body, offset, length - offset);
                    if (read == 0) throw new IOException("Closed body");
                    offset += read;
                }
                request.InputStream = new MemoryStream(body);
                return new TcpContext { _client = client, Request = request, Response = new TcpResponse(client) };
            }
            public async Task<WebSocket> AcceptWebSocketAsync()
            {
                string accept;
                using (SHA1 sha = SHA1.Create())
                    accept = Convert.ToBase64String(sha.ComputeHash(Encoding.ASCII.GetBytes(Request.Headers["Sec-WebSocket-Key"] + "258EAFA5-E914-47DA-95CA-C5AB0DC85B11")));
                byte[] bytes = Encoding.ASCII.GetBytes("HTTP/1.1 101 Switching Protocols\r\nUpgrade: websocket\r\nConnection: Upgrade\r\nSec-WebSocket-Accept: " + accept + "\r\n\r\n");
                await _client.GetStream().WriteAsync(bytes, 0, bytes.Length);
                return WebSocket.CreateFromStream(_client.GetStream(), true, null, Timeout.InfiniteTimeSpan);
            }
        }
    }
}