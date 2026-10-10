using System;
using System.IO;
using System.Text;
using System.Threading;
using System.Threading.Tasks;
using EchoCradle.Orchestration;
using Newtonsoft.Json.Linq;
using UnityEngine;

namespace EchoCradle.Interview
{
    [RequireComponent(typeof(AudioSource))]
    public sealed class InterviewController : MonoBehaviour
    {
        [SerializeField, Tooltip("Data-driven voice interview configuration.")]
        private InterviewSettings settings;
        private AudioSource _audio;
        private JObject _config;
        private MicrophoneCapture _microphone;
        private OwnedOrchestrator _owner;
        private InterviewState _state;
        private CancellationTokenSource _lifetime, _turn;
        private readonly CancellationTokenSource _application = new CancellationTokenSource();
        private Task _preparation;
        private Task _shutdown;
        private bool _companionReady;
        private string _preparationStatus;
        private byte[] _pendingAudio;
        private bool _running, _interrupted, _quitting, _error;
        private bool _destroyed;
        private string _status, _subtitle = "", _player = "", _saveFolder = "";
        private Task _run;
        private readonly StringBuilder _delivered = new StringBuilder();
        private string _playingText;
        private AudioClip _playingClip;
        private GUIStyle _titleStyle, _textStyle, _statusStyle, _buttonStyle;

        private void Awake()
        {
            _audio = GetComponent<AudioSource>();
            _audio.playOnAwake = false;
            _audio.spatialBlend = 0;
            Application.runInBackground = true;
            _config = settings.Read();
            Application.wantsToQuit += WantsToQuit;
            _preparation = PrepareCompanionAsync();
        }

        private async Task PrepareCompanionAsync()
        {
            _companionReady = false;
            _preparationStatus = Ui("loading");
            Debug.Log("Companion preload started.");
            try
            {
                JObject service = (JObject)_config["service"];
                string root = Path.GetFullPath(Path.Combine(Application.dataPath, "..", (string)service["packageRoot"]));
                OwnedOrchestrator owner = await OwnedOrchestrator.StartAsync((string)service["pythonExecutable"], root, _application.Token);
                if (_application.IsCancellationRequested)
                {
                    await owner.CloseAsync();
                    return;
                }
                _owner = owner;
                _companionReady = true;
                _preparationStatus = Ui("ready");
                Debug.Log("Companion preload ready: text, TTS and STT loaded.");
            }
            catch (OperationCanceledException) { }
            catch (Exception error)
            {
                _preparationStatus = Ui("preparationFailed");
                Debug.LogWarning("Companion preload failed: " + (error is OrchestratorException remote ? remote.Code : error.GetType().Name));
            }
        }

        private string Ui(string key) => (string)_config["ui"][key];

        private void OnGUI()
        {
            if (_config == null) return;
            if (_titleStyle == null)
            {
                _titleStyle = new GUIStyle(GUI.skin.label) { fontSize = 42, alignment = TextAnchor.MiddleCenter };
                _textStyle = new GUIStyle(GUI.skin.label) { fontSize = 24, wordWrap = true, alignment = TextAnchor.MiddleCenter };
                _statusStyle = new GUIStyle(_textStyle) { fontSize = 16 };
                _buttonStyle = new GUIStyle(GUI.skin.button) { fontSize = 22 };
            }
            GUI.DrawTexture(new Rect(0, 0, Screen.width, Screen.height), Texture2D.blackTexture);
            float width = Mathf.Min(800, Screen.width - 80);
            float left = (Screen.width - width) / 2;
            if (!_running && !_error)
            {
                GUI.Label(new Rect(left, Screen.height * 0.28f, width, 80), Ui("title"), _titleStyle);
                if (GUI.Button(new Rect(Screen.width / 2f - 120, Screen.height * 0.5f, 240, 52), Ui("newGame"), _buttonStyle))
                {
                    _running = true;
                    _run = RunAsync();
                }
                GUI.enabled = false;
                GUI.Button(new Rect(Screen.width / 2f - 120, Screen.height * 0.5f + 68, 240, 52), Ui("loadGame"), _buttonStyle);
                GUI.enabled = true;
                GUI.Label(new Rect(left, Screen.height * 0.5f + 140, width, 70), _preparationStatus, _statusStyle);
                return;
            }
            GUI.Label(new Rect(left, Screen.height * 0.23f, width, 200), _subtitle, _textStyle);
            GUI.Label(new Rect(left, Screen.height * 0.57f, width, 100), _player, _statusStyle);
            GUI.Label(new Rect(left, Screen.height - 160, width, 60), _status, _statusStyle);
            if (_saveFolder.Length > 0)
                GUI.Label(new Rect(20, Screen.height - 35, Screen.width - 40, 30), Ui("saveLocation") + _saveFolder);
            GUI.enabled = !_error || !_running;
            if (!_quitting && GUI.Button(new Rect(Screen.width / 2f - 120, Screen.height - 90, 240, 40),
                Ui(_error ? "back" : "cancel"), _buttonStyle))
            {
                if (_error) { _error = false; _running = false; }
                else Cancel();
            }
            GUI.enabled = true;
        }

