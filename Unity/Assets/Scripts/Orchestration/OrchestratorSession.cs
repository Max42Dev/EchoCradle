using System;
using System.Collections.Concurrent;
using System.Collections.Generic;
using System.IO;
using System.Net.Http;
using System.Net.WebSockets;
using System.Threading;
using System.Threading.Tasks;
using Newtonsoft.Json.Linq;

namespace EchoCradle.Orchestration
{
    // Internal socket authority. One turn at a time, one receiver and one serialized sender.
    internal sealed class OrchestratorSession
    {
        private readonly OrchestratorClient _client;
        private readonly string _id;
        private readonly Func<string, JObject, JObject> _handler;
        private readonly HashSet<string> _names;
        private readonly ClientWebSocket _socket = new ClientWebSocket();
        private readonly CancellationTokenSource _stop;
        private readonly SemaphoreSlim _send = new SemaphoreSlim(1, 1);
        private readonly SemaphoreSlim _turnGate = new SemaphoreSlim(1, 1);
        private readonly BlockingCollection<JObject> _events = new BlockingCollection<JObject>(64);
        private readonly object _sync = new object();
        private readonly HashSet<string> _seen = new HashSet<string>();
        private readonly TaskCompletionSource<bool> _hello = NewSignal();
        private TaskCompletionSource<bool> _end;
        private Task _reader, _consumer, _heartbeat;
        private CancellationTokenSource _turnStop;
        private string _turn, _job;
        private Action<string> _delta;
        private OrchestratorException _error;
        private long _sequence = 1;
        private int _queuedBytes, _textBytes;
        private bool _pendingTool;

        private static TaskCompletionSource<bool> NewSignal() =>
            new TaskCompletionSource<bool>(TaskCreationOptions.RunContinuationsAsynchronously);

        private OrchestratorSession(OrchestratorClient client, string id,
            Func<string, JObject, JObject> handler, HashSet<string> names)
        {
            _client = client;
            _id = id;
            _handler = handler;
            _names = names;
            _stop = CancellationTokenSource.CreateLinkedTokenSource(client.Lifetime);
            _socket.Options.Proxy = null;
            _socket.Options.SetRequestHeader("Authorization", "Bearer " + client.Token);
            _socket.Options.KeepAliveInterval = Timeout.InfiniteTimeSpan;
        }

        internal static async Task<OrchestratorSession> OpenAsync(OrchestratorClient client,
            JArray tools, Func<string, JObject, JObject> handler, CancellationToken token)
        {
            if (tools == null || tools.Count > 16 || (tools.Count > 0 && handler == null))
                throw new OrchestratorException("INVALID_TOOLS");
            var copy = (JArray)tools.DeepClone();
            OrchestratorWire.Encode(new JObject { ["tools"] = copy }, OrchestratorWire.ToolLimit);
            var names = new HashSet<string>();
            foreach (JToken declaration in copy)
            {
                if (!(declaration is JObject tool) || (string)tool["type"] != "function" ||
                    !(tool["function"] is JObject function) || !(function["parameters"] is JObject schema) ||
                    (string)schema["type"] != "object")
                    throw new OrchestratorException("INVALID_TOOLS");
                string name = OrchestratorWire.Text(function["name"], 64);
                if (name.Length == 0 || !names.Add(name)) throw new OrchestratorException("INVALID_TOOLS");
                foreach (char c in name)
                    if (!(c >= 'a' && c <= 'z') && !(c >= 'A' && c <= 'Z') &&
                        !(c >= '0' && c <= '9') && c != '_' && c != '-')
                        throw new OrchestratorException("INVALID_TOOLS");
                // The server validates the full schema; reject reference-bearing declarations locally too.
                foreach (JToken node in schema.DescendantsAndSelf())
                    if (node is JProperty property && (property.Name == "$ref" ||
                        property.Name == "$dynamicRef" || property.Name == "$recursiveRef"))
                        throw new OrchestratorException("INVALID_TOOLS");
            }
            JObject created = await client.JsonAsync(HttpMethod.Post, "/v1/sessions", new JObject
            {
                ["tools"] = copy, ["delivery_mode"] = "text"
            }, token, true).ConfigureAwait(false);
            string id = OrchestratorWire.Id(created["session_id"]);
            var session = new OrchestratorSession(client, id, handler, names);
            try
            {
                using (var timeout = CancellationTokenSource.CreateLinkedTokenSource(token, session._stop.Token))
                {
                    timeout.CancelAfter(TimeSpan.FromSeconds(5));
                    var uri = new UriBuilder(client.Origin)
                    {
                        Scheme = client.Origin.Scheme == "https" ? "wss" : "ws",
                        Path = "/v1/sessions/" + id + "/events"
                    };
                    await session._socket.ConnectAsync(uri.Uri, timeout.Token).ConfigureAwait(false);
                    session._reader = Task.Run(session.ReceiveAsync);
                    session._consumer = Task.Run(session.ConsumeAsync);
                    await session.SendAsync("hello", new JObject(), null, null, timeout.Token).ConfigureAwait(false);
                    await WaitAsync(session._hello.Task, timeout.Token).ConfigureAwait(false);
                    session.Check();
                    session._heartbeat = Task.Run(session.HeartbeatAsync);
                }
                return session;
            }
            catch (Exception error)
            {
                session.Abort();
                using (var cleanup = new CancellationTokenSource(TimeSpan.FromSeconds(3)))
                    await session.CloseAsync(cleanup.Token).ConfigureAwait(false);
                if (error is OperationCanceledException && token.IsCancellationRequested) throw;
                throw new OrchestratorException("SOCKET_NEGOTIATION_FAILED");
            }
        }

