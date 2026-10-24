using System;
using System.IO;
using System.Threading;
using System.Threading.Tasks;
using EchoCradle.Interview;
using Newtonsoft.Json.Linq;
using NUnit.Framework;
using UnityEngine;

public sealed class InterviewTests
{
    private JObject Configuration() => JObject.Parse(File.ReadAllText(Path.Combine(Application.dataPath, "Data/Interview.json")));

    [Test]
    public void State_RequiresActiveTurnAndRejectsUnknownOrInvalidValues()
    {
        var state = new InterviewState((JObject)Configuration()["schema"]);
        var invoke = state.SessionHandler();
        var args = new JObject { ["field"] = "username", ["value"] = " Ada " };
        Assert.That(invoke("config_set", args)["error"], Is.Not.Null);
        state.AcceptTools(true);
        Assert.That(invoke("config_set", args)["error"], Is.Null);
        Assert.That((string)state.Snapshot()["username"], Is.EqualTo("Ada"));
        args["field"] = "unknown";
        Assert.That(invoke("config_set", args)["error"], Is.Not.Null);
        args["field"] = "username";
        args["value"] = new string('a', 41);
        Assert.That(invoke("config_set", args)["error"], Is.Not.Null);
        args["value"] = "A\nB";
        Assert.That(invoke("config_set", args)["error"], Is.Not.Null);
        state.AcceptTools(false);
        args["value"] = "Bob";
        Assert.That(invoke("config_set", args)["error"], Is.Not.Null);
        Assert.That((string)state.Snapshot()["username"], Is.EqualTo("Ada"));
    }

    [Test]
    public void State_CorrectionsReplaceValuesAndCompletionIsValidated()
    {
        var state = new InterviewState((JObject)Configuration()["schema"]);
        var invoke = state.SessionHandler();
        Assert.Throws<InvalidOperationException>(() => state.ValidateComplete(state.Snapshot()));
        Assert.That(state.IsComplete, Is.False);
        state.AcceptTools(true);
        foreach (string field in new[] { "username", "style", "ai_name" })
            invoke("config_set", new JObject { ["field"] = field, ["value"] = "first" });
        Assert.That(state.IsComplete, Is.True, "Only the three required interview fields determine completion.");
        state.AcceptTools(false);
        Assert.That(state.IsComplete, Is.True, "Revoking tools must not undo completion.");
        state.AcceptTools(true);
        invoke("config_set", new JObject { ["field"] = "username", ["value"] = "corrected" });
        Assert.That(state.Missing().Count, Is.Zero);
        state.ValidateComplete(state.Snapshot());
        Assert.That((string)state.Snapshot()["username"], Is.EqualTo("corrected"));
        JObject snapshot = state.Snapshot();
        snapshot["bad"] = "value";
        Assert.Throws<InvalidOperationException>(() => state.ValidateComplete(snapshot));
    }

    [Test]
    public void State_ObsoleteSessionCannotMutateNewSession()
    {
        var state = new InterviewState((JObject)Configuration()["schema"]);
        var old = state.SessionHandler();
        var current = state.SessionHandler();
        state.AcceptTools(true);
        var args = new JObject { ["field"] = "username", ["value"] = "Ada" };
        Assert.That(old("config_set", args)["error"], Is.Not.Null);
        Assert.That(current("config_set", args)["error"], Is.Null);
    }

