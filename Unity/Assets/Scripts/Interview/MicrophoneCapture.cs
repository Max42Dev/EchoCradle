using System;
using System.Collections.Concurrent;
using System.Threading.Tasks;
using Newtonsoft.Json.Linq;
using UnityEngine;

namespace EchoCradle.Interview
{
    /// <summary>Continuous mono capture with bounded pre-roll and RMS speech endpointing.</summary>
    public sealed class MicrophoneCapture : IDisposable
    {
        private readonly string _device;
        private readonly AudioClip _clip;
        private readonly float[] _block, _preRoll, _utterance;
        private readonly float _onset, _silence;
        private readonly BlockingCollection<float[]> _blocks = new BlockingCollection<float[]>(128);
        private readonly ConcurrentQueue<float[]> _free = new ConcurrentQueue<float[]>();
        private readonly ConcurrentQueue<byte[]> _completed = new ConcurrentQueue<byte[]>();
        private readonly SileroDetector _vad;
        private Task _worker;
        private volatile bool _started, _failed;
        private int _read, _preIndex, _preCount, _count;
        private float _voiced, _quiet;
        private volatile bool _active;
        private bool _disposed;
        public event Action SpeechStarted;
        public event Action<byte[]> Utterance;
        public bool IsSpeaking => _active;
        public bool HasPendingAudio => _active || _started || !_completed.IsEmpty || _blocks.Count > 0;

        public MicrophoneCapture(JObject settings, SileroDetector vad)
        {
            _vad = vad;
            string requested = (string)settings["device"];
            string[] devices = Microphone.devices;
            if (devices.Length == 0) throw new InvalidOperationException("MICROPHONE_UNAVAILABLE");
            _device = string.IsNullOrEmpty(requested) ? devices[0] : requested;
            if (Array.IndexOf(devices, _device) < 0) throw new InvalidOperationException("MICROPHONE_UNAVAILABLE");
            _block = new float[(int)settings["blockSamples"]];
            _preRoll = new float[Math.Max(_block.Length, (int)((float)settings["preRollSeconds"] * 16000))];
            _utterance = new float[(int)((float)settings["maxUtteranceSeconds"] * 16000)];
            _onset = (float)settings["onsetSeconds"];
            _silence = (float)settings["silenceSeconds"];
            _clip = Microphone.Start(_device, true, 4, 16000);
            if (_clip == null || _clip.channels != 1 || _clip.frequency != 16000)
            {
                Dispose();
                throw new InvalidOperationException("MICROPHONE_UNAVAILABLE");
            }
            for (int i = 0; i < 130; i++) _free.Enqueue(new float[_block.Length]);
            _worker = Task.Run(() =>
            {
                try
                {
                    foreach (float[] block in _blocks.GetConsumingEnumerable())
                    {
                        ProcessBlock(block);
                        _free.Enqueue(block);
                    }
                }
                catch (Exception) { _failed = true; }
                finally { _vad.Dispose(); }
            });
        }

        // Called once per frame; fixed buffers, no routine allocations.
        public void Poll()
        {
            if (_disposed) return;
            if (_failed) throw new InvalidOperationException("VAD_CAPTURE_FAILED");
            if (_started) { _started = false; SpeechStarted?.Invoke(); }
            while (_completed.TryDequeue(out byte[] pcm)) Utterance?.Invoke(pcm);
            if (!Microphone.IsRecording(_device)) throw new InvalidOperationException("MICROPHONE_UNAVAILABLE");
            int position = Microphone.GetPosition(_device);
            if (position < 0) throw new InvalidOperationException("MICROPHONE_UNAVAILABLE");
            int available = (position - _read + _clip.samples) % _clip.samples;
            while (available >= _block.Length)
            {
                if (!_clip.GetData(_block, _read)) throw new InvalidOperationException("MICROPHONE_UNAVAILABLE");
                _read = (_read + _block.Length) % _clip.samples;
                available -= _block.Length;
                if (!_free.TryDequeue(out float[] block)) throw new InvalidOperationException("CAPTURE_OVERRUN");
                Array.Copy(_block, block, _block.Length);
                if (!_blocks.TryAdd(block)) throw new InvalidOperationException("CAPTURE_OVERRUN");
            }
        }

        private void ProcessBlock(float[] block)
        {
            bool speech = _vad.Process(block);
            float seconds = _block.Length / 16000f;
            if (!_active)
            {
                for (int i = 0; i < _block.Length; i++)
                {
                    _preRoll[_preIndex] = block[i];
                    _preIndex = (_preIndex + 1) % _preRoll.Length;
                    _preCount = Math.Min(_preCount + 1, _preRoll.Length);
                }
                _voiced = speech ? _voiced + seconds : 0;
                if (_voiced < _onset) return;
                _active = true;
                _count = 0;
                int first = (_preIndex - _preCount + _preRoll.Length) % _preRoll.Length;
                for (int i = 0; i < _preCount; i++) _utterance[_count++] = _preRoll[(first + i) % _preRoll.Length];
                _quiet = 0;
                _started = true;
                return;
            }
            int copy = Math.Min(_block.Length, _utterance.Length - _count);
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
            if (_completed.Count >= 2) throw new InvalidOperationException("UTTERANCE_OVERRUN");
            _completed.Enqueue(pcm);
            _active = false;
        }

        public void Dispose()
        {
            if (_disposed) return;
            _disposed = true;
            Microphone.End(_device);
            if (_clip != null) UnityEngine.Object.Destroy(_clip);
            if (_worker != null)
            {
                _blocks.CompleteAdding();
                _ = _worker.ContinueWith(_ => _blocks.Dispose(), TaskScheduler.Default);
            }
            else { _vad.Dispose(); _blocks.Dispose(); }
        }
    }
}