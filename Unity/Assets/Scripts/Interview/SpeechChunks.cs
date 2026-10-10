using System;
using System.Collections.Concurrent;
using System.Text;

namespace EchoCradle.Interview
{
    /// <summary>Worker-thread hand-off only; bounded text, no synthesis on the socket consumer.</summary>
    public sealed class SpeechChunks
    {
        private readonly StringBuilder _pending = new StringBuilder();
        private readonly ConcurrentQueue<string> _queue = new ConcurrentQueue<string>();
        private readonly int _chunkLimit, _totalLimit;
        private int _total;
        public SpeechChunks(int chunkLimit, int totalLimit) { _chunkLimit = chunkLimit; _totalLimit = totalLimit; }

        public void Add(string delta)
        {
            _total += delta.Length;
            if (_total > _totalLimit) throw new InvalidOperationException("Speech exceeds configured limit.");
            foreach (char character in delta)
            {
                _pending.Append(character);
                if (character == '.' || character == '?' || character == '!' || character == '\n') Flush();
                else if (_pending.Length >= _chunkLimit)
                {
                    // Split at a word boundary where possible, never exceed the service's 300-char cap.
                    int split = _pending.Length;
                    for (int i = _pending.Length - 1; i > 0; i--)
                        if (char.IsWhiteSpace(_pending[i])) { split = i; break; }
                    Emit(_pending.ToString(0, split));
                    _pending.Remove(0, split);
                }
            }
        }

        public void Flush()
        {
            Emit(_pending.ToString());
            _pending.Clear();
        }

        private void Emit(string text)
        {
            text = text.Trim();
            if (text.Length > 0) _queue.Enqueue(text);
        }

        public bool TryTake(out string text) => _queue.TryDequeue(out text);

        public static string EstimatedPrefix(string text, int samplesPlayed, int totalSamples)
        {
            if (totalSamples <= 0 || samplesPlayed <= 0) return string.Empty;
            string[] words = text.Split(new[] { ' ', '\n', '\r', '\t' }, StringSplitOptions.RemoveEmptyEntries);
            int count = Math.Min(words.Length, (int)(words.Length * (double)samplesPlayed / totalSamples));
            return string.Join(" ", words, 0, count);
        }
    }
}