using System;
using System.Collections.Generic;
using System.IO;
using System.Net.Http;
using System.Net.Http.Headers;
using System.Security.Cryptography;
using System.Threading;
using System.Threading.Tasks;
using Newtonsoft.Json.Linq;

namespace EchoCradle.Orchestration
{
    /// <summary>Native protocol v1 transport. Callbacks are worker-thread callbacks, not Unity callbacks.</summary>
    public sealed partial class OrchestratorClient : IDisposable
    {
        private readonly HttpClient _http;
        private readonly CancellationTokenSource _lifetime = new CancellationTokenSource();
        private readonly object _sync = new object();
        private readonly HashSet<string> _jobs = new HashSet<string>();
        private readonly SemaphoreSlim _sessionGate = new SemaphoreSlim(1, 1);
        private OrchestratorSession _session;
        private Task _close;
        private string _instance;
        internal Uri Origin { get; }
        internal string Token { get; }
        internal string Instance => _instance;
        internal CancellationToken Lifetime => _lifetime.Token;

        private OrchestratorClient(string url, string token)
        {
            Origin = OrchestratorWire.Origin(url);
            OrchestratorWire.Token(token);
            Token = token;
            _http = new HttpClient(new HttpClientHandler
            {
                UseProxy = false, AllowAutoRedirect = false, UseCookies = false
            }) { BaseAddress = Origin, Timeout = Timeout.InfiniteTimeSpan };
            _http.DefaultRequestHeaders.Authorization = new AuthenticationHeaderValue("Bearer", token);
        }

        public static Task<OrchestratorClient> ConnectAsync(string url, string token,
            CancellationToken cancellationToken = default)
        {
            return Task.Run(async () =>
            {
                OrchestratorClient client = null;
                try
                {
                    client = new OrchestratorClient(url, token);
                    using (var startup = CancellationTokenSource.CreateLinkedTokenSource(cancellationToken))
                    {
                        startup.CancelAfter(TimeSpan.FromSeconds(120));
                        while (true)
                        {
                            startup.Token.ThrowIfCancellationRequested();
                            JObject ready;
                            try
                            {
                                ready = await client.JsonAsync(HttpMethod.Get, "/v1/health/ready", null,
                                    startup.Token, true, true).ConfigureAwait(false);
                            }
                            catch (OrchestratorException error) when (error.Code == "TRANSPORT_ERROR")
                            {
                                await Task.Delay(100, startup.Token).ConfigureAwait(false);
                                continue;
                            }
                            string state = OrchestratorWire.Text(ready["state"], 32);
                            if (state == "failed" || state == "draining" || state == "stopped")
                                throw new OrchestratorException("NOT_READY");
                            if (state == "ready" && ready["ready"]?.Type == JTokenType.Boolean &&
                                (bool)ready["ready"]) break;
                            await Task.Delay(100, startup.Token).ConfigureAwait(false);
                        }
                        await client.CapabilitiesAsync(startup.Token).ConfigureAwait(false);
                    }
                    return client;
                }
                catch (OperationCanceledException)
                {
                    if (client != null) await client.CloseAsync().ConfigureAwait(false);
                    if (cancellationToken.IsCancellationRequested) throw;
                    throw new OrchestratorException("STARTUP_TIMEOUT");
                }
                catch (OrchestratorException)
                {
                    if (client != null) await client.CloseAsync().ConfigureAwait(false);
                    throw;
                }
                catch (Exception)
                {
                    if (client != null) await client.CloseAsync().ConfigureAwait(false);
                    throw new OrchestratorException("CONNECT_FAILED");
                }
            });
        }

        private void Identity(JObject data)
        {
            string instance = OrchestratorWire.Id(data["service_instance_id"]);
            OrchestratorWire.Integer(data["protocol_version"], 1, 1);
            lock (_sync)
            {
                if (_instance == null) _instance = instance;
                if (_instance != instance) throw new OrchestratorException("INSTANCE_CHANGED");
            }
        }

