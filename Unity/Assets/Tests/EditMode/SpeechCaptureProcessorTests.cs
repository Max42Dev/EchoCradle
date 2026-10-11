using System;
using System.Collections.Generic;
using System.Threading.Tasks;
using EchoCradle.Interview;
using NUnit.Framework;

namespace EchoCradle.Tests
{

public sealed class SpeechCaptureProcessorTests
{
    private const int BlockSamples = 320;

    private static float[] Block(float value)
    {
        var block = new float[BlockSamples];
        for (int i = 0; i < block.Length; i++) block[i] = value;
        return block;
    }

    private static void Feed(SpeechCaptureProcessor processor, CaptureHandoff handoff, float[] block)
    {
        handoff.QueueSamples(block.Length);
        processor.ProcessBlock(block);
    }

    private static void AssertPcm(byte[] pcm, params short[] blocks)
    {
        Assert.That(pcm.Length, Is.EqualTo(blocks.Length * BlockSamples * 2));
        for (int i = 0; i < pcm.Length / 2; i++)
            Assert.That((short)(pcm[i * 2] | pcm[i * 2 + 1] << 8), Is.EqualTo(blocks[i / BlockSamples]), "Sample " + i);
    }

    [Test]
    public void Replay_TwoUtterances_PublishesOrderedOnsetsAndExactOwnedPcm()
    {
        var handoff = new CaptureHandoff();
        var decisions = new Queue<bool>(new[] { false, false, true, true, true, false, false, true, true, false, false });
        var processor = new SpeechCaptureProcessor(BlockSamples, 960, 3200, 0.04f, 0.04f,
            block => decisions.Dequeue(), handoff);
        // Idle pre-roll wraps; every block has a known PCM value. Reuse one borrowed buffer.
        float[] values = { 0.75f, 0.25f, 0.5f, -0.5f, 1.5f, -1.5f, 0, -0.25f, 0.25f, 0, 0 };
        float[] buffer = Block(0);
        foreach (float value in values)
        {
            for (int i = 0; i < buffer.Length; i++) buffer[i] = value;
            Feed(processor, handoff, buffer);
        }
        Array.Clear(buffer, 0, buffer.Length);
        Assert.That(decisions, Is.Empty);
        Assert.That(processor.IsSpeaking, Is.False);
        Assert.That(processor.HasPendingAudio, Is.True, "Undelivered events still block admission.");
        Assert.That(handoff.TryTake(out byte[] onset), Is.True);
        Assert.That(onset, Is.Null);
        Assert.That(handoff.TryTake(out byte[] first), Is.True);
        AssertPcm(first, 8191, 16383, -16383, 32767, -32767, 0);
        Assert.That(handoff.TryTake(out onset), Is.True);
        Assert.That(onset, Is.Null);
        Assert.That(handoff.TryTake(out byte[] second), Is.True);
        AssertPcm(second, -8191, 8191, 0, 0);
        Assert.That(first, Is.Not.SameAs(second));
        Assert.That(handoff.TryTake(out _), Is.False);
        Assert.That(processor.HasPendingAudio, Is.False);
    }

    [Test]
    public void Replay_UnconfirmedSpeech_ResetsOnsetAndRetainsChronologicalPreRoll()
    {
        var handoff = new CaptureHandoff();
        var decisions = new Queue<bool>(new[] { true, false, true, true, false });
        var processor = new SpeechCaptureProcessor(BlockSamples, 960, 3200, 0.04f, 0.02f,
            block => decisions.Dequeue(), handoff);
        Feed(processor, handoff, Block(1));
        Assert.That(processor.HasPendingAudio, Is.True);
        Assert.That(processor.IsSpeaking, Is.False);
        Assert.That(handoff.HasPendingEvents, Is.False);
        Feed(processor, handoff, Block(0));
        Assert.That(processor.HasPendingAudio, Is.False);
        Feed(processor, handoff, Block(0.25f));
        Assert.That(handoff.HasPendingEvents, Is.False);
        Feed(processor, handoff, Block(0.5f));
        Assert.That(processor.IsSpeaking, Is.True);
        Feed(processor, handoff, Block(0));
        Assert.That(handoff.TryTake(out byte[] onset), Is.True);
        Assert.That(onset, Is.Null);
        Assert.That(handoff.TryTake(out byte[] pcm), Is.True);
        AssertPcm(pcm, 0, 8191, 16383, 0);
        Assert.That(processor.HasPendingAudio, Is.False);
    }