        private static async Task WaitAsync(Task task, CancellationToken token)
        {
            var cancelled = NewSignal();
            using (token.Register(() => cancelled.TrySetResult(true)))
            {
                if (await Task.WhenAny(task, cancelled.Task).ConfigureAwait(false) != task)
                    token.ThrowIfCancellationRequested();
                await task.ConfigureAwait(false);
            }
        }

        private void Fail(string code)
        {
            lock (_sync)
            {
                if (_error == null) _error = new OrchestratorException(code);
                _turnStop?.Cancel();
                _hello.TrySetResult(true);
                _end?.TrySetResult(true);
            }
            Abort();
        }

        internal void Check()
        {
            lock (_sync)
                if (_error != null) throw _error;
            _stop.Token.ThrowIfCancellationRequested();
        }

        private async Task SendAsync(string type, JObject payload, string job, string turn, CancellationToken token)
        {
            using (var timeout = CancellationTokenSource.CreateLinkedTokenSource(token, _stop.Token))
            {
                timeout.CancelAfter(TimeSpan.FromSeconds(5));
                await _send.WaitAsync(timeout.Token).ConfigureAwait(false);
                try
                {
                    byte[] bytes = OrchestratorWire.Encode(new JObject
                    {
                        ["v"] = 1, ["seq"] = _sequence++, ["type"] = type,
                        ["service_instance_id"] = _client.Instance, ["session_id"] = _id,
                        ["job_id"] = job, ["turn_id"] = turn, ["payload"] = payload
                    });
                    await _socket.SendAsync(new ArraySegment<byte>(bytes), WebSocketMessageType.Text,
                        true, timeout.Token).ConfigureAwait(false);
                }
                finally { _send.Release(); }
            }
        }

        private async Task HeartbeatAsync()
        {
            try
            {
                while (true)
                {
                    await Task.Delay(5000, _stop.Token).ConfigureAwait(false);
                    await SendAsync("ping", new JObject(), null, null, _stop.Token).ConfigureAwait(false);
                }
            }
            catch (OperationCanceledException) { if (!_stop.IsCancellationRequested) Fail("SOCKET_LOST"); }
            catch (Exception) { Fail("SOCKET_LOST"); }
        }

