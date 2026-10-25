using System;
using System.Collections.Generic;
using System.IO;
using System.Net;
using System.Net.Sockets;
using System.Reflection;
using System.Security.Cryptography;
using System.Text;
using System.Threading;
using System.Threading.Tasks;
using EchoCradle.Orchestration;
using Newtonsoft.Json.Linq;
using NUnit.Framework;

public sealed class VisualTransportTests
{
    private static readonly byte[] Png = Convert.FromBase64String(
        "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII=");

    private static byte[] Glb(string json = "{\"asset\":{\"version\":\"2.0\"}}")
    {
        byte[] text = Encoding.UTF8.GetBytes(json.PadRight((json.Length + 3) / 4 * 4));
        using (var stream = new MemoryStream())
        using (var writer = new BinaryWriter(stream))
        {
            writer.Write(0x46546c67u); writer.Write(2u); writer.Write((uint)(20 + text.Length));
            writer.Write((uint)text.Length); writer.Write(0x4e4f534au); writer.Write(text);
            return stream.ToArray();
        }
    }

    private static void Validate(byte[] bytes, string media)
    {
        Type type = typeof(OrchestratorClient).Assembly.GetType("EchoCradle.Orchestration.VisualContainer");
        try { type.GetMethod("Validate", BindingFlags.NonPublic | BindingFlags.Static).Invoke(null, new object[] { bytes, media }); }
        catch (TargetInvocationException error) { throw error.InnerException; }
    }

    [Test]
    public void Containers_ValidPngAndSelfContainedGlb_Accepted()
    {
        Validate(Png, "image/png");
        Validate(Glb(), "model/gltf-binary");
    }

    [Test]
    public void Png_CorruptCrcTruncationAndTrailingData_Rejected()
    {
        byte[] corrupt = (byte[])Png.Clone(); corrupt[29] ^= 1;
        Assert.Throws<OrchestratorException>(() => Validate(corrupt, "image/png"));
        Assert.Throws<OrchestratorException>(() => Validate(new byte[20], "image/png"));
        byte[] trailing = new byte[Png.Length + 1]; Buffer.BlockCopy(Png, 0, trailing, 0, Png.Length);
        Assert.Throws<OrchestratorException>(() => Validate(trailing, "image/png"));
    }

    [TestCase("{\"asset\":{\"version\":\"2.0\"},\"buffers\":[{\"uri\":\"file:///secret\",\"byteLength\":4}]}")]
    [TestCase("{\"asset\":{\"version\":\"2.0\"},\"buffers\":[{\"uri\":\"data:application/octet-stream;base64,AAAA\",\"byteLength\":4}]}")]
    [TestCase("{\"asset\":{\"version\":\"2.0\"},\"extensionsRequired\":[\"KHR_draco_mesh_compression\"]}")]
    [TestCase("{\"asset\":{\"version\":\"2.0\"},\"extras\":{\"extensions\":{\"CUSTOM_loader\":{}}}}")]
    [TestCase("{\"asset\":{\"version\":\"2.0\"},\"bufferViews\":[{\"buffer\":0,\"byteLength\":8}]}")]
    [TestCase("{\"asset\":{\"version\":\"2.0\"},\"images\":[{\"bufferView\":0,\"mimeType\":\"image/png\"}]}")]
    [TestCase("{\"asset\":{\"version\":\"2.0\",\"version\":\"2.0\"}}")]
    [TestCase("{\"asset\":{\"version\":\"2.0\"},\"extras\":NaN}")]
    public void Glb_UnsafeOrMalformedResources_Rejected(string json)
    {
        Assert.Throws<OrchestratorException>(() => Validate(Glb(json), "model/gltf-binary"));
    }

    [Test]
    public void Glb_OverflowChunkAndLengthMismatch_Rejected()
    {
        byte[] bytes = Glb(); bytes[12] = 0xfc; bytes[13] = 0xff; bytes[14] = 0xff; bytes[15] = 0xff;
        Assert.Throws<OrchestratorException>(() => Validate(bytes, "model/gltf-binary"));
        bytes = Glb(); bytes[8]++;
        Assert.Throws<OrchestratorException>(() => Validate(bytes, "model/gltf-binary"));
    }

