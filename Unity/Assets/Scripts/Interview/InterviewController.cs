using System;
using System.Collections.Generic;
using System.IO;
using System.Text;
using System.Threading;
using System.Threading.Tasks;
using EchoCradle.Orchestration;
using EchoCradle.CharacterPreview;
using Newtonsoft.Json.Linq;
using UnityEngine;

namespace EchoCradle.Interview
{
    public sealed class InterviewController : MonoBehaviour
    {
        [SerializeField, Tooltip("Data-driven voice interview configuration.")]
        private InterviewSettings settings;
        private AudioSource _audio;
        private JObject _config;
        private MicrophoneCapture _microphone;
        private OwnedOrchestrator _owner;
        private InterviewState _state;
        private CancellationTokenSource _lifetime, _turn, _speech;
        private readonly CancellationTokenSource _application = new CancellationTokenSource();
        private Task _preparation;
        private Task _shutdown;
        private bool _companionReady;
        private string _preparationStatus;
        private readonly Queue<byte[]> _pendingAudio = new Queue<byte[]>(2);
        private bool _running, _interrupted, _quitting, _error;
        private bool _destroyed;
        private string _status, _subtitle = "", _player = "", _saveFolder = "";
        private Task _run;
        private readonly StringBuilder _delivered = new StringBuilder();
        private string _playingText;
        private AudioClip _playingClip;
        private string _audioDiagnosticsDirectory;
        private int _microphoneDiagnosticIndex, _playbackDiagnosticIndex;
        private GUIStyle _titleStyle, _textStyle, _statusStyle, _buttonStyle;
        private InterviewLaunchOptions _options;
        private PlayerCharacterPreview _preview;
        private CharacterCreationState _creation;
        private TaskCompletionSource<string> _textInput;
        private string _typedText = "";
        private bool _approve, _reject, _continue, _next, _waitingForText, _finished;
        private int _reviewFrame;
        private bool _reviewOpen;
        public CharacterCreationState Creation => _creation;
        public bool WaitingForText => _waitingForText;
        public bool HasError => _error;
        public bool IsFinished => _finished;
        public string SaveFolder => _saveFolder;
        public string Subtitle => _subtitle;
        internal InterviewLaunchOptions LaunchOptions => _options;
        public bool CanReview => _reviewOpen && Time.frameCount > _reviewFrame;

        private void Awake()
        {
            Application.runInBackground = true;
            _config = settings.Read();
            string projectRoot = Path.GetFullPath(Path.Combine(Application.dataPath, ".."));
            _options = new InterviewLaunchOptions(_config, projectRoot, Environment.GetCommandLineArgs());
            if (!_options.TextOnly)
            {
                _audio = GetComponent<AudioSource>();
                if (_audio == null) _audio = gameObject.AddComponent<AudioSource>();
                _audio.playOnAwake = false;
                _audio.spatialBlend = 0;
            }
            _config["vad"]["nativeDirectory"] = InterviewLaunchOptions.Resolve(projectRoot, (string)_config["vad"]["nativeDirectory"]);
            _preview = GetComponent<PlayerCharacterPreview>();
            if (_preview == null) _preview = gameObject.AddComponent<PlayerCharacterPreview>();
            _audioDiagnosticsDirectory = Environment.GetEnvironmentVariable("ECHOCRADLE_AUDIO_DIAGNOSTICS");
            if (!string.IsNullOrEmpty(_audioDiagnosticsDirectory))
            {
                _audioDiagnosticsDirectory = Path.GetFullPath(Path.Combine(_audioDiagnosticsDirectory,
                    DateTime.UtcNow.ToString("yyyyMMdd-HHmmss") + "-" + Guid.NewGuid().ToString("N")));
                Debug.Log("Opt-in speech diagnostics: " + _audioDiagnosticsDirectory);
            }
            Application.wantsToQuit += WantsToQuit;
            _preparation = PrepareCompanionAsync();
            if (_options.Smoke) gameObject.AddComponent<CharacterSmokeDriver>();
        }

