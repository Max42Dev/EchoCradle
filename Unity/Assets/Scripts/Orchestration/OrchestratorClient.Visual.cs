using System;
using System.IO;
using System.Net.Http;
using System.Security.Cryptography;
using System.Threading;
using System.Threading.Tasks;
using Newtonsoft.Json.Linq;

namespace EchoCradle.Orchestration
{
    public sealed partial class OrchestratorClient
    {
        public Task<OrchestratorVisualResult> GenerateImageAsync(string text, uint seed,
            string negativePrompt = "", int deadlineMs = 600000, CancellationToken token = default)
        {
            return VisualAsync(new JObject { ["kind"] = "image", ["text"] = text,
                ["negative_prompt"] = negativePrompt, ["seed"] = seed, ["deadline_ms"] = deadlineMs }, null, null, token);
        }

        public Task<OrchestratorVisualResult> Generate3DAsync(byte[] png, uint seed,
            int deadlineMs = 600000, CancellationToken token = default) =>
            VisualAsync(new JObject { ["kind"] = "image_to_3d", ["seed"] = seed,
                ["deadline_ms"] = deadlineMs }, png, "image/png", token);

        public Task<OrchestratorVisualResult> Animate3DAsync(byte[] glb, uint seed,
            int deadlineMs = 600000, CancellationToken token = default) =>
            VisualAsync(new JObject { ["kind"] = "animate_3d", ["seed"] = seed,
                ["deadline_ms"] = deadlineMs }, glb, "model/gltf-binary", token);

        public async Task ReleaseArtifactAsync(string artifactId, CancellationToken token = default)
        {
            OrchestratorWire.Id(new JValue(artifactId));
            using (var linked = CancellationTokenSource.CreateLinkedTokenSource(token, Lifetime))
            {
                await JsonAsync(HttpMethod.Get, "/v1/health/live", null, linked.Token, true).ConfigureAwait(false);
                JObject result = await JsonAsync(HttpMethod.Delete, "/v1/artifacts/" + artifactId,
                    null, linked.Token).ConfigureAwait(false);
                if (OrchestratorWire.Id(result["artifact_id"]) != artifactId || (string)result["state"] != "deleted")
                    throw new OrchestratorException("PROTOCOL_ERROR");
            }
        }