    [Test]
    public async Task Image_CallerSeedLongDeadlineAndDefensiveCopies_OutputRetainedUntilRelease()
    {
        using (var server = new VisualServer())
        using (OrchestratorClient client = await OrchestratorClient.ConnectAsync(server.Url, "test-token"))
        {
            OrchestratorVisualResult result = await client.GenerateImageAsync("portrait", uint.MaxValue, "blur");
            Assert.That(result.Seed, Is.EqualTo(uint.MaxValue));
            Assert.That(result.MediaType, Is.EqualTo("image/png"));
            Assert.That(result.ByteLength, Is.EqualTo(Png.Length));
            Assert.That(result.Sha256, Is.EqualTo(VisualServer.Digest(Png)));
            byte[] payload = result.Payload; payload[0] = 0;
            Assert.That(result.Payload, Is.EqualTo(Png));
            Assert.That((int)server.Spec["deadline_ms"], Is.EqualTo(600000));
            Assert.That((string)server.Spec["negative_prompt"], Is.EqualTo("blur"));
            Assert.That((string)server.Spec["idempotency_key"], Is.Not.Empty);
            Assert.That(server.Deletes, Is.Empty);
            await client.ReleaseArtifactAsync(result.ArtifactId);
            Assert.That(server.Deletes, Is.EqualTo(new[] { result.ArtifactId }));
            Assert.That(server.BadAuthorization, Is.False);
        }
    }

    [TestCase(false)]
    [TestCase(true)]
    public async Task VisualInput_ExactMediaSnapshotAndDeleteAfterAdmission(bool animate)
    {
        using (var server = new VisualServer())
        using (OrchestratorClient client = await OrchestratorClient.ConnectAsync(server.Url, "test-token"))
        {
            byte[] input = animate ? Glb() : (byte[])Png.Clone();
            byte[] expected = (byte[])input.Clone();
            Task<OrchestratorVisualResult> pending = animate ? client.Animate3DAsync(input, 73) : client.Generate3DAsync(input, 73);
            input[0] = 0;
            OrchestratorVisualResult result = await pending;
            Assert.That(server.Upload, Is.EqualTo(expected));
            Assert.That(server.UploadMedia, Is.EqualTo(animate ? "model/gltf-binary" : "image/png"));
            Assert.That(server.UploadSampleRate, Is.Null);
            Assert.That(server.Spec["kind"].ToString(), Is.EqualTo(animate ? "animate_3d" : "image_to_3d"));
            Assert.That(server.Events.IndexOf("admit"), Is.LessThan(server.Events.IndexOf("delete-input")));
            Assert.That(server.Deletes, Is.EqualTo(new[] { server.InputId }));
            Assert.That(result.Payload, Is.EqualTo(Glb()));
        }
    }

    [TestCase("digest")]
    [TestCase("header")]
    [TestCase("media")]
    [TestCase("length")]
    [TestCase("seed")]
    [TestCase("container")]
    [TestCase("identity")]
    [TestCase("job")]
    [TestCase("kind")]
    public async Task VisualOutput_BadIntegrityOrCorrelation_Rejected(string fault)
    {
        using (var server = new VisualServer { Fault = fault })
        using (OrchestratorClient client = await OrchestratorClient.ConnectAsync(server.Url, "test-token"))
        {
            try { await client.GenerateImageAsync("portrait", 19); Assert.Fail("Expected rejection."); }
            catch (OrchestratorException) { }
            Assert.That(server.Deletes, Is.Empty, "Only explicit release deletes published outputs.");
        }
    }

    [TestCase(429, 1)]
    [TestCase(500, 0)]
    public async Task Admission_DefiniteRejectionDeletesInput_AmbiguityRetainsIt(int status, int deletes)
    {
        using (var server = new VisualServer { RejectStatus = status })
        using (OrchestratorClient client = await OrchestratorClient.ConnectAsync(server.Url, "test-token"))
        {
            try { await client.Generate3DAsync(Png, 8); Assert.Fail("Expected rejection."); }
            catch (OrchestratorException) { }
            Assert.That(server.Deletes.Count, Is.EqualTo(deletes));
        }
    }

