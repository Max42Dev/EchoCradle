using System;
using System.Threading;
using System.Threading.Tasks;
using Newtonsoft.Json.Linq;

namespace EchoCradle.Orchestration
{
    /// <summary>Owns a contained Windows service or attaches without shutdown/kill authority.</summary>
    public sealed class OwnedOrchestrator : IDisposable
    {
        private readonly object _sync = new object();
        private WindowsOrchestratorProcess _process;
        private Task _close;
        public OrchestratorClient Client { get; private set; }
        public bool IsOwned => _process != null;

        private OwnedOrchestrator() { }

        /// <summary>
        /// Attach when ECHOCRADLE_SERVICE_URL and ECHOCRADLE_SERVICE_TOKEN are set;
        /// otherwise launch the configured absolute Python executable from the package root
        /// (the directory containing orchestrator/service.py). Never downloads or installs models.
        /// </summary>
        public static Task<OwnedOrchestrator> StartAsync(string pythonExecutable, string packageRoot,
            CancellationToken cancellationToken = default)
        {
            return Task.Run(async () =>
            {
                var owner = new OwnedOrchestrator();
                try
                {
                    cancellationToken.ThrowIfCancellationRequested();
                    string attachedUrl = Environment.GetEnvironmentVariable("ECHOCRADLE_SERVICE_URL");
                    string token = Environment.GetEnvironmentVariable("ECHOCRADLE_SERVICE_TOKEN");
                    if (!string.IsNullOrEmpty(attachedUrl))
                    {
                        owner.Client = await OrchestratorClient.ConnectAsync(attachedUrl, token,
                            cancellationToken).ConfigureAwait(false);
                    }
                    else
                    {
                        byte[] random = new byte[32];
                        using (var rng = System.Security.Cryptography.RandomNumberGenerator.Create())
                            rng.GetBytes(random);
                        token = Convert.ToBase64String(random);
                        Array.Clear(random, 0, random.Length);
                        owner._process = WindowsOrchestratorProcess.Launch(pythonExecutable, packageRoot, token);
                        JObject bootstrap = await owner._process.BootstrapAsync(cancellationToken).ConfigureAwait(false);
                        OrchestratorWire.Integer(bootstrap["protocol_version"], 1, 1);
                        int port = OrchestratorWire.Integer(bootstrap["port"], 1, 65535);
                        string instance = OrchestratorWire.Id(bootstrap["service_instance_id"]);
                        using (var startup = CancellationTokenSource.CreateLinkedTokenSource(cancellationToken))
                        {
                            Task<OrchestratorClient> connect = OrchestratorClient.ConnectAsync(
                                "http://127.0.0.1:" + port, token, startup.Token);
                            // Fail early if the contained process exits during profile loading.
                            while (!connect.IsCompleted)
                            {
                                if (owner._process.Exited) startup.Cancel();
                                await Task.WhenAny(connect, Task.Delay(100)).ConfigureAwait(false);
                            }
                            owner.Client = await connect.ConfigureAwait(false);
                        }
                        if (owner.Client.Instance != instance) throw new OrchestratorException("INSTANCE_CHANGED");
                    }
                    cancellationToken.ThrowIfCancellationRequested();
                    return owner;
                }
                catch (OperationCanceledException)
                {
                    owner._process?.Dispose();
                    await owner.CloseAsync().ConfigureAwait(false);
                    if (cancellationToken.IsCancellationRequested) throw;
                    throw new OrchestratorException("STARTUP_FAILED");
                }
                catch (OrchestratorException)
                {
                    owner._process?.Dispose();
                    await owner.CloseAsync().ConfigureAwait(false);
                    throw;
                }
                catch (Exception)
                {
                    owner._process?.Dispose();
                    await owner.CloseAsync().ConfigureAwait(false);
                    throw new OrchestratorException("STARTUP_FAILED");
                }
            });
        }

        public Task CloseAsync()
        {
            lock (_sync)
            {
                if (_close == null)
                {
                    _close = Task.Run(async () =>
                    {
                        try
                        {
                            using (var cleanup = new CancellationTokenSource(TimeSpan.FromSeconds(5)))
                            {
                                if (_process != null && Client != null && !_process.Exited)
                                {
                                    try { await Client.ShutdownAsync(cleanup.Token).ConfigureAwait(false); }
                                    catch (Exception) { }
                                }
                                if (Client != null) await Client.CloseAsync().ConfigureAwait(false);
                                if (_process != null)
                                {
                                    while (!_process.Exited && !cleanup.IsCancellationRequested)
                                        await Task.Delay(50).ConfigureAwait(false);
                                }
                            }
                        }
                        finally
                        {
                            _process?.Dispose();
                            _process?.CloseOutput();
                        }
                    });
                }
                return _close;
            }
        }

        /// <summary>Immediate kill-on-close containment; use CloseAsync for graceful drain.</summary>
        public void Dispose()
        {
            _process?.Dispose();
            Client?.Dispose();
            _ = CloseAsync();
        }
    }
}