    [Test]
    public void Tools_SdkMetadataAndDomainConstraintsComeFromSourceAndSchema()
    {
        JObject config = Configuration();
        Assert.That(config["tools"], Is.Null);
        var schema = (JObject)config["schema"].DeepClone();
        schema["properties"]["new_preference"] = new JObject { ["type"] = "string", ["maxLength"] = 100 };
        var state = new InterviewState(schema);
        JArray tools = state.ToolDeclarations();
        JObject function = (JObject)tools[0]["function"];
        Assert.That((string)function["name"], Is.EqualTo("config_set"));
        Assert.That((string)function["description"], Does.Contain("confirmed player preference"));
        Assert.That((bool)function["parameters"]["additionalProperties"], Is.False);
        Assert.That(function["parameters"]["required"].ToString(), Does.Contain("value"));
        Assert.That(function["parameters"]["properties"]["field"]["enum"].ToString(), Does.Contain("new_preference"));
        tools.Clear();
        Assert.That(state.ToolDeclarations().Count, Is.EqualTo(1));
        var invoke = state.SessionHandler();
        state.AcceptTools(true);
        Assert.That(invoke("config_set", new JObject { ["field"] = "new_preference", ["value"] = "yes" })["error"], Is.Null);
    }

    [Test]
    public void Tools_GeneratedBinderRejectsUnknownExtraMissingAndWrongTypedArguments()
    {
        var state = new InterviewState((JObject)Configuration()["schema"]);
        var invoke = state.SessionHandler();
        state.AcceptTools(true);
        Assert.That(invoke("unknown", new JObject())["error"], Is.Not.Null);
        Assert.That(invoke("config_set", null)["error"], Is.Not.Null);
        Assert.That(invoke("config_set", new JObject { ["field"] = "username" })["error"], Is.Not.Null);
        Assert.That(invoke("config_set", new JObject { ["field"] = "username", ["value"] = 42 })["error"], Is.Not.Null);
        Assert.That(invoke("config_set", new JObject { ["field"] = "username", ["value"] = "Ada", ["extra"] = true })["error"], Is.Not.Null);
        Assert.That(state.Snapshot().Count, Is.Zero);
    }

    [Test]
    public async Task Silero_CachedNativeRuntimeProcessesSilenceOffMainThread()
    {
        JObject config = Configuration();
        await Task.Run(() =>
        {
            using (var detector = new SileroDetector((JObject)config["vad"]))
            {
                detector.SetNoiseFloor(0.004);
                for (int i = 0; i < 100; i++) Assert.That(detector.Process(new float[320]), Is.False);
            }
        });
    }

    [Test]
    public void Calibration_UsesSampleDurationMedianAndDoesNotMutatePcm()
    {
        var calibration = new NoiseFloorCalibration(320, 0.6, 4);
        var block = new float[320];
        for (int i = 0; i < block.Length; i++) block[i] = 0.001f;
        for (int i = 0; i < 29; i++) calibration.Add(block);
        Assert.That(calibration.IsComplete, Is.False);
        var transient = new float[320];
        for (int i = 0; i < transient.Length; i++) transient[i] = 0.5f;
        calibration.Add(transient);
        Assert.That(calibration.IsComplete, Is.True);
        Assert.That(calibration.Threshold, Is.EqualTo(0.004).Within(0.000001));
        foreach (float sample in block) Assert.That(sample, Is.EqualTo(0.001f));
        foreach (float sample in transient) Assert.That(sample, Is.EqualTo(0.5f));
    }

    [Test]
    public void Calibration_EvenMedianAndSilentRoomAreHandled()
    {
        var calibration = new NoiseFloorCalibration(160, 0.02, 4);
        var first = new float[160];
        var second = new float[160];
        for (int i = 0; i < first.Length; i++) { first[i] = 0.001f; second[i] = 0.003f; }
        calibration.Add(first);
        calibration.Add(second);
        Assert.That(calibration.Threshold, Is.EqualTo(0.008).Within(0.000001));
        var silent = new NoiseFloorCalibration(320, 0.02, 4);
        silent.Add(new float[320]);
        Assert.That(silent.Threshold, Is.Zero);
    }