        private Task<OrchestratorVisualResult> VisualAsync(JObject spec, byte[] input, string inputMedia,
            CancellationToken token)
        {
            if (inputMedia != null && (input == null || input.Length == 0 || input.Length > VisualContainer.Limit))
                throw new OrchestratorException("ARTIFACT_INVALID");
            // Snapshot before scheduling any worker; later caller mutations cannot alter upload bytes.
            byte[] copy = input == null ? null : (byte[])input.Clone();
            return Task.Run(async () =>
            {
                token.ThrowIfCancellationRequested();
                int deadlineMs = OrchestratorWire.Integer(spec["deadline_ms"], 1000, 3600000);
                if ((string)spec["kind"] == "image" &&
                    (string.IsNullOrEmpty((string)spec["text"]) || ((string)spec["text"]).Length > 16384 ||
                    spec["negative_prompt"].Type != JTokenType.String || ((string)spec["negative_prompt"]).Length > 16384))
                    throw new OrchestratorException("INVALID_SPEC");
                if (copy != null) VisualContainer.Validate(copy, inputMedia);
                OrchestratorWire.Encode(spec); // Check bounds before allocating server storage.
                using (var operation = CancellationTokenSource.CreateLinkedTokenSource(token, Lifetime))
                {
                    operation.CancelAfter(deadlineMs);
                    string uploaded = null;
                    try
                    {
                        operation.Token.ThrowIfCancellationRequested();
                        if (copy != null)
                        {
                            JObject upload = await JsonAsync(HttpMethod.Post, "/v1/artifacts", null,
                                operation.Token, binary: copy, mediaType: inputMedia).ConfigureAwait(false);
                            uploaded = OrchestratorWire.Id(upload["artifact_id"]);
                            spec["input_artifact_id"] = uploaded;
                        }
                        JObject result = await RunJobAsync(spec, operation.Token,
                            admittedAsync: () => CleanupVisualUploadAsync(uploaded)).ConfigureAwait(false);
                        string artifact = OrchestratorWire.Id(result["artifact_id"]);
                        string media = (string)spec["kind"] == "image" ? "image/png" : "model/gltf-binary";
                        int length = OrchestratorWire.Integer(result["byte_length"], 1, VisualContainer.Limit);
                        string digest = OrchestratorWire.Text(result["sha256"], 64);
                        if ((string)result["media_type"] != media || digest.Length != 64 ||
                            result["seed"]?.Type != JTokenType.Integer || !JToken.DeepEquals(result["seed"], spec["seed"]) ||
                            result["experimental"]?.Type != JTokenType.Boolean || !(bool)result["experimental"])
                            throw new OrchestratorException("ARTIFACT_INVALID");
                        foreach (char c in digest)
                            if (!(c >= '0' && c <= '9') && !(c >= 'a' && c <= 'f'))
                                throw new OrchestratorException("ARTIFACT_INVALID");
                        await JsonAsync(HttpMethod.Get, "/v1/health/live", null, operation.Token, true).ConfigureAwait(false);
                        byte[] bytes;
                        using (var download = CancellationTokenSource.CreateLinkedTokenSource(operation.Token))
                        {
                            download.CancelAfter(TimeSpan.FromSeconds(30));
                            using (HttpResponseMessage response = await ResponseAsync(HttpMethod.Get,
                                "/v1/artifacts/" + artifact, null, download.Token).ConfigureAwait(false))
                            using (download.Token.Register(() => response.Dispose()))
                            using (Stream stream = await response.Content.ReadAsStreamAsync().ConfigureAwait(false))
                            {
                                bytes = await ReadBoundedAsync(stream, length, download.Token).ConfigureAwait(false);
                                string actual;
                                using (SHA256 sha = SHA256.Create())
                                    actual = BitConverter.ToString(sha.ComputeHash(bytes)).Replace("-", "").ToLowerInvariant();
                                if ((int)response.StatusCode != 200 || bytes.Length != length || actual != digest ||
                                    Header(response, "X-Content-SHA256") != digest ||
                                    response.Content.Headers.ContentType?.ToString() != media ||
                                    response.Content.Headers.ContentLength.HasValue && response.Content.Headers.ContentLength.Value != length)
                                    throw new OrchestratorException("ARTIFACT_INVALID");
                            }
                        }
                        await JsonAsync(HttpMethod.Get, "/v1/health/live", null, operation.Token, true).ConfigureAwait(false);
                        VisualContainer.Validate(bytes, media);
                        operation.Token.ThrowIfCancellationRequested();
                        return new OrchestratorVisualResult(bytes, artifact, media, digest, (uint)spec["seed"]);
                    }
                    catch (OperationCanceledException)
                    {
                        token.ThrowIfCancellationRequested();
                        Lifetime.ThrowIfCancellationRequested();
                        throw new OrchestratorException("DEADLINE_EXCEEDED");
                    }
                    catch (OrchestratorException error)
                    {
                        if (AdmissionRejected(error)) await CleanupVisualUploadAsync(uploaded).ConfigureAwait(false);
                        throw;
                    }
                    catch (Exception)
                    {
                        token.ThrowIfCancellationRequested();
                        Lifetime.ThrowIfCancellationRequested();
                        throw new OrchestratorException("ARTIFACT_INVALID");
                    }
                }
            });
        }

        private async Task CleanupVisualUploadAsync(string id)
        {
            if (id == null) return;
            using (var cleanup = new CancellationTokenSource(TimeSpan.FromSeconds(3)))
                await DeleteArtifactQuietlyAsync(id, cleanup.Token).ConfigureAwait(false);
        }
    }
}