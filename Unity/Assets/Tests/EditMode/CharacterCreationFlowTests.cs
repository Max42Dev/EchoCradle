using System;
using System.Collections.Generic;
using System.IO;
using System.Reflection;
using System.Threading;
using System.Threading.Tasks;
using EchoCradle.Interview;
using EchoCradle.Orchestration;
using Newtonsoft.Json.Linq;
using NUnit.Framework;
using UnityEngine;

public sealed class CharacterCreationFlowTests
{
    private static readonly byte[] Image = { 1, 2, 3 };
    private static readonly byte[] Model = { 4, 5, 6 };
    private static readonly byte[] Animated = { 7, 8, 9 };

    [Test]
    public async Task Completion_LocalToolMutationWaitsForDeliveryAndSuccessfulTurn()
    {
        var state = new InterviewState((JObject)Read("PlayerDescription.json")["schema"]);
        state.AcceptTools(true);
        state.SessionHandler()("config_set", new JObject
        { ["field"] = "description", ["value"] = "An explorer in blue clothes." });
        Assert.That(state.IsComplete, Is.True);
        var delivery = new TaskCompletionSource<OrchestratorJob>();
        Task<OrchestratorJob> completion = AwaitTurn(delivery.Task);
        Assert.That(completion.IsCompleted, Is.False, "Local recording must not short-circuit result delivery.");
        OrchestratorJob succeeded = Job("succeeded");
        delivery.SetResult(succeeded);
        Assert.That(await completion, Is.SameAs(succeeded));
    }

    [TestCase("SOCKET_LOST")]
    [TestCase("TOOL_TIMEOUT")]
    [TestCase("TOOL_HANDLER_FAILED")]
    [TestCase("PROTOCOL_ERROR")]
    public void Completion_TransportAndToolFailuresAreNotSuccessfulCompletion(string code)
    {
        var failure = (OrchestratorException)Activator.CreateInstance(typeof(OrchestratorException),
            BindingFlags.Instance | BindingFlags.NonPublic, null,
            new object[] { code, null, false, null, null, null }, null);
        Assert.That(Assert.ThrowsAsync<OrchestratorException>(async () =>
            await AwaitTurn(Task.FromException<OrchestratorJob>(failure))), Is.SameAs(failure));
    }

    [Test]
    public void Completion_UnrequestedCancellationAndRemoteCancelledJobAreErrors()
    {
        Assert.ThrowsAsync<OperationCanceledException>(async () =>
            await AwaitTurn(Task.FromException<OrchestratorJob>(new OperationCanceledException())));
        Assert.ThrowsAsync<InvalidOperationException>(async () => await AwaitTurn(Task.FromResult(Job("cancelled"))));
        Assert.ThrowsAsync<InvalidOperationException>(async () => await AwaitTurn(Task.FromResult(Job("failed"))));
    }

    [Test]
    public async Task Lifecycle_DisablePreservesApplicationLifetimeAndDestroyDisposesIt()
    {
        var go = new GameObject("Interview lifecycle test");
        go.SetActive(false); // Do not run Awake or provision a real companion.
        var controller = go.AddComponent<InterviewController>();
        var application = (CancellationTokenSource)Field("_application").GetValue(controller);
        using (var run = CancellationTokenSource.CreateLinkedTokenSource(application.Token))
        {
            try
            {
                Field("_lifetime").SetValue(controller, run);
                Field("_running").SetValue(controller, true);
                Field("_waitingForText").SetValue(controller, true);
                Field("_reviewOpen").SetValue(controller, true);
                Field("_subtitle").SetValue(controller, "Old speech");
                Invoke(controller, "OnDisable");
                Assert.That(run.IsCancellationRequested, Is.True);
                Assert.That(application.IsCancellationRequested, Is.False);
                Assert.That(controller.WaitingForText, Is.False);
                Assert.That(controller.CanReview, Is.False);
                Assert.That(controller.Subtitle, Is.Empty);
                Assert.That(controller.BeginNewGame(), Is.False, "Disabled controllers must not start work.");
                using (var next = CancellationTokenSource.CreateLinkedTokenSource(application.Token))
                    Assert.That(next.IsCancellationRequested, Is.False, "A later enable can create a live run.");
                Invoke(controller, "OnDestroy");
                await (Task)Field("_shutdown").GetValue(controller);
                Assert.Throws<ObjectDisposedException>(() => { var unused = application.Token; });
            }
            finally { UnityEngine.Object.DestroyImmediate(go); }
        }
    }