    [Test]
    public async Task VisualCancellation_AdmittedJobCancelled_InputAlreadyReleased()
    {
        using (var server = new VisualServer { Pending = true })
        using (OrchestratorClient client = await OrchestratorClient.ConnectAsync(server.Url, "test-token"))
        using (var stop = new CancellationTokenSource())
        {
            Task<OrchestratorVisualResult> task = client.Generate3DAsync(Png, 8, token: stop.Token);
            // Polling starts only after client admission and the awaited input release.
            Task admitted = server.JobPolled.Task;
            Assert.That(await Task.WhenAny(admitted, task, Task.Delay(5000)), Is.SameAs(admitted));
            Assert.That(server.InputReleased.Task.IsCompleted, Is.True);
            Assert.That(server.Deletes, Is.EqualTo(new[] { server.InputId }));
            stop.Cancel();
            Assert.That(await Task.WhenAny(task, Task.Delay(5000)), Is.SameAs(task));
            try { await task; Assert.Fail("Expected cancellation."); }
            catch (OperationCanceledException) { }
            Assert.That(server.CancelObserved.Task.IsCompleted, Is.True);
            Assert.That(server.Cancels, Is.GreaterThanOrEqualTo(1));
            Assert.That(server.Deletes, Is.EqualTo(new[] { server.InputId }));
        }
    }

    [Test]
    public async Task Validation_BeforeUploadAndLegacyDeadlineStillBounded()
    {
        using (var server = new VisualServer())
        using (OrchestratorClient client = await OrchestratorClient.ConnectAsync(server.Url, "test-token"))
        {
            try { await client.Generate3DAsync(new byte[10], 1); Assert.Fail("Expected invalid input."); }
            catch (OrchestratorException) { }
            try { await client.GenerateImageAsync("x", 1, deadlineMs: 3600001); Assert.Fail("Expected invalid deadline."); }
            catch (OrchestratorException) { }
            MethodInfo run = typeof(OrchestratorClient).GetMethod("RunJobAsync", BindingFlags.Instance | BindingFlags.NonPublic);
            var legacy = (Task<JObject>)run.Invoke(client, new object[] { new JObject
                { ["kind"] = "tts", ["deadline_ms"] = 600000 }, CancellationToken.None, null, null, false, null });
            try { await legacy; Assert.Fail("Expected legacy deadline cap."); }
            catch (OrchestratorException) { }
            Assert.That(server.Spec, Is.Null);
            Assert.That(server.Upload, Is.Null);
        }
    }

    [Test]
    public void OwnedStartup_CommandQuotesProfileConfigAndTrailingSlashes()
    {
        Type type = typeof(OwnedOrchestrator).Assembly.GetType("EchoCradle.Orchestration.WindowsOrchestratorProcess");
        MethodInfo build = type.GetMethod("BuildCommand", BindingFlags.Static | BindingFlags.NonPublic);
        string command = (string)build.Invoke(null, new object[] { @"C:\Python Folder\python.exe", "visual", @"C:\Visual Config\automatic.json" });
        Assert.That(command, Is.EqualTo("\"C:\\Python Folder\\python.exe\" -m orchestrator.service --profile \"visual\" --port 0 --visual-config \"C:\\Visual Config\\automatic.json\""));
        command = (string)build.Invoke(null, new object[] { "python", "voice", "C:\\folder\\" });
        Assert.That(command, Does.EndWith("\"C:\\folder\\\\\""));
    }

