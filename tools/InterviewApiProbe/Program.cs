using EchoCradle.Interview;
using EchoCradle.Orchestration;
using Newtonsoft.Json;
using Newtonsoft.Json.Linq;

// Opt-in cached-model probe: compile and exercise the game's actual transport and authority.
string root = Path.GetFullPath(args.Length > 0 ? args[0] : ".");
JObject config = JObject.Parse(await File.ReadAllTextAsync(
    Path.Combine(root, "Unity/Assets/Data/Interview.json")));
using var budget = new CancellationTokenSource(TimeSpan.FromMinutes(8));
OwnedOrchestrator owner = await OwnedOrchestrator.StartAsync(
    (string)config["service"]["pythonExecutable"],
    (string)config["service"]["packageRoot"], budget.Token);
try
{
    var state = new InterviewState((JObject)config["schema"]);
    await owner.Client.ResetSessionAsync(state.ToolDeclarations(), state.SessionHandler(), budget.Token);
    var history = new JArray();
    foreach (string reply in new[] { null, "My name is Alex.",
        "A medieval fantasy world with floating islands.", "Your name is Echo.",
        "Yes, I insist. Your name is Echo." })
    {
        if (reply != null) history.Add(InterviewState.Message("user", reply));
        state.AcceptTools(true);
        OrchestratorJob result;
        try
        {
            result = await owner.Client.TextAsync(
                state.Messages(history, (JObject)config["prompts"], false, false),
                (int)config["generation"]["maxTokens"],
                (float)config["generation"]["temperature"],
                (int)config["generation"]["deadlineMs"], null, budget.Token);
        }
        finally { state.AcceptTools(false); }
        Console.WriteLine(JsonConvert.SerializeObject(new
        {
            user = reply, result.State, result.Text, result.Calls,
            recorded = state.Snapshot(), missing = state.Missing()
        }));
        history.Add(InterviewState.Message("assistant", result.Text));
    }
    if (state.Missing().Count > 0)
        throw new InvalidOperationException("Live interview did not record all required fields.");
}
finally { await owner.CloseAsync(); }