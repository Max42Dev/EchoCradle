using System;
using System.Collections.Concurrent;
using System.Threading;

namespace EchoCradle.Interview
{
    /// <summary>Ordered worker-to-main-thread speech events and a capture processing watermark.</summary>
    public sealed class CaptureHandoff
    {
        // Null is an onset; a PCM buffer is its later completion. One producer, one consumer.
        private readonly ConcurrentQueue<byte[]> _events = new ConcurrentQueue<byte[]>();
        private long _queuedSamples, _processedSamples;
        private int _pendingUtterances;

        public bool HasPendingEvents => !_events.IsEmpty;
        public bool IsProcessing => Interlocked.Read(ref _processedSamples) < Interlocked.Read(ref _queuedSamples);

        public void QueueSamples(int count) => Interlocked.Add(ref _queuedSamples, count);
        public void ProcessedSamples(int count) => Interlocked.Add(ref _processedSamples, count);
        public void PublishOnset() => _events.Enqueue(null);

        public void PublishUtterance(byte[] pcm)
        {
            if (pcm == null) throw new ArgumentNullException(nameof(pcm));
            if (Interlocked.Increment(ref _pendingUtterances) > 2)
            {
                Interlocked.Decrement(ref _pendingUtterances);
                throw new InvalidOperationException("UTTERANCE_OVERRUN");
            }
            _events.Enqueue(pcm);
        }

        public bool TryTake(out byte[] pcm)
        {
            if (!_events.TryDequeue(out pcm)) return false;
            if (pcm != null) Interlocked.Decrement(ref _pendingUtterances);
            return true;
        }
    }
}