        private void Update()
        {
            if (_microphone == null) return;
            try { _microphone.Poll(); }
            catch (Exception)
            {
                _error = true;
                _status = Ui("noMicrophone");
                Cancel();
            }
        }

        private void SpeechStarted()
        {
            Debug.Log("Microphone speech onset detected.");
            if (_turn == null) return;
            _interrupted = true;
            _state.AcceptTools(false);
            RecordPartialPlayback();
            _audio.Stop();
            _turn.Cancel();
        }

        private void Captured(byte[] pcm)
        {
            // One utterance slot, fail explicitly instead of silently losing subsequent speech.
            if (_pendingAudio != null)
            {
                _error = true;
                _status = Ui("limit");
                Cancel();
                return;
            }
            _pendingAudio = pcm;
            Debug.Log("Microphone utterance captured: " + (pcm.Length / 32000f).ToString("F2") + " seconds.");
        }

        private async Task RunAsync()
        {
            _lifetime = CancellationTokenSource.CreateLinkedTokenSource(_application.Token);
            CancellationToken token = _lifetime.Token;
            bool complete = false;
            _subtitle = _player = _saveFolder = "";
            _pendingAudio = null;
            try
            {
                _status = Ui("loading");
                _state = new InterviewState((JObject)_config["schema"]);
                SaveGame save = await SaveGame.CreateAsync(Application.persistentDataPath, (JObject)_config["save"], token);
                _saveFolder = save.Folder;
                if (_preparation == null || (_preparation.IsCompleted && !_companionReady))
                    _preparation = PrepareCompanionAsync();
                // Cancelling an interview doesn't cancel app-scoped background model loading.
                while (!_preparation.IsCompleted) await Awaitable.NextFrameAsync(token);
                await _preparation;
                token.ThrowIfCancellationRequested();
                if (!_companionReady || _owner == null) throw new InvalidOperationException("COMPANION_NOT_READY");
                await _owner.Client.ResetSessionAsync((JArray)_config["tools"], _state.SessionHandler(), token);
                SileroDetector vad = await Task.Run(() => new SileroDetector((JObject)_config["vad"]), token);
                if (token.IsCancellationRequested) { vad.Dispose(); token.ThrowIfCancellationRequested(); }
                try { _microphone = new MicrophoneCapture((JObject)_config["microphone"], vad); }
                catch { vad.Dispose(); throw; }
                _microphone.SpeechStarted += SpeechStarted;
                _microphone.Utterance += Captured;
                var history = new JArray();
                bool interruptedContext = false;
                string previousMissing = null;
                int stalled = 0;
                for (int index = 0; index < (int)_config["generation"]["maxTurns"]; index++)
                {
                    token.ThrowIfCancellationRequested();
                    // Onset during STT/reset has no active turn to cancel. Consume that speech first.
                    if (_pendingAudio != null || _microphone.IsSpeaking)
                    {
                        string extra = await ListenAsync(token);
                        history.Add(InterviewState.Message("user", extra));
                        _player = extra;
                    }
                    _interrupted = false;
                    _delivered.Clear();
                    _subtitle = "";
                    _turn = CancellationTokenSource.CreateLinkedTokenSource(token);
                    _state.AcceptTools(true);
                    string missing = _state.Missing().ToString();
                    stalled = missing == previousMissing ? stalled + 1 : 0;
                    previousMissing = missing;
                    JArray messages = _state.Messages(history, (JObject)_config["prompts"], interruptedContext, stalled >= 2);
                    try { await SpeakTurnAsync(messages, _turn.Token); }
                    catch (OperationCanceledException) when (_interrupted && !token.IsCancellationRequested) { }
                    finally
                    {
                        _state.AcceptTools(false);
                        _turn.Dispose();
                        _turn = null;
                        ReleaseClip();
                    }
                    token.ThrowIfCancellationRequested();
                    if (_delivered.Length > 0) history.Add(InterviewState.Message("assistant", _delivered.ToString().Trim()));
                    interruptedContext = _interrupted;
                    if (!_interrupted && _state.Missing().Count == 0)
                    {
                        // Give onset detection and the worker hand-off a final quiet boundary.
                        float quietUntil = Time.realtimeSinceStartup + (float)_config["completion"]["quietSeconds"];
                        while (Time.realtimeSinceStartup < quietUntil && _pendingAudio == null && !_microphone.IsSpeaking)
                            await Awaitable.NextFrameAsync(token);
                        if (_pendingAudio == null && !_microphone.HasPendingAudio)
                        {
                            _status = Ui("saving");
                            StopMicrophone();
                            await save.CompleteAsync(_state.Snapshot(), _state, token);
                            complete = true;
                            break;
                        }
                    }
                    if (_interrupted)
                        await _owner.Client.ResetSessionAsync((JArray)_config["tools"], _state.SessionHandler(), token);
                    string transcript = await ListenAsync(token);
                    _player = transcript;
                    history.Add(InterviewState.Message("user", transcript));
                    int limit = (int)_config["generation"]["historyPairs"] * 2;
                    while (history.Count > limit) history.RemoveAt(0);
                }
                if (!complete) throw new InvalidOperationException("INTERVIEW_LIMIT");
                _status = Ui("finished");
            }
            catch (OperationCanceledException) { }
            catch (Exception error)
            {
                _error = true;
                _status = error.Message == "MICROPHONE_UNAVAILABLE" ? Ui("noMicrophone") : Ui("error");
                Debug.LogWarning("Interview stopped: " + (error is OrchestratorException remote ? remote.Code : error.GetType().Name));
            }
            finally
            {
                StopMicrophone();
                if (!_destroyed) _audio.Stop();
                ReleaseClip();
                if (!_quitting && !_destroyed && _owner != null)
                {
                    // Keep models resident in the menu, but revoke all interview tool/session state.
                    try { await _owner.Client.ResetSessionAsync(new JArray(), null, _application.Token); }
                    catch (Exception)
                    {
                        await _owner.CloseAsync();
                        _owner = null;
                        _companionReady = false;
                    }
                }
                _lifetime.Dispose();
                _lifetime = null;
                _running = false;
            }
            if (!_destroyed && (complete || _quitting))
            {
                _quitting = true;
                await ShutdownCompanionAsync();
                Application.wantsToQuit -= WantsToQuit;
                QuitNow();
            }
        }