        internal static async Task<byte[]> ReadBoundedAsync(Stream stream, int limit, CancellationToken token)
        {
            // Unity/Mono streams can ignore ReadAsync's token. Closing the stream interrupts
            // the pending native read; cancellation still remains the primary outcome.
            using (token.Register(() => { try { stream.Dispose(); } catch (Exception) { } }))
            using (var output = new MemoryStream())
            {
                byte[] buffer = new byte[8192];
                while (true)
                {
                    int count;
                    try
                    {
                        count = await stream.ReadAsync(buffer, 0,
                            Math.Min(buffer.Length, limit + 1 - (int)output.Length), token).ConfigureAwait(false);
                    }
                    catch (Exception) { token.ThrowIfCancellationRequested(); throw; }
                    token.ThrowIfCancellationRequested();
                    if (count == 0) return output.ToArray();
                    output.Write(buffer, 0, count);
                    if (output.Length > limit) throw new OrchestratorException("PAYLOAD_LIMIT");
                }
            }
        }

        internal async Task<HttpResponseMessage> ResponseAsync(HttpMethod method, string path,
            HttpContent content, CancellationToken token)
        {
            // Each request has its own bound, even during cleanup. No arbitrary remote paths accepted.
            using (var timeout = CancellationTokenSource.CreateLinkedTokenSource(token))
            using (var request = new HttpRequestMessage(method, path) { Content = content })
            {
                timeout.CancelAfter(TimeSpan.FromSeconds(5));
                try
                {
                    return await _http.SendAsync(request, HttpCompletionOption.ResponseHeadersRead,
                        timeout.Token).ConfigureAwait(false);
                }
                catch (OperationCanceledException)
                {
                    token.ThrowIfCancellationRequested();
                    throw new OrchestratorException("TRANSPORT_ERROR");
                }
                catch (Exception) { throw new OrchestratorException("TRANSPORT_ERROR"); }
            }
        }

        internal async Task<JObject> JsonAsync(HttpMethod method, string path, JObject payload,
            CancellationToken token, bool identity = false, bool allowUnready = false,
            byte[] pcm = null, byte[] binary = null, string mediaType = null)
        {
            HttpContent content = null;
            if (payload != null)
            {
                content = new ByteArrayContent(OrchestratorWire.Encode(payload));
                content.Headers.ContentType = new MediaTypeHeaderValue("application/json");
            }
            else if (binary != null)
            {
                content = new ByteArrayContent(binary);
                content.Headers.ContentType = new MediaTypeHeaderValue(mediaType);
            }
            else if (pcm != null)
            {
                content = new ByteArrayContent(pcm);
                content.Headers.ContentType = new MediaTypeHeaderValue("application/octet-stream");
                content.Headers.Add("X-Sample-Rate", "16000");
            }
            using (var timeout = CancellationTokenSource.CreateLinkedTokenSource(token))
            {
                timeout.CancelAfter(TimeSpan.FromSeconds(5));
                try
                {
                    using (HttpResponseMessage response = await ResponseAsync(method, path, content,
                        timeout.Token).ConfigureAwait(false))
                    using (timeout.Token.Register(() => response.Dispose()))
                    using (Stream stream = await response.Content.ReadAsStreamAsync().ConfigureAwait(false))
                    {
                        byte[] bytes = await ReadBoundedAsync(stream, OrchestratorWire.JsonLimit,
                            timeout.Token).ConfigureAwait(false);
                        if (!response.IsSuccessStatusCode && !(allowUnready && (int)response.StatusCode == 503))
                        {
                            JObject failure;
                            try { failure = OrchestratorWire.Decode(bytes); }
                            catch (OrchestratorException)
                            { throw new OrchestratorException("HTTP_ERROR", (int)response.StatusCode, phase: Phase(path)); }
                            throw OrchestratorWire.Error(failure["error"] as JObject,
                                (int)response.StatusCode, Phase(path));
                        }
                        JObject result = OrchestratorWire.Decode(bytes);
                        if (identity) Identity(result);
                        return result;
                    }
                }
                catch (OperationCanceledException)
                {
                    token.ThrowIfCancellationRequested();
                    throw new OrchestratorException("TRANSPORT_ERROR");
                }
                catch (OrchestratorException) { throw; }
                catch (Exception)
                {
                    token.ThrowIfCancellationRequested();
                    throw new OrchestratorException("TRANSPORT_ERROR");
                }
            }
        }

        private static string Phase(string path) => path == "/v1/jobs" ? "admission" :
            path.StartsWith("/v1/artifacts") ? "artifact" : "transport";

        private static bool AdmissionRejected(OrchestratorException error) => error.Phase == "admission" &&
            (error.HttpStatus >= 400 && error.HttpStatus < 500 || error.HttpStatus == 503 && error.Code == "NOT_READY");

