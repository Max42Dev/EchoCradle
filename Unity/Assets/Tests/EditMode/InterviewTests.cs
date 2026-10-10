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
        state.AcceptTools(true);
        foreach (string field in new[] { "username", "style", "ai_name" })
            invoke("config_set", new JObject { ["field"] = field, ["value"] = "first" });
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
            var invoke = state.SessionHandler();
            SaveGame save = await SaveGame.CreateAsync(root, (JObject)config["save"], CancellationToken.None);
            try { await save.CompleteAsync(state.Snapshot(), state, CancellationToken.None); Assert.Fail(); }
            catch (InvalidOperationException) { }
            JObject session = JObject.Parse(File.ReadAllText(Path.Combine(save.Folder, "session.json")));
            Assert.That((string)session["status"], Is.EqualTo("interview"));
            Assert.That(File.Exists(Path.Combine(save.Folder, "config.json")), Is.False);
            state.AcceptTools(true);
            foreach (string field in new[] { "username", "style", "ai_name" })
                invoke("config_set", new JObject { ["field"] = field, ["value"] = "test" });
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