        private async Task<string> ListenAsync(CancellationToken token)
        {
            while (true)
            {
                _status = Ui("listening");
                float started = Time.realtimeSinceStartup;
                while (_pendingAudio == null)
                {
                    token.ThrowIfCancellationRequested();
                    if (Time.realtimeSinceStartup - started > (int)_config["microphone"]["idleTimeoutSeconds"])
                        throw new TimeoutException();
                    await Awaitable.NextFrameAsync(token);
                }
                byte[] pcm = _pendingAudio;
                _pendingAudio = null;
                _status = Ui("thinking");
                string transcript = await _owner.Client.TranscribeAsync(pcm, token);
                token.ThrowIfCancellationRequested();
                Debug.Log("Microphone STT completed: " + transcript.Length + " characters (content not logged).");
                if (!string.IsNullOrWhiteSpace(transcript)) return transcript;
                _subtitle = Ui("emptyTranscript");
            }
        }

        private async Task SpeakTurnAsync(JArray messages, CancellationToken token)
        {
            JObject generation = (JObject)_config["generation"];
            var chunks = new SpeechChunks((int)_config["speech"]["chunkCharacters"], (int)_config["speech"]["maxCharacters"]);
            _status = Ui("thinking");
            Task<string> dialogue = _owner.Client.DialogueAsync(messages, (int)generation["maxTokens"],
                (float)generation["temperature"], (int)generation["deadlineMs"], chunks.Add, token);
            bool flushed = false;
            try
            {
                while (true)
                {
                    token.ThrowIfCancellationRequested();
                    if (dialogue.IsCompleted && !flushed)
                    {
                        await dialogue; // Observe failures before using further generated speech.
                        chunks.Flush();
                        flushed = true;
                    }
                    if (!chunks.TryTake(out string text))
                    {
                        if (flushed) break;
                        await Awaitable.NextFrameAsync(token);
                        continue;
                    }
                    PcmAudio pcm = await _owner.Client.SynthesizeAsync(text, token);
                    token.ThrowIfCancellationRequested();
                    float[] samples = await Task.Run(() =>
                    {
                        var values = new float[pcm.Samples.Length / 2];
                        for (int i = 0; i < values.Length; i++)
                            values[i] = (short)(pcm.Samples[i * 2] | pcm.Samples[i * 2 + 1] << 8) / 32768f;
                        return values;
                    }, token);
                    token.ThrowIfCancellationRequested();
                    if (samples.Length == 0) continue;
                    _playingClip = AudioClip.Create("Companion speech", samples.Length, 1, pcm.SampleRate, false);
                    _playingClip.SetData(samples, 0);
                    _playingText = text;
                    _subtitle = text;
                    _status = Ui("speaking");
                    _audio.clip = _playingClip;
                    _audio.Play();
                    while (_audio.isPlaying) await Awaitable.NextFrameAsync(token);
                    token.ThrowIfCancellationRequested();
                    _delivered.Append(text).Append(' ');
                    ReleaseClip();
                }
                if (_delivered.Length == 0) throw new InvalidOperationException("EMPTY_ASSISTANT_SPEECH");
            }
            finally
            {
                // Cancellation invalidates the socket session; observe the cancelled generator before reset.
                _turn.Cancel();
                try { await dialogue; } catch (Exception) { }
            }
        }