        /// <summary>Advanced admission only; the caller owns polling/cancellation of this job.</summary>
        public async Task<OrchestratorJob> SubmitAsync(JObject spec, CancellationToken token = default)
        {
            if (spec == null) throw new OrchestratorException("INVALID_SPEC");
            var copy = (JObject)spec.DeepClone();
            if (copy["idempotency_key"] == null) copy["idempotency_key"] = Guid.NewGuid().ToString("D");
            using (var linked = CancellationTokenSource.CreateLinkedTokenSource(token, Lifetime))
            {
                JObject data = await JsonAsync(HttpMethod.Post, "/v1/jobs", copy, linked.Token, true).ConfigureAwait(false);
                MatchSpec(data, copy);
                return new OrchestratorJob(data);
            }
        }

        private static void MatchSpec(JObject snapshot, JObject spec)
        {
            if (!JToken.DeepEquals(snapshot["session_id"], spec["session_id"] ?? JValue.CreateNull()) ||
                !JToken.DeepEquals(snapshot["turn_id"], spec["turn_id"] ?? JValue.CreateNull()) ||
                !JToken.DeepEquals(snapshot["kind"], spec["kind"]))
                throw new OrchestratorException("PROTOCOL_ERROR");
        }

        public async Task<JObject> CapabilitiesAsync(CancellationToken cancellationToken = default)
        {
            using (var linked = CancellationTokenSource.CreateLinkedTokenSource(cancellationToken, Lifetime))
            {
                JObject data = await JsonAsync(HttpMethod.Get, "/v1/capabilities", null,
                    linked.Token, true).ConfigureAwait(false);
                if (!(data["kinds"] is JObject) || !(data["probe"] is JObject))
                    throw new OrchestratorException("PROTOCOL_ERROR");
                return data;
            }
        }

        public async Task OpenSessionAsync(JArray tools, Func<string, JObject, JObject> toolHandler,
            CancellationToken cancellationToken = default)
        {
            using (var linked = CancellationTokenSource.CreateLinkedTokenSource(cancellationToken, Lifetime))
            {
                await _sessionGate.WaitAsync(linked.Token).ConfigureAwait(false);
                try
                {
                    if (_session != null) throw new OrchestratorException("SESSION_EXISTS");
                    _session = await OrchestratorSession.OpenAsync(this, tools, toolHandler,
                        linked.Token).ConfigureAwait(false);
                }
                finally { _sessionGate.Release(); }
            }
        }

        public Task<OrchestratorJob> TextAsync(JArray messages, int maxTokens, float temperature, int deadlineMs,
            Action<string> onDelta, CancellationToken cancellationToken = default)
        {
            // Snapshot mutable caller data before handing it to the worker. Do not mutate during this call.
            JArray copy = messages == null ? null : (JArray)messages.DeepClone();
            return Task.Run(async () =>
            {
                OrchestratorSession session = _session;
                if (session == null) throw new OrchestratorException("NO_SESSION");
                return await session.TextAsync(copy, maxTokens, temperature, deadlineMs,
                    onDelta, cancellationToken).ConfigureAwait(false);
            });
        }

        public async Task ResetSessionAsync(JArray tools, Func<string, JObject, JObject> toolHandler,
            CancellationToken cancellationToken = default)
        {
            using (var linked = CancellationTokenSource.CreateLinkedTokenSource(cancellationToken, Lifetime))
            {
                await _sessionGate.WaitAsync(linked.Token).ConfigureAwait(false);
                try
                {
                    if (_session != null)
                    {
                        using (var cleanup = new CancellationTokenSource(TimeSpan.FromSeconds(3)))
                            await _session.CloseAsync(cleanup.Token).ConfigureAwait(false);
                        _session = null;
                    }
                    _session = await OrchestratorSession.OpenAsync(this, tools, toolHandler,
                        linked.Token).ConfigureAwait(false);
                }
                finally { _sessionGate.Release(); }
            }
        }

