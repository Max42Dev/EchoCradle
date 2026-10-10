using System;
using System.Collections.Generic;
using System.IO;
using System.Net;
using System.Reflection;
using System.Security.Cryptography;
using System.Text;
using System.Threading;
using System.Threading.Tasks;
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

    // Loopback-only fake: no models, downloads, Unity objects or external services.
    private sealed class FakeService : IDisposable
    {
        private readonly HttpListener _listener = new HttpListener();
        private readonly CancellationTokenSource _stop = new CancellationTokenSource();
        private readonly string _instance = Guid.NewGuid().ToString("D");
        private readonly string _artifact = Guid.NewGuid().ToString("D");
        private readonly string _job = Guid.NewGuid().ToString("D");
        private JObject _spec;
        private readonly Task _loop;
        public readonly byte[] Pcm = { 1, 0, 2, 0 };
        public readonly TaskCompletionSource<bool> Submitted = new TaskCompletionSource<bool>(TaskCreationOptions.RunContinuationsAsynchronously);
        public string Url { get; }
        public bool Reject, RejectJobs, BadDigest, Pending, UploadValid, BadAuthorization;
        public int Deletes, Cancels, Shutdowns;

        public FakeService()
        {
            var portProbe = new System.Net.Sockets.TcpListener(IPAddress.Loopback, 0);
            portProbe.Start();
            int port = ((IPEndPoint)portProbe.LocalEndpoint).Port;
            portProbe.Stop();
            Url = "http://127.0.0.1:" + port;
            _listener.Prefixes.Add(Url + "/");
            _listener.Start();
            _loop = Task.Run(ServeAsync);
        }

        private JObject Identity() => new JObject { ["protocol_version"] = 1, ["service_instance_id"] = _instance };

        private JObject Snapshot()
        {
            JObject snapshot = Identity();
            snapshot["job_id"] = _job;
            snapshot["kind"] = _spec["kind"];
            snapshot["session_id"] = JValue.CreateNull();
            snapshot["turn_id"] = JValue.CreateNull();
            snapshot["state"] = Pending ? "running" : "succeeded";
            string hash;
            using (SHA256 sha = SHA256.Create())
                hash = BitConverter.ToString(sha.ComputeHash(Pcm)).Replace("-", "").ToLowerInvariant();
            snapshot["result"] = (string)_spec["kind"] == "stt" ?
                new JObject { ["text"] = "transcribed" } : new JObject
                {
                    ["artifact_id"] = _artifact, ["sample_rate"] = 24000,
                    ["sample_count"] = 2, ["sha256"] = hash
                };
            return snapshot;
        }

        private async Task ServeAsync()
        {
            try
            {
                while (!_stop.IsCancellationRequested)
                {
                    HttpListenerContext context = await _listener.GetContextAsync();
                    HttpListenerRequest request = context.Request;
                    HttpListenerResponse response = context.Response;
                    if (request.Headers["Authorization"] != "Bearer test-token" && !Reject) BadAuthorization = true;
                    string path = request.Url.AbsolutePath;
                    JObject body = Identity();
                    byte[] raw = null;
                    response.StatusCode = 200;
                    if (Reject || RejectJobs && path == "/v1/jobs")
                    {
                        response.StatusCode = 403;
                        body = new JObject { ["error"] = new JObject { ["code"] = "private-test-token", ["message"] = "private-test-token" } };
                    }
                    else if (path == "/v1/health/ready") { body["ready"] = true; body["state"] = "ready"; }
                    else if (path == "/v1/capabilities") { body["kinds"] = new JObject(); body["probe"] = new JObject(); }
                    else if (path == "/v1/jobs" && request.HttpMethod == "POST")
                    {
                        using (var reader = new StreamReader(request.InputStream)) _spec = JObject.Parse(await reader.ReadToEndAsync());
                        body = Snapshot();
                        response.StatusCode = 202;
                        Submitted.TrySetResult(true);
                    }
                    else if (path.EndsWith("/cancel")) { Cancels++; body = Snapshot(); body["state"] = "cancelled"; }
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
                    }
                    else if (path.StartsWith("/v1/artifacts/"))
                    {
                        raw = Pcm;
                        response.Headers["X-Sample-Rate"] = "24000";
                        response.Headers["X-Sample-Count"] = "2";
                        response.Headers["X-Content-SHA256"] = BadDigest ? "wrong" : (string)Snapshot()["result"]["sha256"];
                    }
                    else if (path == "/v1/control/shutdown") Shutdowns++;
                    raw = raw ?? Encoding.UTF8.GetBytes(body.ToString(Newtonsoft.Json.Formatting.None));
                    response.ContentLength64 = raw.Length;
                    await response.OutputStream.WriteAsync(raw, 0, raw.Length);
                    response.Close();
                }
            }
            catch (Exception) when (_stop.IsCancellationRequested) { }
        }

        public void Dispose()
        {
            _stop.Cancel();
            _listener.Close();
            // The task observes listener cancellation; no synchronous wait on Unity's main thread.
            _ = _loop;
        }
    }
}