        private async Task ReceiveAsync()
        {
            long sequence = 1;
            bool accepted = false;
            byte[] buffer = new byte[8192];
            try
            {
                while (!_stop.IsCancellationRequested)
                {
                    using (var timeout = CancellationTokenSource.CreateLinkedTokenSource(_stop.Token))
                    using (var message = new MemoryStream())
                    {
                        timeout.CancelAfter(TimeSpan.FromSeconds(15));
                        WebSocketReceiveResult chunk;
                        do
                        {
                            chunk = await _socket.ReceiveAsync(new ArraySegment<byte>(buffer),
                                timeout.Token).ConfigureAwait(false);
                            if (chunk.MessageType != WebSocketMessageType.Text)
                                throw new OrchestratorException("SOCKET_LOST");
                            if (message.Length + chunk.Count > OrchestratorWire.JsonLimit)
                                throw new OrchestratorException("PAYLOAD_LIMIT");
                            message.Write(buffer, 0, chunk.Count);
                        } while (!chunk.EndOfMessage);
                        JObject data = OrchestratorWire.Decode(message.ToArray());
                        OrchestratorWire.Integer(data["v"], 1, 1);
                        if (data["seq"]?.Type != JTokenType.Integer || (long)data["seq"] != sequence++ ||
                            OrchestratorWire.Id(data["service_instance_id"]) != _client.Instance ||
                            OrchestratorWire.Id(data["session_id"]) != _id || !(data["payload"] is JObject payload))
                            throw new OrchestratorException("PROTOCOL_ERROR");
                        string type = OrchestratorWire.Text(data["type"], 64);
                        if (!accepted)
                        {
                            if (type != "hello.accepted") throw new OrchestratorException("PROTOCOL_ERROR");
                            OrchestratorWire.Integer(payload["protocol_version"], 1, 1);
                            accepted = true;
                            _hello.TrySetResult(true);
                            continue;
                        }
                        if (type == "pong") continue;
                        if (type == "error") throw new OrchestratorException("SOCKET_PROTOCOL_ERROR");
                        if (type != "text.delta" && type != "text.end" && type != "tool.call" && type != "job.state")
                            throw new OrchestratorException("PROTOCOL_ERROR");
                        lock (_sync)
                        {
                            string job = OrchestratorWire.Id(data["job_id"]);
                            if (_turn == null || (string)data["turn_id"] != _turn || (_job != null && _job != job))
                                throw new OrchestratorException("PROTOCOL_ERROR");
                            _job = job; // Socket events can arrive before the REST admission response.
                            if (type == "job.state")
                            {
                                if ((string)payload["job_id"] != job || (string)payload["turn_id"] != _turn ||
                                    (string)payload["session_id"] != _id)
                                    throw new OrchestratorException("PROTOCOL_ERROR");
                                string state = OrchestratorWire.Text(payload["state"], 32);
                                if (state == "failed" || state == "cancelled") _turnStop?.Cancel();
                            }
                            if (type == "tool.call")
                            {
                                string call = OrchestratorWire.Id(payload["call_id"]);
                                if (!_names.Contains(OrchestratorWire.Text(payload["name"], 64)) ||
                                    !(payload["arguments"] is JObject) || _pendingTool ||
                                    _seen.Count >= 8 || !_seen.Add(call))
                                    throw new OrchestratorException("PROTOCOL_ERROR");
                                OrchestratorWire.Integer(payload["timeout_ms"], 5000, 5000);
                                OrchestratorWire.Encode(payload["arguments"], OrchestratorWire.ToolLimit);
                                _pendingTool = true;
                                // Monotonic local timestamp includes time spent waiting in our event queue.
                                data["received_ticks"] = System.Diagnostics.Stopwatch.GetTimestamp();
                            }
                            if (type == "text.delta")
                            {
                                string delta = OrchestratorWire.Text(payload["text"]);
                                _textBytes += OrchestratorWire.Utf8.GetByteCount(delta);
                                if (_textBytes > 24576) throw new OrchestratorException("PAYLOAD_LIMIT");
                            }
                            int size = OrchestratorWire.Encode(data).Length;
                            if (_queuedBytes + size > 262144 || !_events.TryAdd(data))
                                throw new OrchestratorException("SLOW_CONSUMER");
                            _queuedBytes += size;
                        }
                    }
                }
            }
            catch (OperationCanceledException) { if (!_stop.IsCancellationRequested) Fail("SOCKET_TIMEOUT"); }
            catch (OrchestratorException error) { Fail(error.Code); }
            catch (Exception) { Fail("SOCKET_LOST"); }
        }

