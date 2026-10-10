using System.Collections;
using System.Reflection;
using EchoCradle.Interview;
using NUnit.Framework;
using UnityEngine;
using UnityEngine.SceneManagement;
using UnityEngine.TestTools;

public sealed class StartScreenTests
{
    [UnityTest]
    public IEnumerator StartScene_BlackCameraAndIdleInterviewAreWired()
    {
        LogAssert.Expect(LogType.Log, "Companion preload started.");
        yield return SceneManager.LoadSceneAsync("Start");
        yield return null;
        InterviewController controller = Object.FindFirstObjectByType<InterviewController>();
        Assert.That(controller, Is.Not.Null);
        Camera camera = Object.FindFirstObjectByType<Camera>();
        Assert.That(camera.backgroundColor, Is.EqualTo(Color.black));
        Assert.That(camera.clearFlags, Is.EqualTo(CameraClearFlags.SolidColor));
        Assert.That(controller.GetComponent<AudioSource>().isPlaying, Is.False);
        FieldInfo running = typeof(InterviewController).GetField("_running", BindingFlags.NonPublic | BindingFlags.Instance);
        Assert.That((bool)running.GetValue(controller), Is.False);
        FieldInfo settings = typeof(InterviewController).GetField("settings", BindingFlags.NonPublic | BindingFlags.Instance);
        Assert.That(((InterviewSettings)settings.GetValue(controller)).Read()["ui"]["loadGame"].ToString(), Is.EqualTo("Load game"));
        controller.enabled = false;
        yield return null;
        LogAssert.NoUnexpectedReceived();
    }
}