        private async Task PrepareCompanionAsync()
        {
            _companionReady = false;
            _preparationStatus = Ui("loading");
            Debug.Log("Companion preload started.");
            try
            {
                OwnedOrchestrator owner = await OwnedOrchestrator.StartAsync(_options.Python, _options.PackageRoot,
                    _application.Token, _options.TextOnly ? "text" : "voice", _options.VisualConfig);
                if (_application.IsCancellationRequested)
                {
                    await owner.CloseAsync();
                    return;
                }
                _owner = owner;
                _companionReady = true;
                _preparationStatus = Ui("ready");
                Debug.Log("Companion preload ready: " + (_options.TextOnly ? "text only (no audio devices or speech jobs)." : "voice."));
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
                _textStyle = new GUIStyle(GUI.skin.label) { fontSize = 24, wordWrap = true, alignment = TextAnchor.MiddleCenter, clipping = TextClipping.Clip };
                _statusStyle = new GUIStyle(_textStyle) { fontSize = 16 };
                _buttonStyle = new GUIStyle(GUI.skin.button) { fontSize = 22 };
            }
            if (_preview.PreviewTexture != null)
                GUI.DrawTexture(new Rect(20, 20, Screen.width * 0.52f, Screen.height - 180),
                    _preview.PreviewTexture, ScaleMode.ScaleToFit);
            float width = Mathf.Min(800, Screen.width - 80);
            float left = (Screen.width - width) / 2;
            if (_preview.PreviewTexture != null) { left = Screen.width * 0.56f; width = Screen.width * 0.41f; }
            if (!_running && !_error && !_finished)
            {
                GUI.Label(new Rect(left, Screen.height * 0.28f, width, 80), Ui("title"), _titleStyle);
                if (GUI.Button(new Rect(Screen.width / 2f - 120, Screen.height * 0.5f, 240, 52), Ui("newGame"), _buttonStyle))
                {
                    BeginNewGame();
                }
                GUI.enabled = false;
                GUI.Button(new Rect(Screen.width / 2f - 120, Screen.height * 0.5f + 68, 240, 52), Ui("loadGame"), _buttonStyle);
                GUI.enabled = true;
                GUI.Label(new Rect(left, Screen.height * 0.5f + 140, width, 70), _preparationStatus, _statusStyle);
                return;
            }
            float controlsY = Screen.height - 240;
            float textTop = Mathf.Min(Screen.height * 0.23f, 100);
            float playerTop = Mathf.Max(textTop, controlsY - 90);
            GUI.Label(new Rect(left, textTop, width, Mathf.Max(0, playerTop - textTop - 12)), _subtitle, _textStyle);
            GUI.Label(new Rect(left, playerTop, width, Mathf.Max(0, controlsY - playerTop - 12)), _player, _statusStyle);
            if (_waitingForText)
            {
                _typedText = GUI.TextField(new Rect(left, Screen.height - 240, width, 44), _typedText, 2000);
                if (GUI.Button(new Rect(left, Screen.height - 190, width, 40), "Send text", _buttonStyle))
                    if (SubmitText(_typedText)) _typedText = "";
            }
            if (_creation != null && !_error && _running)
            {
                float y = controlsY;
                if (_creation.Stage == CharacterCreationState.Phase.ImageReview)
                {
                    if (GUI.Button(new Rect(left, y, width / 2 - 4, 44), "Approve image")) ApproveImage();
                    if (GUI.Button(new Rect(left + width / 2, y, width / 2, 44), "Reject / describe again")) RejectImage();
                }
                if (_creation.Stage == CharacterCreationState.Phase.StaticReview &&
                    GUI.Button(new Rect(left, y, width, 44), "Continue: generate animations")) ContinueToAnimation();
                if (_creation.Stage == CharacterCreationState.Phase.AnimationReview &&
                    GUI.Button(new Rect(left, y, width, 44), "Reviewed: Next / Finish")) NextAnimation();
            }
            string displayStatus = _status;
            if (_creation != null && !_error && _running &&
                _creation.Stage != CharacterCreationState.Phase.Configuration &&
                _creation.Stage != CharacterCreationState.Phase.Description)
                displayStatus = CanReview ? "Awaiting review" : _creation.Stage.ToString();
            GUI.Label(new Rect(left, Screen.height - 160, width, 60), displayStatus, _statusStyle);
            if (_saveFolder.Length > 0)
                GUI.Label(new Rect(20, Screen.height - 35, Screen.width - 40, 30), Ui("saveLocation") + _saveFolder);
            GUI.enabled = !_error || !_running;
            if (!_quitting && !_finished && GUI.Button(new Rect(Screen.width / 2f - 120, Screen.height - 90, 240, 40),
                Ui(_error ? "back" : "cancel"), _buttonStyle))
            {
                if (_error) { _error = false; _running = false; }
                else Cancel();
            }
            GUI.enabled = true;
        }