        internal async Task<JObject> RunJobAsync(JObject spec, CancellationToken token,
            Action<string> submitted = null, Action checkSocket = null, bool returnTerminal = false,
            Func<Task> admittedAsync = null)
        {
            string id = null;
            bool terminal = false;
            bool rejected = false;
            JObject latest = null;
            var clock = System.Diagnostics.Stopwatch.StartNew();
            string kind = (string)spec["kind"];
            bool visual = kind == "image" || kind == "image_to_3d" || kind == "animate_3d";
            int deadlineMs = OrchestratorWire.Integer(spec["deadline_ms"], 1000, visual ? 3600000 : 120000);
            if (spec["idempotency_key"] == null) spec["idempotency_key"] = Guid.NewGuid().ToString("D");
            using (var deadline = CancellationTokenSource.CreateLinkedTokenSource(token, Lifetime))
            {
                deadline.CancelAfter(deadlineMs);
                try
                {
                    JObject snapshot = await JsonAsync(HttpMethod.Post, "/v1/jobs", spec,
                        deadline.Token, true).ConfigureAwait(false);
                    id = OrchestratorWire.Id(snapshot["job_id"]);
                    new OrchestratorJob(snapshot);
                    latest = snapshot;
                    lock (_sync) _jobs.Add(id);
                    MatchSpec(snapshot, spec);
                    submitted?.Invoke(id);
                    if (admittedAsync != null) await admittedAsync().ConfigureAwait(false);
                    while (true)
                    {
                        deadline.Token.ThrowIfCancellationRequested();
                        checkSocket?.Invoke();
                        var job = new OrchestratorJob(snapshot);
                        string state = job.State;
                        if (state == "succeeded" || state == "failed" || state == "cancelled")
                        {
                            terminal = true;
                            if (returnTerminal) return snapshot;
                            if (state != "succeeded") throw job.Error ?? new OrchestratorException("JOB_FAILED", jobId: id,
                                phase: "inference", snapshot: job);
                            if (!(snapshot["result"] is JObject result))
                                throw new OrchestratorException("PROTOCOL_ERROR");
                            return result;
                        }
                        if (state != "queued" && state != "running" && state != "waiting_tool")
                            throw new OrchestratorException("PROTOCOL_ERROR");
                        await Task.Delay(50, deadline.Token).ConfigureAwait(false);
                        snapshot = await SnapshotAsync(id, deadline.Token).ConfigureAwait(false);
                        MatchSpec(snapshot, spec);
                        if ((int)snapshot["revision"] < (int)latest["revision"])
                            throw new OrchestratorException("PROTOCOL_ERROR");
                        latest = snapshot;
                    }
                }
                catch (OperationCanceledException)
                {
                    int remaining = deadlineMs - (int)clock.ElapsedMilliseconds;
                    if (id != null && remaining > 0 && !Lifetime.IsCancellationRequested)
                    {
                        // Caller cancellation revokes callbacks immediately; use the remaining total
                        // job budget to obtain the authoritative cancellation snapshot.
                        using (var recovery = CancellationTokenSource.CreateLinkedTokenSource(Lifetime))
                        {
                            recovery.CancelAfter(Math.Min(1000, remaining));
                            if (remaining > 0)
                            {
                                try
                                {
                                    JObject cancelled = await CancelJobAsync(id, recovery.Token).ConfigureAwait(false);
                                    terminal = true;
                                    if (returnTerminal) return cancelled;
                                }
                                catch (Exception) { }
                            }
                        }
                    }
                    token.ThrowIfCancellationRequested();
                    Lifetime.ThrowIfCancellationRequested();
                    throw new OrchestratorException("DEADLINE_EXCEEDED", jobId: id, phase: "operation",
                        snapshot: latest == null ? null : new OrchestratorJob(latest));
                }
                catch (OrchestratorException error)
                {
                    rejected = AdmissionRejected(error);
                    throw;
                }
                finally
                {
                    if (!terminal && !rejected)
                    {
                        // A cancelled POST can have been admitted. Recover its ID using the SAME
                        // idempotency key, then cancel. Session deletion is the second safety net.
                        _ = RecoverAndCancelAsync(id, spec);
                    }
                    if (id != null) lock (_sync) _jobs.Remove(id);
                }
            }
        }

        private async Task RecoverAndCancelAsync(string id, JObject spec)
        {
            using (var cleanup = new CancellationTokenSource(TimeSpan.FromSeconds(3)))
            {
                try
                {
                    if (id == null)
                    {
                        JObject recovered = await JsonAsync(HttpMethod.Post, "/v1/jobs", spec,
                            cleanup.Token, true).ConfigureAwait(false);
                        new OrchestratorJob(recovered);
                        id = OrchestratorWire.Id(recovered["job_id"]);
                    }
                    await CancelQuietlyAsync(id, cleanup.Token).ConfigureAwait(false);
                }
                catch (Exception) { }
            }
        }

