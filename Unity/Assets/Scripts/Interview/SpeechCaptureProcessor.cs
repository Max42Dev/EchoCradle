using System;

// Unity's C# 9 compiler does not support file-scoped namespaces.
namespace EchoCradle.Interview
{

/// <summary>
/// Fixed-block 16 kHz mono endpointing shared by microphone capture and PCM replay.
/// One worker owns ProcessBlock and all buffers; readers only observe volatile state.
/// </summary>
public sealed class SpeechCaptureProcessor
{
    public const int SampleRate = 16000;
    private readonly int _blockSamples;
    private readonly float[] _preRoll, _utterance;
    private readonly float _onset, _silence;
    private readonly Func<float[], bool> _vad;
    private readonly CaptureHandoff _handoff;
    private int _preIndex, _preCount, _count;
    private float _voiced, _quiet;
    private volatile bool _active, _confirmingOnset;

    public bool IsSpeaking => _active;
    // Read the processing watermark BEFORE state: a dequeued block can still publish onset.
    public bool HasPendingAudio => _handoff.IsProcessing || _active || _confirmingOnset || _handoff.HasPendingEvents;

    public SpeechCaptureProcessor(int blockSamples, int preRollSamples, int maxUtteranceSamples,
        float onsetSeconds, float silenceSeconds, Func<float[], bool> vad, CaptureHandoff handoff)
    {
        if (blockSamples <= 0 || preRollSamples < blockSamples || maxUtteranceSamples < preRollSamples)
            throw new ArgumentOutOfRangeException(nameof(blockSamples), "Invalid capture buffer bounds.");
        if (float.IsNaN(onsetSeconds) || float.IsInfinity(onsetSeconds) || onsetSeconds <= 0)
            throw new ArgumentOutOfRangeException(nameof(onsetSeconds));
        if (float.IsNaN(silenceSeconds) || float.IsInfinity(silenceSeconds) || silenceSeconds <= 0)
            throw new ArgumentOutOfRangeException(nameof(silenceSeconds));
        _vad = vad ?? throw new ArgumentNullException(nameof(vad));
        _handoff = handoff ?? throw new ArgumentNullException(nameof(handoff));
        _blockSamples = blockSamples;
        _preRoll = new float[preRollSamples];
        _utterance = new float[maxUtteranceSamples];
        _onset = onsetSeconds;
        _silence = silenceSeconds;
    }

    /// <summary>
    /// Caller queues the sample watermark before handing the block to the worker.
    /// VAD must not mutate/retain the borrowed input. Input is copied before return;
    /// published PCM is independently owned and can outlive input-buffer reuse.
    /// Only successful processing advances the watermark, after state/events are published.
    /// </summary>
    public void ProcessBlock(float[] block)
    {
        if (block == null) throw new ArgumentNullException(nameof(block));
        if (block.Length != _blockSamples) throw new ArgumentException("Expected a fixed capture block.", nameof(block));
        ProcessEndpoint(block);
        _handoff.ProcessedSamples(block.Length);
    }

    private void ProcessEndpoint(float[] block)
    {
        bool speech = _vad(block);
        float seconds = _blockSamples / (float)SampleRate;
        if (!_active)
        {
            for (int i = 0; i < _blockSamples; i++)
            {
                _preRoll[_preIndex] = block[i];
                _preIndex = (_preIndex + 1) % _preRoll.Length;
                _preCount = Math.Min(_preCount + 1, _preRoll.Length);
            }
            _voiced = speech ? _voiced + seconds : 0;
            _confirmingOnset = _voiced > 0;
            if (_voiced < _onset) return;
            _active = true;
            _count = 0;
            int first = (_preIndex - _preCount + _preRoll.Length) % _preRoll.Length;
            for (int i = 0; i < _preCount; i++) _utterance[_count++] = _preRoll[(first + i) % _preRoll.Length];
            _quiet = 0;
            _handoff.PublishOnset();
            _confirmingOnset = false;
            return;
        }
        int copy = Math.Min(_blockSamples, _utterance.Length - _count);
        Array.Copy(block, 0, _utterance, _count, copy);
        _count += copy;
        _quiet = speech ? 0 : _quiet + seconds;
        if (_quiet < _silence && _count < _utterance.Length) return;
        var pcm = new byte[_count * 2];
        for (int i = 0; i < _count; i++)
        {
            short sample = (short)(Math.Max(-1, Math.Min(1, _utterance[i])) * 32767);
            pcm[i * 2] = (byte)sample;
            pcm[i * 2 + 1] = (byte)(sample >> 8);
        }
        _voiced = _quiet = 0;
        _count = _preCount = _preIndex = 0;
        _handoff.PublishUtterance(pcm);
        _active = false;
    }
}
}