        private async Task ConsumeAsync()
        {
            try
            {
                foreach (JObject data in _events.GetConsumingEnumerable(_stop.Token))
                {
                    CancellationToken token;
                    Action<string> callback;
                    lock (_sync)
                    {
                        _queuedBytes -= OrchestratorWire.Encode(data).Length;
                        if (_turnStop == null || _turnStop.IsCancellationRequested) continue;
                        token = _turnStop.Token;
                        callback = _delta;
                    }
                    JObject payload = (JObject)data["payload"];
                    switch ((string)data["type"])
                    {
                        case "text.delta":
                            // Hand-off only: a slow/throwing callback fails the turn, not the reader.
                            callback?.Invoke((string)payload["text"]);
                            break;
                        case "job.state":
                            if ((string)payload["state"] == "succeeded")
                                lock (_sync) _end?.TrySetResult(true);
                            break;
                        case "tool.call":
                            await InvokeToolAsync(data, token).ConfigureAwait(false);
                            break;
                    }
                }
            }
            catch (OperationCanceledException) { }
            catch (OrchestratorException error) { Fail(error.Code); }
            catch (Exception) { Fail("CALLBACK_FAILED"); }
        }

        private async Task InvokeToolAsync(JObject data, CancellationToken token)
        {
            JObject payload = (JObject)data["payload"];
            long ticks = (long)data["received_ticks"];
            double elapsed = (System.Diagnostics.Stopwatch.GetTimestamp() - ticks) * 1000.0 /
                System.Diagnostics.Stopwatch.Frequency;
            int remaining = Math.Max(0, 4900 - (int)elapsed);
            if (remaining == 0) throw new OrchestratorException("TOOL_TIMEOUT");
            using (var timeout = CancellationTokenSource.CreateLinkedTokenSource(token))
            {
                timeout.CancelAfter(remaining);
                JObject snapshot = await _client.SnapshotAsync((string)data["job_id"], timeout.Token).ConfigureAwait(false);
                if ((string)snapshot["state"] != "waiting_tool")
                    throw new OrchestratorException("STALE_TOOL_CALL");
                timeout.Token.ThrowIfCancellationRequested();
                // A synchronous authority cannot be forcibly stopped. It MUST be short and thread-safe;
                // cancellation revokes delivery, not an already-dispatched side effect.
                Task<JObject> invocation = Task.Run(() =>
                {
                    try
                    {
                        timeout.Token.ThrowIfCancellationRequested();
                        JObject result = _handler((string)payload["name"], (JObject)payload["arguments"].DeepClone());
                        if (result == null) throw new OrchestratorException("INVALID_TOOL_RESULT");
                        return (JObject)result.DeepClone();
                    }
                    catch (Exception) { return null; } // Also observes late handler failures.
                });
                try { await WaitAsync(invocation, timeout.Token).ConfigureAwait(false); }
                catch (OperationCanceledException)
                {
                    token.ThrowIfCancellationRequested();
                    throw new OrchestratorException("TOOL_TIMEOUT");
                }
                JObject answer = await invocation.ConfigureAwait(false);
                if (answer == null) throw new OrchestratorException("TOOL_HANDLER_FAILED");
                OrchestratorWire.Encode(answer, OrchestratorWire.ToolLimit);
                timeout.Token.ThrowIfCancellationRequested();
                // Release the pending flag before send completes: the next sequential call can race send completion.
                lock (_sync) _pendingTool = false;
                await SendAsync("tool.result", new JObject
                {
                    ["call_id"] = payload["call_id"], ["result"] = answer
                }, (string)data["job_id"], (string)data["turn_id"], timeout.Token).ConfigureAwait(false);
            }
        }