    private sealed class VisualServer : IDisposable
    {
        private readonly TcpListener _listener = new TcpListener(IPAddress.Loopback, 0);
        private readonly CancellationTokenSource _stop = new CancellationTokenSource();
        private readonly string _instance = Guid.NewGuid().ToString("D");
        private readonly string _job = Guid.NewGuid().ToString("D");
        private readonly string _output = Guid.NewGuid().ToString("D");
        internal readonly string InputId = Guid.NewGuid().ToString("D");
        internal readonly List<string> Deletes = new List<string>();
        internal readonly List<string> Events = new List<string>();
        internal readonly TaskCompletionSource<bool> Admitted = new TaskCompletionSource<bool>(TaskCreationOptions.RunContinuationsAsynchronously);
        internal readonly TaskCompletionSource<bool> JobPolled = new TaskCompletionSource<bool>(TaskCreationOptions.RunContinuationsAsynchronously);
        internal readonly TaskCompletionSource<bool> CancelObserved = new TaskCompletionSource<bool>(TaskCreationOptions.RunContinuationsAsynchronously);
        internal readonly TaskCompletionSource<bool> InputReleased = new TaskCompletionSource<bool>(TaskCreationOptions.RunContinuationsAsynchronously);
        internal string Url, Fault, UploadMedia, UploadSampleRate;
        internal JObject Spec;
        internal byte[] Upload;
        internal int RejectStatus, Cancels;
        internal bool Pending, BadAuthorization;

        internal VisualServer()
        {
            _listener.Start();
            Url = "http://127.0.0.1:" + ((IPEndPoint)_listener.LocalEndpoint).Port;
            _ = ServeAsync();
        }

        internal static string Digest(byte[] bytes)
        {
            using (SHA256 sha = SHA256.Create())
                return BitConverter.ToString(sha.ComputeHash(bytes)).Replace("-", "").ToLowerInvariant();
        }

        private JObject Identity() => new JObject { ["protocol_version"] = 1, ["service_instance_id"] = _instance };

        private JObject Snapshot()
        {
            JObject value = Identity();
            string kind = (string)Spec["kind"];
            byte[] payload = kind == "image" ? Png : Glb();
            if (Fault == "container") payload = new byte[32];
            bool cancelled = Cancels > 0;
            value["v"] = 1; value["job_id"] = Fault == "job" ? Guid.NewGuid().ToString("D") : _job;
            value["kind"] = Fault == "kind" ? "tts" : kind;
            value["session_id"] = JValue.CreateNull(); value["turn_id"] = JValue.CreateNull();
            value["state"] = cancelled ? "cancelled" : Pending || Fault == "job" ? "running" : "succeeded";
            value["revision"] = cancelled ? 2 : 1; value["host_pending"] = Pending;
            value["selected_model"] = JValue.CreateNull();
            value["error"] = cancelled ? new JObject { ["code"] = "CANCELLED", ["message"] = "Cancelled",
                ["retryable"] = false, ["job_id"] = _job } : JValue.CreateNull();
            value["result"] = Pending ? JValue.CreateNull() : new JObject
            {
                ["artifact_id"] = _output, ["media_type"] = kind == "image" ? "image/png" : "model/gltf-binary",
                ["byte_length"] = payload.Length + (Fault == "length" ? 1 : 0),
                ["sha256"] = Fault == "digest" ? new string('0', 64) : Digest(payload),
                ["seed"] = Fault == "seed" ? new JValue(100) : Spec["seed"].DeepClone(), ["experimental"] = true
            };
            return value;
        }