    [Test]
    public void Calibration_BelowFloorNeverBoostsButSpeechRetainsBoundedGain()
    {
        Assert.That(NoiseFloorCalibration.InputGain(0.001, 0.001, 0.002, 0.004, 0.1, 0.5, 100), Is.EqualTo(1));
        Assert.That(NoiseFloorCalibration.InputGain(0.004, 0.004, 0.008, 0.004, 0.1, 0.5, 100), Is.EqualTo(1));
        Assert.That(NoiseFloorCalibration.InputGain(0.01, 0.01, 0.02, 0.004, 0.1, 0.5, 100), Is.EqualTo(10));
        Assert.That(NoiseFloorCalibration.InputGain(0, 0, 0, 0, 0.1, 0.5, 100), Is.EqualTo(1));
        Assert.That(NoiseFloorCalibration.InputGain(0.3, 0.3, 1, 0.4, 0.1, 0.5, 100), Is.EqualTo(0.5));
    }

    [Test]
    public void Capture_Native48kBlocksPreserve16kSampleTimingAcrossBoundaries()
    {
        var native = new float[960];
        var output = new float[320];
        for (int block = 0; block < 3; block++)
        {
            for (int i = 0; i < native.Length; i++) native[i] = block * native.Length + i;
            MicrophoneResampler.ConvertBlock(native, output);
            for (int i = 0; i < output.Length; i++)
                Assert.That(output[i], Is.EqualTo(block * 960 + i * 3));
        }
    }

    [Test]
    public void Capture_Native44100BlockInterpolatesWithoutChangingDuration()
    {
        var native = new float[882];
        var output = new float[320];
        for (int i = 0; i < native.Length; i++) native[i] = i;
        MicrophoneResampler.ConvertBlock(native, output);
        for (int i = 0; i < output.Length; i++)
            Assert.That(output[i], Is.EqualTo(i * 44100f / 16000f).Within(0.001f));
    }

    [Test]
    public void Capture_OnsetsAndUtterancesRemainOrderedAcrossOnePoll()
    {
        var handoff = new CaptureHandoff();
        byte[] first = { 1, 0 }, second = { 2, 0 };
        handoff.PublishOnset();
        handoff.PublishUtterance(first);
        handoff.PublishOnset();
        handoff.PublishUtterance(second);
        Assert.That(handoff.TryTake(out byte[] onset), Is.True);
        Assert.That(onset, Is.Null);
        Assert.That(handoff.TryTake(out byte[] pcm), Is.True);
        Assert.That(pcm, Is.SameAs(first));
        Assert.That(handoff.TryTake(out onset), Is.True);
        Assert.That(onset, Is.Null);
        Assert.That(handoff.TryTake(out pcm), Is.True);
        Assert.That(pcm, Is.SameAs(second));
        Assert.That(handoff.HasPendingEvents, Is.False);
    }

    [Test]
    public async Task Capture_DequeuedBlockRemainsPendingUntilWorkerPublishesEvents()
    {
        var handoff = new CaptureHandoff();
        var entered = new TaskCompletionSource<bool>();
        var release = new TaskCompletionSource<bool>();
        handoff.QueueSamples(320);
        Task worker = Task.Run(async () =>
        {
            entered.SetResult(true);
            await release.Task;
            handoff.PublishOnset();
            handoff.ProcessedSamples(320);
        });
        await entered.Task;
        try
        {
            Assert.That(handoff.IsProcessing, Is.True);
            Assert.That(handoff.HasPendingEvents, Is.False);
        }
        finally { release.SetResult(true); await worker; }
        Assert.That(handoff.IsProcessing, Is.False);
        Assert.That(handoff.HasPendingEvents, Is.True);
    }

    [Test]
    public void Capture_OverflowIsExplicitAndPollingReleasesCapacity()
    {
        var handoff = new CaptureHandoff();
        handoff.PublishUtterance(new byte[2]);
        handoff.PublishUtterance(new byte[2]);
        Assert.Throws<InvalidOperationException>(() => handoff.PublishUtterance(new byte[2]));
        Assert.That(handoff.TryTake(out _), Is.True);
        Assert.DoesNotThrow(() => handoff.PublishUtterance(new byte[2]));
    }

