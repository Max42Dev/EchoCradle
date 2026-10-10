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
        var args = new JObject { ["field"] = "username", ["value"] = " Ada " };
        Assert.That(state.Invoke("config_set", args)["error"], Is.Not.Null);
        state.AcceptTools(true);
        Assert.That(state.Invoke("config_set", args)["error"], Is.Null);
        Assert.That((string)state.Snapshot()["username"], Is.EqualTo("Ada"));
        args["field"] = "unknown";
        Assert.That(state.Invoke("config_set", args)["error"], Is.Not.Null);
        args["field"] = "username";
        args["value"] = new string('a', 41);
        Assert.That(state.Invoke("config_set", args)["error"], Is.Not.Null);
        args["value"] = "A\nB";
        Assert.That(state.Invoke("config_set", args)["error"], Is.Not.Null);
        state.AcceptTools(false);
        args["value"] = "Bob";
        Assert.That(state.Invoke("config_set", args)["error"], Is.Not.Null);
        Assert.That((string)state.Snapshot()["username"], Is.EqualTo("Ada"));
    }

    [Test]
    public void State_CorrectionsReplaceValuesAndCompletionIsValidated()
    {
        var state = new InterviewState((JObject)Configuration()["schema"]);
        Assert.Throws<InvalidOperationException>(() => state.ValidateComplete(state.Snapshot()));
        state.AcceptTools(true);
        foreach (string field in new[] { "username", "style", "ai_name" })
            state.Invoke("config_set", new JObject { ["field"] = field, ["value"] = "first" });
        state.Invoke("config_set", new JObject { ["field"] = "username", ["value"] = "corrected" });
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
    public async Task Silero_CachedNativeRuntimeProcessesSilenceOffMainThread()
    {
        JObject config = Configuration();
        await Task.Run(() =>
        {
            using (var detector = new SileroDetector((JObject)config["vad"]))
                for (int i = 0; i < 100; i++) Assert.That(detector.Process(new float[320]), Is.False);
        });
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
    public async Task Save_InvalidStateNeverBecomesReadyAndValidStatePersistsSeed()
    {
        string root = Path.Combine(Path.GetTempPath(), "EchoCradle-test-" + Guid.NewGuid().ToString("N"));
        try
        {
            JObject config = Configuration();
            var state = new InterviewState((JObject)config["schema"]);
            SaveGame save = await SaveGame.CreateAsync(root, (JObject)config["save"], CancellationToken.None);
            try { await save.CompleteAsync(state.Snapshot(), state, CancellationToken.None); Assert.Fail(); }
            catch (InvalidOperationException) { }
            JObject session = JObject.Parse(File.ReadAllText(Path.Combine(save.Folder, "session.json")));
            Assert.That((string)session["status"], Is.EqualTo("interview"));
            Assert.That(File.Exists(Path.Combine(save.Folder, "config.json")), Is.False);
            state.AcceptTools(true);
            foreach (string field in new[] { "username", "style", "ai_name" })
                state.Invoke("config_set", new JObject { ["field"] = field, ["value"] = "test" });
            await save.CompleteAsync(state.Snapshot(), state, CancellationToken.None);
            JObject ready = JObject.Parse(File.ReadAllText(Path.Combine(save.Folder, "session.json")));
            Assert.That((string)ready["status"], Is.EqualTo("ready"));
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