        internal async Task<JObject> SnapshotAsync(string id, CancellationToken token)
        {
            JObject result = await JsonAsync(HttpMethod.Get, "/v1/jobs/" + id, null,
                token, true).ConfigureAwait(false);
            if (OrchestratorWire.Id(result["job_id"]) != id)
                throw new OrchestratorException("PROTOCOL_ERROR");
            new OrchestratorJob(result);
            return result;
        }

        public async Task<OrchestratorJob> GetJobAsync(string id, CancellationToken token = default)
        {
            OrchestratorWire.Id(new JValue(id));
            using (var linked = CancellationTokenSource.CreateLinkedTokenSource(token, Lifetime))
                return new OrchestratorJob(await SnapshotAsync(id, linked.Token).ConfigureAwait(false));
        }

        internal async Task<JObject> CancelJobAsync(string id, CancellationToken token)
        {
            OrchestratorWire.Id(new JValue(id));
            JObject result = await JsonAsync(HttpMethod.Post, "/v1/jobs/" + id + "/cancel", null,
                token, true).ConfigureAwait(false);
            var snapshot = new OrchestratorJob(result);
            if (snapshot.JobId != id || !snapshot.IsTerminal) throw new OrchestratorException("PROTOCOL_ERROR");
            return result;
        }

        public async Task<OrchestratorJob> CancelAsync(string id, CancellationToken token = default)
        {
            using (var linked = CancellationTokenSource.CreateLinkedTokenSource(token, Lifetime))
                return new OrchestratorJob(await CancelJobAsync(id, linked.Token).ConfigureAwait(false));
        }

        private async Task CancelQuietlyAsync(string id, CancellationToken token)
        {
            try { await CancelJobAsync(id, token).ConfigureAwait(false); }
            catch (Exception) { }
        }

        internal async Task DeleteArtifactQuietlyAsync(string id, CancellationToken token)
        {
                try { await JsonAsync(HttpMethod.Delete, "/v1/artifacts/" + id, null, token).ConfigureAwait(false); }
                catch (Exception) { }
        }

        public async Task<string> TranscribeAsync(byte[] pcm, CancellationToken cancellationToken = default)
        {
            if (pcm == null || pcm.Length == 0 || pcm.Length > OrchestratorWire.PcmLimit || pcm.Length % 2 != 0)
                throw new OrchestratorException("INVALID_PCM");
            byte[] copy = (byte[])pcm.Clone();
            using (var linked = CancellationTokenSource.CreateLinkedTokenSource(cancellationToken, Lifetime))
            {
                string artifact = null;
                bool consumed = false;
                bool definiteRejection = false;
                linked.CancelAfter(120000);
                try
                {
                    JObject upload = await JsonAsync(HttpMethod.Post, "/v1/artifacts", null, linked.Token,
                        pcm: copy).ConfigureAwait(false);
                    artifact = OrchestratorWire.Id(upload["artifact_id"]);
                    JObject result = await RunJobAsync(new JObject
                    {
                        ["kind"] = "stt", ["input_artifact_id"] = artifact, ["deadline_ms"] = 120000
                    }, linked.Token, _ => consumed = true).ConfigureAwait(false);
                    return OrchestratorWire.Text(result["text"]);
                }
                catch (OrchestratorException error)
                {
                    definiteRejection = AdmissionRejected(error);
                    throw;
                }
                finally
                {
                    // Ambiguous POST failures may have consumed/admitted input. Never delete then.
                    if (!consumed && definiteRejection && artifact != null)
                        await DeleteArtifactQuietlyAsync(artifact, linked.Token).ConfigureAwait(false);
                }
            }
        }