    [Test]
    public void Speech_DiagnosticWavePreservesExactUploadedPcmAndSampleRate()
    {
        byte[] pcm = { 0, 0, 255, 127, 0, 128, 1, 0 };
        byte[] wav = SpeechDiagnostics.Wave(pcm, 16000);
        Assert.That(System.Text.Encoding.ASCII.GetString(wav, 0, 4), Is.EqualTo("RIFF"));
        Assert.That(System.Text.Encoding.ASCII.GetString(wav, 8, 4), Is.EqualTo("WAVE"));
        Assert.That(BitConverter.ToInt32(wav, 24), Is.EqualTo(16000));
        Assert.That(BitConverter.ToInt16(wav, 22), Is.EqualTo(1));
        Assert.That(BitConverter.ToInt32(wav, 40), Is.EqualTo(pcm.Length));
        var payload = new byte[pcm.Length];
        Array.Copy(wav, 44, payload, 0, payload.Length);
        Assert.That(payload, Is.EqualTo(pcm));
    }

    [Test]
    public void Speech_StreamingChunksAreBoundedAndPartialHistoryIsEstimated()
    {
        var chunks = new SpeechChunks(80, 2400);
        chunks.Add("Hello there. How ");
        chunks.Add("are you?");
        chunks.Flush();
        Assert.That(chunks.TryTake(out string first), Is.True);
        Assert.That(first, Is.EqualTo("Hello there."));
        Assert.That(chunks.TryTake(out string second), Is.True);
        Assert.That(second, Is.EqualTo("How are you?"));
        Assert.That(SpeechChunks.EstimatedPrefix("one two three four", 50, 100), Is.EqualTo("one two"));
        Assert.Throws<InvalidOperationException>(() => chunks.Add(new string('a', 2401)));
    }

    [Test]
    public async Task Save_InvalidStateNeverBecomesDraftAndValidDraftPersistsSeedWithoutReadyMarker()
    {
        string root = Path.Combine(Path.GetTempPath(), "EchoCradle-test-" + Guid.NewGuid().ToString("N"));
        try
        {
            JObject config = Configuration();
            var state = new InterviewState((JObject)config["schema"]);
            var invoke = state.SessionHandler();
            SaveGame save = await SaveGame.CreateAsync(root, (JObject)config["save"], CancellationToken.None);
            try { await save.SaveDraftAsync(state.Snapshot(), state, CancellationToken.None); Assert.Fail(); }
            catch (InvalidOperationException) { }
            JObject session = JObject.Parse(File.ReadAllText(Path.Combine(save.Folder, "session.json")));
            Assert.That((string)session["status"], Is.EqualTo("interview"));
            Assert.That(File.Exists(Path.Combine(save.Folder, "config.json")), Is.False);
            state.AcceptTools(true);
            foreach (string field in new[] { "username", "style", "ai_name" })
                invoke("config_set", new JObject { ["field"] = field, ["value"] = "test" });
            await save.SaveDraftAsync(state.Snapshot(), state, CancellationToken.None);
            JObject ready = JObject.Parse(File.ReadAllText(Path.Combine(save.Folder, "session.json")));
            Assert.That((string)ready["status"], Is.EqualTo("character_creation"));
            Assert.That(ready["completedUtc"], Is.Null);
            Assert.That(ready["seed"], Is.EqualTo(session["seed"]));
            state.ValidateComplete(JObject.Parse(File.ReadAllText(Path.Combine(save.Folder, "config.json"))));
            Assert.That(Directory.GetFiles(save.Folder, "*.tmp"), Is.Empty);
        }
        finally { if (Directory.Exists(root)) Directory.Delete(root, true); }
    }

    [TestCase("../escape")]
    [TestCase("C:/escape")]
    public void Save_PathTraversalIsRejected(string folder)
    {
        JObject settings = (JObject)Configuration()["save"].DeepClone();
        settings["folder"] = folder;
        Assert.Throws<ArgumentException>(() => SaveGame.CreateAsync(Path.GetTempPath(), settings, CancellationToken.None));
    }
}