    private static FieldInfo Field(string name) => typeof(InterviewController).GetField(name, BindingFlags.Instance | BindingFlags.NonPublic);
    private static void Invoke(InterviewController controller, string name) =>
        typeof(InterviewController).GetMethod(name, BindingFlags.Instance | BindingFlags.NonPublic).Invoke(controller, null);
    private static Task<OrchestratorJob> AwaitTurn(Task<OrchestratorJob> turn) =>
        (Task<OrchestratorJob>)typeof(InterviewController).GetMethod("AwaitSuccessfulTurnAsync", BindingFlags.Static | BindingFlags.NonPublic)
            .Invoke(null, new object[] { turn });

    private static OrchestratorJob Job(string state)
    {
        var data = new JObject
        {
            ["v"] = 1, ["protocol_version"] = 1, ["service_instance_id"] = Guid.NewGuid().ToString("D"),
            ["job_id"] = Guid.NewGuid().ToString("D"), ["session_id"] = null, ["turn_id"] = null,
            ["kind"] = "text", ["state"] = state, ["revision"] = 1, ["host_pending"] = false,
            ["selected_model"] = null, ["error"] = null,
            ["result"] = state == "succeeded" ? new JObject { ["text"] = "", ["calls"] = new JArray() } : null
        };
        return (OrchestratorJob)Activator.CreateInstance(typeof(OrchestratorJob), BindingFlags.Instance | BindingFlags.NonPublic,
            null, new object[] { data, null, "" }, null);
    }

    [Test]
    public void Gates_RequireApprovalStaticPresentationAndEveryClip()
    {
        var flow = new CharacterCreationState();
        Assert.Throws<InvalidOperationException>(() => flow.ApproveImage());
        flow.ConfigurationRecorded();
        flow.RequestImage("An explorer with auburn hair and blue clothes.");
        Assert.Throws<InvalidOperationException>(() => flow.StartAnimation());
        flow.ImageReceived(Image, "image");
        flow.ApproveImage();
        flow.ModelReceived(Model, "model");
        Assert.Throws<InvalidOperationException>(() => flow.StartAnimation());
        flow.StaticPresented();
        flow.StartAnimation();
        Assert.Throws<InvalidOperationException>(() => flow.AnimationReceived(Animated, "animated", Array.Empty<string>()));
        flow.AnimationReceived(Animated, "animated", new[] { "idle", "walk", "run" });
        Assert.That(flow.CurrentClip, Is.EqualTo("idle"));
        flow.NextClip();
        Assert.That(flow.CurrentClip, Is.EqualTo("walk"));
        flow.NextClip();
        Assert.That(flow.IsReady, Is.False);
        Assert.That(flow.CurrentClip, Is.EqualTo("run"));
        flow.NextClip();
        Assert.That(flow.IsReady, Is.True);
        Assert.Throws<InvalidOperationException>(() => flow.NextClip());
    }

    [Test]
    public void Rejection_RequiresNewDescriptionAndFreshUniqueSeedsWithProvenance()
    {
        var flow = new CharacterCreationState();
        var seeds = new HashSet<uint>();
        flow.ConfigurationRecorded();
        for (int i = 0; i < 32; i++)
        {
            Assert.That(seeds.Add(flow.RequestImage("A described character number " + i)), Is.True);
            flow.ImageReceived(Image, "image-" + i);
            flow.RejectImage();
            Assert.That(flow.Stage, Is.EqualTo(CharacterCreationState.Phase.Description));
            Assert.That(flow.Description, Is.Null);
            Assert.Throws<InvalidOperationException>(() => flow.ApproveImage());
        }
        Assert.That(flow.DescriptionSession, Is.EqualTo(33));
        Assert.That(seeds.Add(flow.RequestImage("A new character wearing a green tunic.")), Is.True);
        flow.ImageReceived(Image, "approved-image");
        Assert.That(seeds.Add(flow.ApproveImage()), Is.True);
        flow.ModelReceived(Model, "model");
        flow.StaticPresented();
        Assert.That(seeds.Add(flow.StartAnimation()), Is.True);
        JArray requests = (JArray)flow.Snapshot()["requests"];
        Assert.That((bool)requests[0]["approved"], Is.False);
        Assert.That((bool)requests[32]["approved"], Is.True);
        Assert.That((string)requests[33]["inputSha256"], Is.EqualTo(CharacterCreationState.Hash(Image)));
        Assert.That((string)requests[34]["inputSha256"], Is.EqualTo(CharacterCreationState.Hash(Model)));
        requests.Clear();
        Assert.That(((JArray)flow.Snapshot()["requests"]).Count, Is.EqualTo(35));
    }