        private void RecordPartialPlayback()
        {
            if (_playingClip == null || _playingText == null || !_audio.isPlaying) return;
            string prefix = SpeechChunks.EstimatedPrefix(_playingText, _audio.timeSamples, _playingClip.samples);
            if (prefix.Length > 0) _delivered.Append(prefix).Append(' ');
        }

        private void ReleaseClip()
        {
            if (!_destroyed) _audio.clip = null;
            if (_playingClip != null) Destroy(_playingClip);
            _playingClip = null;
            _playingText = null;
        }

        private void Cancel()
        {
            _state?.AcceptTools(false);
            _lifetime?.Cancel();
            if (!_destroyed) _audio.Stop();
            StopMicrophone();
        }

        private void StopMicrophone()
        {
            if (_microphone == null) return;
            _microphone.SpeechStarted -= SpeechStarted;
            _microphone.Utterance -= Captured;
            _microphone.Dispose();
            _microphone = null;
        }

        private bool WantsToQuit()
        {
            if (_quitting && _shutdown != null && _shutdown.IsCompleted) return true;
            _quitting = true;
            Cancel();
            if (!_running) _shutdown = ShutdownAndQuitAsync();
            return false;
        }

        private async Task ShutdownCompanionAsync()
        {
            _application.Cancel();
            if (_preparation != null) await _preparation;
            if (_owner != null) { await _owner.CloseAsync(); _owner = null; }
            _companionReady = false;
        }

        private async Task ShutdownAndQuitAsync()
        {
            await ShutdownCompanionAsync();
            // wantsToQuit must accept this second request, rather than re-enter shutdown.
            Application.wantsToQuit -= WantsToQuit;
            if (!_destroyed) QuitNow();
        }

        private static void QuitNow()
        {
#if UNITY_EDITOR
            UnityEditor.EditorApplication.isPlaying = false;
#else
            Application.Quit();
#endif
        }

        private void OnDestroy()
        {
            _destroyed = true;
            _application.Cancel();
            Application.wantsToQuit -= WantsToQuit;
            Cancel();
            _owner?.Dispose(); // Kill-on-close fallback when Play Mode ends without awaiting cleanup.
        }

        private void OnDisable()
        {
            if (_running) Cancel();
            if (_application != null) _application.Cancel();
            _owner?.Dispose();
        }
    }
}