    [Test]
    public void Replay_SpeechResumesBeforeSilenceThreshold_DoesNotSplitUtterance()
    {
        var handoff = new CaptureHandoff();
        var decisions = new Queue<bool>(new[] { true, false, true, false, false });
        var processor = new SpeechCaptureProcessor(BlockSamples, 320, 3200, 0.02f, 0.04f,
            block => decisions.Dequeue(), handoff);
        foreach (float value in new[] { 0.25f, 0, 0.5f, 0, 0 }) Feed(processor, handoff, Block(value));
        Assert.That(handoff.TryTake(out byte[] onset), Is.True);
        Assert.That(onset, Is.Null);
        Assert.That(handoff.TryTake(out byte[] pcm), Is.True);
        AssertPcm(pcm, 8191, 0, 16383, 0, 0);
        Assert.That(handoff.HasPendingEvents, Is.False);
    }

    [Test]
    public void Replay_MaximumLength_TruncatesFinalBlockWithoutOverflow()
    {
        var handoff = new CaptureHandoff();
        var processor = new SpeechCaptureProcessor(BlockSamples, 320, 480, 0.02f, 1,
            block => true, handoff);
        Feed(processor, handoff, Block(0.25f));
        Feed(processor, handoff, Block(0.5f));
        Assert.That(handoff.TryTake(out byte[] onset), Is.True);
        Assert.That(onset, Is.Null);
        Assert.That(handoff.TryTake(out byte[] pcm), Is.True);
        Assert.That(pcm.Length, Is.EqualTo(960));
        for (int i = 0; i < 480; i++)
            Assert.That((short)(pcm[i * 2] | pcm[i * 2 + 1] << 8), Is.EqualTo(i < 320 ? 8191 : 16383));
        Assert.That(processor.HasPendingAudio, Is.False);
    }

    [Test]
    public async Task Replay_WorkerPausedInsideVad_WatermarkBlocksAdmissionUntilPublication()
    {
        var handoff = new CaptureHandoff();
        var entered = new TaskCompletionSource<bool>(TaskCreationOptions.RunContinuationsAsynchronously);
        var release = new TaskCompletionSource<bool>(TaskCreationOptions.RunContinuationsAsynchronously);
        var processor = new SpeechCaptureProcessor(BlockSamples, 320, 3200, 0.02f, 0.02f,
            block => { entered.SetResult(true); release.Task.GetAwaiter().GetResult(); return true; }, handoff);
        handoff.QueueSamples(BlockSamples);
        Task worker = Task.Run(() => processor.ProcessBlock(Block(0.25f)));
        try
        {
            Assert.That(await Task.WhenAny(entered.Task, worker, Task.Delay(2000)), Is.SameAs(entered.Task));
            Assert.That(processor.IsSpeaking, Is.False);
            Assert.That(handoff.HasPendingEvents, Is.False);
            Assert.That(processor.HasPendingAudio, Is.True);
        }
        finally { release.TrySetResult(true); await worker; }
        Assert.That(handoff.IsProcessing, Is.False);
        Assert.That(processor.IsSpeaking, Is.True);
        Assert.That(handoff.TryTake(out byte[] onset), Is.True);
        Assert.That(onset, Is.Null);
        Assert.That(processor.HasPendingAudio, Is.True, "Active speech still blocks admission after onset is drained.");
    }

    [Test]
    public void Replay_InvalidBlock_DoesNotCallVadOrAdvanceWatermark()
    {
        var handoff = new CaptureHandoff();
        int calls = 0;
        var processor = new SpeechCaptureProcessor(BlockSamples, 320, 3200, 0.02f, 0.02f,
            block => { calls++; return true; }, handoff);
        handoff.QueueSamples(BlockSamples);
        Assert.Throws<ArgumentException>(() => processor.ProcessBlock(new float[319]));
        Assert.That(calls, Is.Zero);
        Assert.That(handoff.IsProcessing, Is.True);
        Assert.That(handoff.HasPendingEvents, Is.False);
    }
}
}