        internal async Task<string> DialogueAsync(JArray messages, int maxTokens, float temperature,
            int deadlineMs, Action<string> onDelta, CancellationToken token)
        {
            if (messages == null || messages.Count == 0 || messages.Count > 64 || maxTokens < 1 ||
                maxTokens > 512 || float.IsNaN(temperature) || float.IsInfinity(temperature) ||
                temperature < 0 || temperature > 2 || deadlineMs < 1000 || deadlineMs > 120000)
                throw new OrchestratorException("INVALID_DIALOGUE");
            foreach (JToken item in messages)
            {
                if (!(item is JObject message) || message.Count != 2 ||
                    (string)message["role"] != "system" && (string)message["role"] != "user" &&
                    (string)message["role"] != "assistant") throw new OrchestratorException("INVALID_DIALOGUE");
                OrchestratorWire.Text(message["content"], 16384);
            }
            using (var linked = CancellationTokenSource.CreateLinkedTokenSource(token, _stop.Token))
            {
                await _turnGate.WaitAsync(linked.Token).ConfigureAwait(false);
                bool succeeded = false;
                try
                {
                    Check();
                    lock (_sync)
                    {
                        _turn = Guid.NewGuid().ToString("D");
                        _job = null;
                        _seen.Clear();
                        _pendingTool = false;
                        _textBytes = 0;
                        _delta = onDelta;
                        _end = NewSignal();
                        _turnStop = CancellationTokenSource.CreateLinkedTokenSource(linked.Token);
                        _turnStop.CancelAfter(deadlineMs);
                    }
                    JObject result = await _client.RunJobAsync(new JObject
                    {
                        ["kind"] = "dialogue", ["session_id"] = _id, ["turn_id"] = _turn,
                        ["messages"] = messages, ["max_tokens"] = maxTokens, ["temperature"] = temperature,
                        ["deadline_ms"] = deadlineMs, ["streaming"] = true, ["idempotency_key"] = _turn
                    }, _turnStop.Token, id =>
                    {
                        lock (_sync)
                        {
                            if (_job != null && _job != id) throw new OrchestratorException("PROTOCOL_ERROR");
                            _job = id;
                        }
                    }, Check).ConfigureAwait(false);
                    // Drain preceding deltas before the caller can start the next turn.
                    await WaitAsync(_end.Task, _turnStop.Token).ConfigureAwait(false);
                    Check();
                    string text = OrchestratorWire.Text(result["text"]);
                    succeeded = true;
                    return text;
                }
                catch (OperationCanceledException)
                {
                    lock (_sync) if (_error != null) throw _error;
                    token.ThrowIfCancellationRequested();
                    _client.Lifetime.ThrowIfCancellationRequested();
                    throw new OrchestratorException("TURN_CANCELLED");
                }
                catch (OrchestratorException) { throw; }
                catch (Exception) { throw new OrchestratorException("DIALOGUE_FAILED"); }
                finally
                {
                    lock (_sync)
                    {
                        _turnStop?.Cancel();
                        _delta = null;
                    }
                    // A cancelled turn leaves uncertain queued events; invalidate this session rather
                    // than accidentally applying them to a subsequent turn. Create a fresh client/session.
                    if (!succeeded) Abort();
                    _turnGate.Release();
                }
            }
        }

        internal void Abort()
        {
            _stop.Cancel();
            _socket.Abort();
        }

        internal async Task CloseAsync(CancellationToken token)
        {
            Abort();
            try { await _client.JsonAsync(HttpMethod.Delete, "/v1/sessions/" + _id, null, token).ConfigureAwait(false); }
            catch (Exception) { }
            // Never await a misbehaving user callback indefinitely.
            foreach (Task task in new[] { _reader, _consumer, _heartbeat })
            {
                if (task == null) continue;
                try { await WaitAsync(task, token).ConfigureAwait(false); }
                catch (Exception) { }
            }
            _socket.Dispose();
        }
    }
}