        public bool BeginNewGame()
        {
            if (!isActiveAndEnabled || _destroyed || _running || _quitting || _finished) return false;
            _running = true;
            _error = false;
            _run = RunAsync();
            return true;
        }

        public bool SubmitText(string text)
        {
            if (!_waitingForText || string.IsNullOrWhiteSpace(text) || text.Length > 2000) return false;
            return _textInput.TrySetResult(text.Trim());
        }

        public void ApproveImage()
        {
            if (_creation?.Stage == CharacterCreationState.Phase.ImageReview && CanReview) _approve = true;
        }
        public void RejectImage()
        {
            if (_creation?.Stage == CharacterCreationState.Phase.ImageReview && CanReview) _reject = true;
        }
        public void ContinueToAnimation()
        {
            if (_creation?.Stage == CharacterCreationState.Phase.StaticReview && CanReview) _continue = true;
        }
        public void NextAnimation()
        {
            if (_creation?.Stage == CharacterCreationState.Phase.AnimationReview && CanReview) _next = true;
        }

        private void Update()
        {
            if (!_options.TextOnly && _running && _turn != null && _state.IsComplete)
            {
                // Field completion wins over playback, pending microphone work and further turns.
                _state.AcceptTools(false);
                StopMicrophone();
                _audio.Stop();
                // Recording is a local side effect, not proof that tool.result was delivered.
                // Stop speech only; let the text/tool exchange report its actual outcome.
                _speech?.Cancel();
                return;
            }
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
            if (_turn == null || _state.IsComplete) return;
            _interrupted = true;
            _state.AcceptTools(false);
            RecordPartialPlayback();
            _audio.Stop();
            _turn.Cancel();
        }

        private void Captured(byte[] pcm)
        {
            // Preserve consecutive utterances while STT/reset is awaiting the service.
            if (_pendingAudio.Count >= 2)
            {
                _error = true;
                _status = Ui("limit");
                Cancel();
                return;
            }
            _pendingAudio.Enqueue(pcm);
            Debug.Log("Microphone utterance captured: " + (pcm.Length / 32000f).ToString("F2") + " seconds.");
        }