        public Task<PcmAudio> SynthesizeAsync(string text, CancellationToken cancellationToken = default)
        {
            return Task.Run(async () =>
            {
                if (string.IsNullOrWhiteSpace(text) || text.Length > 300)
                    throw new OrchestratorException("INVALID_TTS_TEXT");
                using (var linked = CancellationTokenSource.CreateLinkedTokenSource(cancellationToken, Lifetime))
                {
                    linked.CancelAfter(120000); // Total: admission, polling, download, verification and cleanup.
                    JObject result = await RunJobAsync(new JObject
                    {
                        ["kind"] = "tts", ["text"] = text, ["deadline_ms"] = 120000
                    }, linked.Token).ConfigureAwait(false);
                    string artifact = OrchestratorWire.Id(result["artifact_id"]);
                    try
                    {
                        int rate = OrchestratorWire.Integer(result["sample_rate"], 1, 192000);
                        int count = OrchestratorWire.Integer(result["sample_count"], 0,
                            Math.Min(rate * 20, OrchestratorWire.PcmLimit / 2));
                        string expected = OrchestratorWire.Text(result["sha256"], 64);
                        await JsonAsync(HttpMethod.Get, "/v1/health/live", null, linked.Token, true).ConfigureAwait(false);
                        using (var download = CancellationTokenSource.CreateLinkedTokenSource(linked.Token))
                        {
                            download.CancelAfter(TimeSpan.FromSeconds(5));
                            using (HttpResponseMessage response = await ResponseAsync(HttpMethod.Get,
                                "/v1/artifacts/" + artifact, null, download.Token).ConfigureAwait(false))
                            using (download.Token.Register(() => response.Dispose()))
                            using (Stream stream = await response.Content.ReadAsStreamAsync().ConfigureAwait(false))
                            {
                                byte[] bytes = await ReadBoundedAsync(stream, OrchestratorWire.PcmLimit,
                                    download.Token).ConfigureAwait(false);
                                string digest;
                                using (SHA256 sha = SHA256.Create())
                                    digest = BitConverter.ToString(sha.ComputeHash(bytes)).Replace("-", "").ToLowerInvariant();
                                if (!response.IsSuccessStatusCode || bytes.Length != count * 2 || digest != expected ||
                                    Header(response, "X-Content-SHA256") != digest ||
                                    Header(response, "X-Sample-Rate") != rate.ToString(System.Globalization.CultureInfo.InvariantCulture) ||
                                    Header(response, "X-Sample-Count") != count.ToString(System.Globalization.CultureInfo.InvariantCulture))
                                    throw new OrchestratorException("ARTIFACT_INVALID");
                                await JsonAsync(HttpMethod.Get, "/v1/health/live", null,
                                    linked.Token, true).ConfigureAwait(false);
                                return new PcmAudio(bytes, rate);
                            }
                        }
                    }
                    catch (OperationCanceledException) { throw; }
                    catch (OrchestratorException) { throw; }
                    catch (Exception)
                    {
                        linked.Token.ThrowIfCancellationRequested();
                        throw new OrchestratorException("ARTIFACT_INVALID");
                    }
                    finally { await DeleteArtifactQuietlyAsync(artifact, linked.Token).ConfigureAwait(false); }
                }
            });
        }

        private static string Header(HttpResponseMessage response, string name)
        {
            if (!response.Headers.TryGetValues(name, out IEnumerable<string> values)) return null;
            string result = null;
            foreach (string value in values)
            {
                if (result != null) return null;
                result = value;
            }
            return result;
        }

        internal Task ShutdownAsync(CancellationToken token) =>
            JsonAsync(HttpMethod.Post, "/v1/control/shutdown", null, token, true);

        public Task CloseAsync()
        {
            lock (_sync)
            {
                if (_close == null)
                {
                    _lifetime.Cancel();
                    _session?.Abort();
                    _close = Task.Run(async () =>
                    {
                        using (var cleanup = new CancellationTokenSource(TimeSpan.FromSeconds(5)))
                        {
                            string[] jobs;
                            lock (_sync) { jobs = new string[_jobs.Count]; _jobs.CopyTo(jobs); }
                            foreach (string id in jobs) await CancelQuietlyAsync(id, cleanup.Token).ConfigureAwait(false);
                            // Wait for an in-flight session creation to finish its own cleanup.
                            try { await _sessionGate.WaitAsync(cleanup.Token).ConfigureAwait(false); }
                            catch (OperationCanceledException) { _http.Dispose(); return; }
                            try
                            {
                                if (_session != null) await _session.CloseAsync(cleanup.Token).ConfigureAwait(false);
                            }
                            finally { _sessionGate.Release(); }
                        }
                        _http.Dispose();
                    });
                }
                return _close;
            }
        }

        /// <summary>Nonblocking: revokes callbacks now and schedules bounded remote cleanup.</summary>
        public void Dispose() { _ = CloseAsync(); }
    }
}