    [Test]
    public void Description_UsesDedicatedBoundedSchemaAndIndependentState()
    {
        JObject configuration = Read("Interview.json");
        JObject description = Read("PlayerDescription.json");
        var first = new InterviewState((JObject)configuration["schema"]);
        var second = new InterviewState((JObject)description["schema"]);
        second.AcceptTools(true);
        Assert.That(second.RecordPreference("username", "Mira")["error"], Is.Not.Null);
        Assert.That(second.RecordPreference("description", new string('x', 2001))["error"], Is.Not.Null);
        Assert.That(second.RecordPreference("description", "An auburn-haired explorer with blue clothes.")["error"], Is.Null);
        Assert.That(second.IsComplete, Is.True);
        Assert.That(first.IsComplete, Is.False);
        JArray messages = second.Messages(new JArray(), (JObject)description["prompts"], false, false);
        Assert.That(messages.ToString(), Does.Contain("NEW conversation"));
    }

    [Test]
    public void Launch_TextOnlyAndVisualConfigResolveWithoutMachinePaths()
    {
        JObject config = Read("Interview.json");
        string root = Path.Combine(Path.GetTempPath(), "EchoCradle", "Unity");
        var options = new InterviewLaunchOptions(config, root,
            new[] { "game.exe", "--text-only", "--character-smoke", "--visual-config", "../visual.json" });
        Assert.That(options.TextOnly, Is.True);
        Assert.That(options.Smoke, Is.True);
        Assert.That(options.VisualConfig, Is.EqualTo(Path.GetFullPath(Path.Combine(root, "../visual.json"))));
        Assert.That(options.PackageRoot, Is.EqualTo(Path.GetFullPath(Path.Combine(root, "../orchestrator"))));
        config["textOnly"] = true;
        Assert.That(new InterviewLaunchOptions(config, root, Array.Empty<string>()).TextOnly, Is.True);
        Assert.Throws<ArgumentException>(() => new InterviewLaunchOptions(config, root, new[] { "--visual-config" }));
    }

    [Test]
    public async Task Save_RemainsDraftUntilLastClipAndVerifiesPersistedHashes()
    {
        string root = Path.Combine(Path.GetTempPath(), "EchoCradle-character-" + Guid.NewGuid().ToString("N"));
        try
        {
            JObject config = Read("Interview.json");
            var state = new InterviewState((JObject)config["schema"]);
            state.AcceptTools(true);
            foreach (string field in new[] { "username", "style", "ai_name" }) state.RecordPreference(field, "test");
            SaveGame save = await SaveGame.CreateAsync(root, (JObject)config["save"], CancellationToken.None);
            await save.SaveDraftAsync(state.Snapshot(), state, CancellationToken.None);
            var flow = new CharacterCreationState();
            flow.ConfigurationRecorded();
            flow.RequestImage("An explorer with blue clothes and brown boots.");
            flow.ImageReceived(Image, "image");
            await save.SaveAppearanceAsync(flow, Image, ".png", CancellationToken.None);
            flow.ApproveImage();
            flow.ModelReceived(Model, "model");
            await save.SaveAppearanceAsync(flow, Model, ".glb", CancellationToken.None);
            flow.StaticPresented();
            flow.StartAnimation();
            flow.AnimationReceived(Animated, "animated", new[] { "idle", "walk" });
            await save.SaveAppearanceAsync(flow, Animated, ".glb", CancellationToken.None);
            flow.NextClip();
            Assert.ThrowsAsync<InvalidOperationException>(async () => await save.CompleteCharacterAsync(flow, CancellationToken.None));
            Assert.That((string)JObject.Parse(File.ReadAllText(Path.Combine(save.Folder, "session.json")))["status"], Is.EqualTo("character_creation"));
            flow.NextClip();
            string modelPath = Path.Combine(save.Folder, "appearance", CharacterCreationState.Hash(Model) + ".glb");
            File.WriteAllBytes(modelPath, Image);
            Assert.ThrowsAsync<InvalidOperationException>(async () => await save.CompleteCharacterAsync(flow, CancellationToken.None));
            File.WriteAllBytes(modelPath, Model);
            await save.CompleteCharacterAsync(flow, CancellationToken.None);
            Assert.That((string)JObject.Parse(File.ReadAllText(Path.Combine(save.Folder, "session.json")))["status"], Is.EqualTo("ready"));
            JObject provenance = JObject.Parse(File.ReadAllText(Path.Combine(save.Folder, "appearance", "appearance.json")));
            Assert.That((int)provenance["reviewedClips"], Is.EqualTo(2));
            Assert.That(Directory.GetFiles(save.Folder, "*.tmp", SearchOption.AllDirectories), Is.Empty);
        }
        finally { if (Directory.Exists(root)) Directory.Delete(root, true); }
    }

    private static JObject Read(string name) => JObject.Parse(File.ReadAllText(Path.Combine(Application.dataPath, "Data", name)));
}