        private async Task RunAsync()
        {
            _lifetime = CancellationTokenSource.CreateLinkedTokenSource(_application.Token);
            CancellationToken token = _lifetime.Token;
            bool complete = false;
            _subtitle = _player = _saveFolder = "";
            _pendingAudio.Clear();
            _reviewOpen = false;
            _preview.Clear();
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
                await _owner.Client.ResetSessionAsync(_state.ToolDeclarations(), _state.SessionHandler(), token);
                if (!_options.TextOnly) await StartMicrophoneAsync(token);
                _creation = new CharacterCreationState();
                await ConverseAsync((JObject)_config["prompts"], token);
                await save.SaveDraftAsync(_state.Snapshot(), _state, token);
                _creation.ConfigurationRecorded();
                StopMicrophone();
                await CreateCharacterAsync(save, token);
                await save.CompleteCharacterAsync(_creation, token);
                complete = true;
                _finished = true;
                _status = Ui("finished");
                Debug.Log("CHARACTER_READY " + _saveFolder);
            }
            catch (OperationCanceledException) when (token.IsCancellationRequested) { }
            catch (Exception error)
            {
                _error = true;
                _status = error.Message == "MICROPHONE_UNAVAILABLE" ? Ui("noMicrophone") : Ui("error");
                Debug.LogWarning("Interview stopped: " + (error is OrchestratorException remote ? remote.Code : error.GetType().Name));
            }
            finally
            {
                _reviewOpen = false;
                StopMicrophone();
                if (!_destroyed) _audio?.Stop();
                ReleaseClip();
                if (!_quitting && !_destroyed && _owner != null)
                {
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
            if (!_destroyed && _quitting)
            {
                await ShutdownCompanionAsync();
                Application.wantsToQuit -= WantsToQuit;
                QuitNow();
            }
            else if (complete) Debug.Log("Character creation complete; application remains open.");
        }

        private async Task StartMicrophoneAsync(CancellationToken token)
        {
                SileroDetector vad = await Task.Run(() => new SileroDetector((JObject)_config["vad"]), token);
                if (token.IsCancellationRequested) { vad.Dispose(); token.ThrowIfCancellationRequested(); }
                try { _microphone = new MicrophoneCapture((JObject)_config["microphone"], vad); }
                catch { vad.Dispose(); throw; }
                _microphone.SpeechStarted += SpeechStarted;
                _microphone.Utterance += Captured;
                _status = Ui("calibrating");
                float calibrationDeadline = Time.realtimeSinceStartup + 10;
                while (!_microphone.IsCalibrated)
                {
                    if (Time.realtimeSinceStartup >= calibrationDeadline)
                        throw new InvalidOperationException("MICROPHONE_UNAVAILABLE");
                    await Awaitable.NextFrameAsync(token);
                }
                Debug.Log("Microphone noise-floor calibration completed.");
        }

        private async Task ConverseAsync(JObject prompts, CancellationToken token)
        {
                if (!_options.TextOnly && _microphone == null) await StartMicrophoneAsync(token);
                var history = new JArray();
                bool interruptedContext = false;
                string previousMissing = null;
                int stalled = 0;
                for (int index = 0; index < (int)_config["generation"]["maxTurns"]; index++)
                {
                    token.ThrowIfCancellationRequested();
                    // Onset during STT/reset has no active turn to cancel. Consume that speech first.
                    while (_pendingAudio.Count > 0 || (_microphone != null && _microphone.HasPendingAudio))
                    {
                        if (_pendingAudio.Count == 0 && !_microphone.IsSpeaking)
                        {
                            // Wait for queued/in-flight VAD before admitting a new assistant turn.
                            await Awaitable.NextFrameAsync(token);
                            continue;
                        }
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
                    JArray messages = _state.Messages(history, prompts, interruptedContext, stalled >= 2);
                    try { await SpeakTurnAsync(messages, _turn.Token); }
                    catch (OperationCanceledException) when (_interrupted && _turn.IsCancellationRequested && !token.IsCancellationRequested) { }
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
                    if (_state.IsComplete)
                    {
                        StopMicrophone();
                        _audio?.Stop();
                        _pendingAudio.Clear();
                        return;
                    }
                    if (_interrupted)
                        await _owner.Client.ResetSessionAsync(_state.ToolDeclarations(), _state.SessionHandler(), token);
                    string transcript = await ListenAsync(token);
                    _player = transcript;
                    history.Add(InterviewState.Message("user", transcript));
                    int limit = (int)_config["generation"]["historyPairs"] * 2;
                    while (history.Count > limit) history.RemoveAt(0);
                }
                throw new InvalidOperationException("INTERVIEW_LIMIT");
        }

        private async Task CreateCharacterAsync(SaveGame save, CancellationToken token)
        {
            JObject descriptionConfig = (JObject)_config["playerDescription"];
            JObject visual = (JObject)_config["visual"];
            int deadline = (int?)visual?["deadlineMs"] ?? 1800000;
            byte[] image = null;
            while (_creation.Stage == CharacterCreationState.Phase.Description)
            {
                _preview.Clear();
                _subtitle = _player = "";
                _pendingAudio.Clear();
                _state.AcceptTools(false);
                _state = new InterviewState((JObject)descriptionConfig["schema"]);
                // New state, new handler, empty history. Rejection never silently retries an image.
                await _owner.Client.ResetSessionAsync(_state.ToolDeclarations(), _state.SessionHandler(), token);
                await ConverseAsync((JObject)descriptionConfig["prompts"], token);
                string description = (string)_state.Snapshot()["description"];
                string prompt = ((string)visual?["promptPrefix"] ?? "") + description;
                string negativePrompt = (string)visual?["negativePrompt"] ?? "";
                uint seed = _creation.RequestImage(description, prompt, negativePrompt);
                await save.SaveAppearanceAsync(_creation, null, ".png", token);
                _subtitle = "Generating your character image…";
                OrchestratorVisualResult result = await _owner.Client.GenerateImageAsync(
                    prompt, seed, negativePrompt, deadline, token);
                image = result.Payload;
                _creation.ImageReceived(image, result.ArtifactId);
                await save.SaveAppearanceAsync(_creation, image, ".png", token);
                await ReleaseVisualAsync(result.ArtifactId);
                _preview.ShowImage(image);
                _subtitle = "Approve this image, or reject it to describe your character in a new conversation.";
                _approve = _reject = false;
                _reviewFrame = Time.frameCount;
                _reviewOpen = true;
                while (!_approve && !_reject) await Awaitable.NextFrameAsync(token);
                _reviewOpen = false;
                if (_reject)
                {
                    _creation.RejectImage();
                    await save.SaveAppearanceAsync(_creation, null, ".png", token);
                }
            }
            uint modelSeed = _creation.ApproveImage();
            await save.SaveAppearanceAsync(_creation, null, ".glb", token);
            _subtitle = "Generating the static 3D character…";
            OrchestratorVisualResult model = await _owner.Client.Generate3DAsync(image, modelSeed, deadline, token);
            byte[] modelBytes = model.Payload;
            _creation.ModelReceived(modelBytes, model.ArtifactId);
            await save.SaveAppearanceAsync(_creation, modelBytes, ".glb", token);
            await ReleaseVisualAsync(model.ArtifactId);
            await _preview.ShowModelAsync(modelBytes, token, sourceZUp: true);
            _subtitle = "Static 3D preview. Continue when ready to generate animations.";
            _continue = false;
            _reviewFrame = Time.frameCount;
            // A rendered frame must separate import from the animation request.
            await Awaitable.EndOfFrameAsync(token);
            await Awaitable.NextFrameAsync(token);
            _creation.StaticPresented();
            _reviewOpen = true;
            while (!_continue) await Awaitable.NextFrameAsync(token);
            _reviewOpen = false;
            uint animationSeed = _creation.StartAnimation();
            await save.SaveAppearanceAsync(_creation, null, ".glb", token);
            _subtitle = "Generating animations…";
            OrchestratorVisualResult animation = await _owner.Client.Animate3DAsync(modelBytes, animationSeed, deadline, token);
            byte[] animationBytes = animation.Payload;
            await _preview.ShowModelAsync(animationBytes, token);
            _creation.AnimationReceived(animationBytes, animation.ArtifactId, _preview.ClipNames);
            await save.SaveAppearanceAsync(_creation, animationBytes, ".glb", token);
            await ReleaseVisualAsync(animation.ArtifactId);
            while (!_creation.IsReady)
            {
                _preview.PlayClip(_creation.CurrentClip);
                _subtitle = "Animation " + (_creation.ClipIndex + 1) + "/" + _creation.ClipCount + ": " + _creation.CurrentClip;
                _next = false;
                _reviewFrame = Time.frameCount;
                _reviewOpen = true;
                while (!_next) await Awaitable.NextFrameAsync(token);
                _reviewOpen = false;
                _creation.NextClip();
                await save.SaveAppearanceAsync(_creation, null, ".glb", token);
            }
        }

        private async Task ReleaseVisualAsync(string artifactId)
        {
            try { await _owner.Client.ReleaseArtifactAsync(artifactId, _application.Token); }
            catch (Exception error) { Debug.LogWarning("Visual artifact release deferred to TTL: " + error.GetType().Name); }
        }

        private async Task<string> ListenAsync(CancellationToken token)
        {
            _textInput = new TaskCompletionSource<string>();
            _waitingForText = true;
            try
            {
            while (true)
            {
                _status = Ui("listening");
                float started = Time.realtimeSinceStartup;
                while (_pendingAudio.Count == 0)
                {
                    token.ThrowIfCancellationRequested();
                    if (_textInput.Task.IsCompleted) return await _textInput.Task;
                    if (Time.realtimeSinceStartup - started > (int)_config["microphone"]["idleTimeoutSeconds"])
                        throw new TimeoutException();
                    await Awaitable.NextFrameAsync(token);
                }
                byte[] pcm = _pendingAudio.Dequeue();
                _status = Ui("thinking");
                string diagnosticName = "microphone-" + (++_microphoneDiagnosticIndex).ToString("D3");
                if (!string.IsNullOrEmpty(_audioDiagnosticsDirectory))
                    await SpeechDiagnostics.WriteAsync(_audioDiagnosticsDirectory, diagnosticName, pcm, 16000);
                token.ThrowIfCancellationRequested();
                string transcript = await _owner.Client.TranscribeAsync(pcm, token);
                token.ThrowIfCancellationRequested();
                if (!string.IsNullOrEmpty(_audioDiagnosticsDirectory))
                    await SpeechDiagnostics.WriteAsync(_audioDiagnosticsDirectory, diagnosticName, pcm, 16000, transcript);
                token.ThrowIfCancellationRequested();
                Debug.Log("Microphone STT completed: " + transcript.Length + " characters (content not logged).");
                if (!string.IsNullOrWhiteSpace(transcript)) return transcript;
                _subtitle = Ui("emptyTranscript");
            }
            }
            finally { _waitingForText = false; _textInput = null; }
        }

        private async Task SpeakTurnAsync(JArray messages, CancellationToken token)
        {
            JObject generation = (JObject)_config["generation"];
            if (_options.TextOnly)
            {
                _status = Ui("thinking");
                var textChunks = new SpeechChunks(80, (int)_config["speech"]["maxCharacters"]);
                Task<OrchestratorJob> job = _owner.Client.TextAsync(messages, (int)generation["maxTokens"],
                    (float)generation["temperature"], (int)generation["deadlineMs"],
                    textChunks.Add, token);
                while (!job.IsCompleted)
                {
                    while (textChunks.TryTake(out string part))
                    { _delivered.Append(part).Append(' '); _subtitle = _delivered.ToString(); }
                    await Awaitable.NextFrameAsync();
                }
                await AwaitSuccessfulTurnAsync(job);
                textChunks.Flush();
                while (textChunks.TryTake(out string part))
                { _delivered.Append(part).Append(' '); _subtitle = _delivered.ToString(); }
                return;
            }
            var chunks = new SpeechChunks((int)_config["speech"]["chunkCharacters"], (int)_config["speech"]["maxCharacters"]);
            _status = Ui("thinking");
            Task<OrchestratorJob> dialogue = _owner.Client.TextAsync(messages, (int)generation["maxTokens"],
                (float)generation["temperature"], (int)generation["deadlineMs"], chunks.Add, token);
            _speech = CancellationTokenSource.CreateLinkedTokenSource(token);
            CancellationToken speechToken = _speech.Token;
            bool flushed = false;
            try
            {
                try
                {
                while (true)
                {
                    if (_state.IsComplete)
                    {
                        _state.AcceptTools(false);
                        _audio.Stop();
                        break;
                    }
                    speechToken.ThrowIfCancellationRequested();
                    if (dialogue.IsCompleted && !flushed)
                    {
                        OrchestratorJob final = await AwaitSuccessfulTurnAsync(dialogue);
                        foreach (OrchestratorToolCall call in final.Calls)
                            Debug.Log("Interview tool: " + call.Name + "; outcome=" + call.Outcome +
                                "; executed=" + call.LocallyExecuted + "; delivered=" + call.ResultDelivered);
                        Debug.Log("Interview turn: " + final.State + "; tool calls=" + final.Calls.Count +
                            "; missing fields=" + _state.Missing().Count);
                        chunks.Flush();
                        flushed = true;
                    }
                    if (!chunks.TryTake(out string text))
                    {
                        if (flushed) break;
                        await Awaitable.NextFrameAsync(speechToken);
                        continue;
                    }
                    PcmAudio pcm = await _owner.Client.SynthesizeAsync(text, speechToken);
                    speechToken.ThrowIfCancellationRequested();
                    if (!string.IsNullOrEmpty(_audioDiagnosticsDirectory) && pcm.Samples.Length > 0)
                        await SpeechDiagnostics.WriteAsync(_audioDiagnosticsDirectory,
                            "companion-" + (++_playbackDiagnosticIndex).ToString("D3"), pcm.Samples, pcm.SampleRate, text);
                    speechToken.ThrowIfCancellationRequested();
                    float[] samples = await Task.Run(() =>
                    {
                        var values = new float[pcm.Samples.Length / 2];
                        for (int i = 0; i < values.Length; i++)
                            values[i] = (short)(pcm.Samples[i * 2] | pcm.Samples[i * 2 + 1] << 8) / 32768f;
                        return values;
                    }, speechToken);
                    speechToken.ThrowIfCancellationRequested();
                    if (samples.Length == 0) continue;
                    _playingClip = AudioClip.Create("Companion speech", samples.Length, 1, pcm.SampleRate, false);
                    _playingClip.SetData(samples, 0);
                    _playingText = text;
                    _subtitle = text;
                    _status = Ui("speaking");
                    _audio.clip = _playingClip;
                    _audio.Play();
                    while (_audio.isPlaying) await Awaitable.NextFrameAsync(speechToken);
                    speechToken.ThrowIfCancellationRequested();
                    _delivered.Append(text).Append(' ');
                    ReleaseClip();
                }
                }
                catch (OperationCanceledException) when (_speech.IsCancellationRequested && _state.IsComplete && !token.IsCancellationRequested) { }
                // The final config_set may still be sending its result. Never reset that
                // session or advance to generation until delivery/inference has succeeded.
                await AwaitSuccessfulTurnAsync(dialogue);
                token.ThrowIfCancellationRequested();
                if (!_state.IsComplete && _delivered.Length == 0) throw new InvalidOperationException("EMPTY_ASSISTANT_SPEECH");
            }
            finally
            {
                _speech.Dispose();
                _speech = null;
                // Cancellation invalidates the socket session; observe the cancelled generator before reset.
                _turn.Cancel();
                try { await dialogue; } catch (Exception) { }
            }
        }

        private static async Task<OrchestratorJob> AwaitSuccessfulTurnAsync(Task<OrchestratorJob> dialogue)
        {
            OrchestratorJob final = await dialogue;
            if (final.State != "succeeded")
                throw (Exception)final.Error ?? new InvalidOperationException("TEXT_" + final.State.ToUpperInvariant());
            return final;
        }

        private void RecordPartialPlayback()
        {
            if (_playingClip == null || _playingText == null || !_audio.isPlaying) return;
            string prefix = SpeechChunks.EstimatedPrefix(_playingText, _audio.timeSamples, _playingClip.samples);
            if (prefix.Length > 0) _delivered.Append(prefix).Append(' ');
        }

        private void ReleaseClip()
        {
            if (!_destroyed && _audio != null) _audio.clip = null;
            if (_playingClip != null) Destroy(_playingClip);
            _playingClip = null;
            _playingText = null;
        }

        private void Cancel()
        {
            _state?.AcceptTools(false);
            _lifetime?.Cancel();
            if (!_destroyed) _audio?.Stop();
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
            if (_destroyed) return;
            _destroyed = true;
            _application.Cancel();
            Application.wantsToQuit -= WantsToQuit;
            Cancel();
            _owner?.Dispose(); // Kill-on-close fallback when Play Mode ends without awaiting cleanup.
            _shutdown = DisposeApplicationAsync();
        }

        private async Task DisposeApplicationAsync()
        {
            try
            {
                if (_run != null) await _run;
                await ShutdownCompanionAsync();
            }
            finally { _application.Dispose(); }
        }

        private void OnDisable()
        {
            if (_running) Cancel();
            _reviewOpen = _waitingForText = false;
            _approve = _reject = _continue = _next = false;
            _pendingAudio.Clear();
            _subtitle = _player = _typedText = "";
            _preview?.Clear();
        }
    }
}