        private async Task ServeAsync()
        {
            try
            {
                while (!_stop.IsCancellationRequested)
                {
                    using (TcpClient connection = await _listener.AcceptTcpClientAsync())
                    using (NetworkStream stream = connection.GetStream())
                    {
                        var header = new StringBuilder();
                        byte[] one = new byte[1];
                        while (!header.ToString().EndsWith("\r\n\r\n", StringComparison.Ordinal))
                        {
                            if (await stream.ReadAsync(one, 0, 1) == 0) throw new IOException();
                            header.Append((char)one[0]);
                            if (header.Length > 16384) throw new IOException();
                        }
                        string[] lines = header.ToString().Split(new[] { "\r\n" }, StringSplitOptions.None);
                        string[] request = lines[0].Split(' ');
                        var headers = new Dictionary<string, string>(StringComparer.OrdinalIgnoreCase);
                        for (int i = 1; i < lines.Length; i++)
                        {
                            int colon = lines[i].IndexOf(':');
                            if (colon > 0) headers[lines[i].Substring(0, colon)] = lines[i].Substring(colon + 1).Trim();
                        }
                        BadAuthorization |= !headers.TryGetValue("Authorization", out string auth) || auth != "Bearer test-token";
                        int length = headers.TryGetValue("Content-Length", out string size) ? int.Parse(size) : 0;
                        byte[] body = new byte[length]; int offset = 0;
                        while (offset < length)
                        {
                            int count = await stream.ReadAsync(body, offset, length - offset);
                            if (count == 0) throw new IOException();
                            offset += count;
                        }
                        string path = request[1], method = request[0], media = "application/json", extra = "";
                        int status = 200;
                        JObject result = Identity(); byte[] raw = null;
                        if (path == "/v1/health/ready") { result["ready"] = true; result["state"] = "ready"; }
                        else if (path == "/v1/capabilities") { result["kinds"] = new JObject(); result["probe"] = new JObject(); }
                        else if (path == "/v1/artifacts")
                        {
                            Upload = body; UploadMedia = headers["Content-Type"];
                            headers.TryGetValue("X-Sample-Rate", out UploadSampleRate);
                            result = new JObject { ["artifact_id"] = InputId }; status = 201;
                        }
                        else if (path == "/v1/jobs")
                        {
                            Spec = JObject.Parse(Encoding.UTF8.GetString(body));
                            if (RejectStatus != 0)
                            {
                                status = RejectStatus;
                                result = new JObject { ["error"] = new JObject { ["code"] = "RESOURCE_BUSY",
                                    ["message"] = "Rejected", ["retryable"] = true, ["job_id"] = JValue.CreateNull() } };
                            }
                            else { Events.Add("admit"); result = Snapshot(); status = 202; }
                        }
                        else if (path.EndsWith("/cancel", StringComparison.Ordinal))
                        {
                            Cancels++; result = Snapshot();
                            CancelObserved.TrySetResult(true);
                        }
                        else if (path.StartsWith("/v1/jobs/", StringComparison.Ordinal))
                        {
                            result = Snapshot();
                        }
                        else if (method == "DELETE")
                        {
                            string id = path.Substring(path.LastIndexOf('/') + 1); Deletes.Add(id);
                            if (id == InputId)
                            {
                                Events.Add("delete-input");
                                InputReleased.TrySetResult(true);
                            }
                            result = new JObject { ["artifact_id"] = id, ["state"] = "deleted" };
                        }
                        else if (path.StartsWith("/v1/artifacts/", StringComparison.Ordinal))
                        {
                            raw = Fault == "container" ? new byte[32] : (string)Spec["kind"] == "image" ? Png : Glb();
                            media = Fault == "media" ? "application/octet-stream" : (string)Spec["kind"] == "image" ? "image/png" : "model/gltf-binary";
                            extra = "X-Content-SHA256: " + (Fault == "header" ? "wrong" : Digest(raw)) + "\r\n";
                        }
                        if (Fault == "identity" && path == "/v1/health/live") result["service_instance_id"] = Guid.NewGuid().ToString("D");
                        raw = raw ?? Encoding.UTF8.GetBytes(result.ToString(Newtonsoft.Json.Formatting.None));
                        byte[] response = Encoding.ASCII.GetBytes("HTTP/1.1 " + status + " Response\r\nConnection: close\r\nContent-Type: " + media +
                            "\r\nContent-Length: " + raw.Length + "\r\n" + extra + "\r\n");
                        await stream.WriteAsync(response, 0, response.Length);
                        await stream.WriteAsync(raw, 0, raw.Length);
                        if (method == "GET" && path == "/v1/jobs/" + _job)
                            JobPolled.TrySetResult(true);
                        if (path == "/v1/jobs" && status == 202) Admitted.TrySetResult(true);
                    }
                }
            }
            catch (Exception) when (_stop.IsCancellationRequested) { }
        }

        public void Dispose() { _stop.Cancel(); _listener.Stop(); }
    }
}