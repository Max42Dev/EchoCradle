using System;
using System.Collections;
using System.IO;
using Newtonsoft.Json;
using Newtonsoft.Json.Linq;
using UnityEngine;

namespace EchoCradle.Interview
{
    /// <summary>Opt-in real-service automation. No fake tools, seeded outputs or bypassed review gates.</summary>
    public sealed class CharacterSmokeDriver : MonoBehaviour
    {
        private InterviewController _controller;
        private string _output;
        private int _capture;
        private bool _failed;

        private IEnumerator Start()
        {
            _controller = GetComponent<InterviewController>();
            string[] args = Environment.GetCommandLineArgs();
            if (Array.IndexOf(args, "--character-smoke") < 0) { enabled = false; yield break; }
            JObject transcript;
            try
            {
                string source = _controller.LaunchOptions.SmokeTranscript;
                transcript = source == null ? DefaultTranscript() : JObject.Parse(File.ReadAllText(source));
                _output = _controller.LaunchOptions.SmokeOutput ??
                    Path.Combine(Application.persistentDataPath, "CharacterSmoke", DateTime.UtcNow.ToString("yyyyMMdd-HHmmss"));
                _output = Path.GetFullPath(_output);
                Directory.CreateDirectory(_output);
                File.WriteAllText(Path.Combine(_output, "transcript.json"), transcript.ToString(Formatting.Indented));
                File.WriteAllText(Path.Combine(_output, "events.jsonl"), "");
            }
            catch (Exception error) { Debug.LogError("CHARACTER_SMOKE_FAILED setup: " + error.Message); yield break; }

            _controller.BeginNewGame();
            float deadline = Time.realtimeSinceStartup + ((float?)transcript["timeoutSeconds"] ?? 5400);
            int configurationTurn = 0, descriptionTurn = 0, sessionTurn = 0, session = 0, rejected = 0;
            int rejectCount = (int?)transcript["rejectImages"] ?? 0;
            string previousStage = null;
            while (!_controller.IsFinished && !_controller.HasError && Time.realtimeSinceStartup < deadline)
            {
                CharacterCreationState flow = _controller.Creation;
                string stage = flow == null ? "Startup" : flow.Stage + ":" + flow.DescriptionSession + ":" + flow.ClipIndex;
                if (stage != previousStage)
                {
                    previousStage = stage;
                    Debug.Log("CHARACTER_SMOKE_STAGE " + stage);
                    Capture(stage);
                }
                if (flow != null && session != flow.DescriptionSession)
                { session = flow.DescriptionSession; sessionTurn = 0; }
                if (_controller.WaitingForText)
                {
                    JArray responses = flow?.Stage == CharacterCreationState.Phase.Configuration ?
                        transcript["configuration"] as JArray : transcript["descriptions"] as JArray;
                    int turn = flow?.Stage == CharacterCreationState.Phase.Configuration ? configurationTurn : descriptionTurn;
                    if (responses == null || responses.Count == 0) { _failed = true; break; }
                    // Advance globally across description sessions; repeat only the last response.
                    string response = (string)responses[Math.Min(turn, responses.Count - 1)];
                    string agent = _controller.Subtitle;
                    if (_controller.SubmitText(response))
                    {
                        if (flow.Stage == CharacterCreationState.Phase.Configuration) ++configurationTurn;
                        else { ++descriptionTurn; ++sessionTurn; }
                        File.AppendAllText(Path.Combine(_output, "events.jsonl"), new JObject {
                            ["utc"] = DateTime.UtcNow.ToString("O"), ["stage"] = stage,
                            ["session"] = session, ["responseIndex"] = Math.Min(turn, responses.Count - 1),
                            ["sessionTurn"] = sessionTurn, ["agent"] = agent, ["user"] = response
                        }.ToString(Formatting.None) + Environment.NewLine);
                        Debug.Log("CHARACTER_SMOKE_SUBMIT session=" + session + " turn=" + turn);
                    }
                    yield return null;
                    continue;
                }
                if (flow != null && _controller.CanReview &&
                    (flow.Stage == CharacterCreationState.Phase.ImageReview ||
                     flow.Stage == CharacterCreationState.Phase.StaticReview ||
                     flow.Stage == CharacterCreationState.Phase.AnimationReview))
                {
                    // Keep the actual production preview visible for screenshots/animation frames.
                    float visibleUntil = Time.realtimeSinceStartup + ((float?)transcript["reviewSeconds"] ?? 2);
                    while (Time.realtimeSinceStartup < visibleUntil) yield return null;
                    Capture("review-" + stage);
                    yield return new WaitForEndOfFrame();
                    if (flow.Stage == CharacterCreationState.Phase.ImageReview)
                    {
                        if (rejected < rejectCount) { ++rejected; _controller.RejectImage(); }
                        else _controller.ApproveImage();
                    }
                    else if (flow.Stage == CharacterCreationState.Phase.StaticReview) _controller.ContinueToAnimation();
                    else _controller.NextAnimation();
                }
                yield return null;
            }
            bool success = !_failed && _controller.IsFinished && !_controller.HasError;
            Capture(success ? "ready" : "failed");
            File.WriteAllText(Path.Combine(_output, "result.json"), new JObject {
                ["success"] = success, ["saveFolder"] = _controller.SaveFolder,
                ["state"] = _controller.Creation?.Snapshot(), ["utc"] = DateTime.UtcNow.ToString("O")
            }.ToString(Formatting.Indented));
            Debug.Log(success ? "CHARACTER_SMOKE_PASSED " + _output : "CHARACTER_SMOKE_FAILED " + _output);
            // Deliberately do not quit: the parent owns player/editor lifetime.
        }

        private void Capture(string stage)
        {
            string name = (++_capture).ToString("D3") + "-" + stage.Replace(':', '-') + ".png";
            ScreenCapture.CaptureScreenshot(Path.Combine(_output, name));
        }

        private static JObject DefaultTranscript() => new JObject {
            ["configuration"] = new JArray("My name is Mira. The world is a colourful medieval fantasy world. Your companion name is Pip. Please record all three preferences.",
                "I confirm: username Mira; style colourful medieval fantasy; ai_name Pip. Please record these preferences."),
            ["descriptions"] = new JArray("My character is an adult female human explorer with short auburn hair, green eyes, a blue tunic, brown trousers and leather boots. Full body, neutral standing pose. Please record this appearance."),
            ["rejectImages"] = 0, ["reviewSeconds"] = 2, ["timeoutSeconds"] = 5400
        };
    }
}