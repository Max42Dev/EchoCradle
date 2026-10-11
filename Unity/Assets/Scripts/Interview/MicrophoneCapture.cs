using System;
using System.Collections.Concurrent;
using System.Threading.Tasks;
using Newtonsoft.Json.Linq;
using UnityEngine;

namespace EchoCradle.Interview
{
    /// <summary>Continuous mono capture with bounded pre-roll and Silero speech endpointing.</summary>
    public sealed class MicrophoneCapture : IDisposable
    {
        private readonly string _device;
        private readonly AudioClip _clip;
        private readonly float[] _block, _nativeBlock;
        private readonly BlockingCollection<float[]> _blocks = new BlockingCollection<float[]>(128);
        private readonly ConcurrentQueue<float[]> _free = new ConcurrentQueue<float[]>();
        private readonly CaptureHandoff _handoff = new CaptureHandoff();
        private readonly SileroDetector _vad;
        private readonly SpeechCaptureProcessor _processor;
        private Task _worker;
        private volatile bool _failed;
        private volatile bool _calibrated;
        public bool IsCalibrated => _calibrated;
        private int _read;
        private bool _disposed;
        public event Action SpeechStarted;
        public event Action<byte[]> Utterance;
        public bool IsSpeaking => _processor.IsSpeaking;
        public bool HasPendingAudio => _processor.HasPendingAudio || (!_disposed &&
            (Microphone.GetPosition(_device) - _read + _clip.samples) % _clip.samples >= _nativeBlock.Length);

        public MicrophoneCapture(JObject settings, SileroDetector vad)
        {
            _vad = vad;
            string requested = (string)settings["device"];
            string[] devices = Microphone.devices;
            if (devices.Length == 0) throw new InvalidOperationException("MICROPHONE_UNAVAILABLE");
            _device = string.IsNullOrEmpty(requested) ? devices[0] : requested;
            if (Array.IndexOf(devices, _device) < 0) throw new InvalidOperationException("MICROPHONE_UNAVAILABLE");
            _block = new float[(int)settings["blockSamples"]];
            var calibration = new NoiseFloorCalibration(_block.Length,
                (double)settings["calibrationSeconds"], (double)settings["noiseFloorMultiple"]);
            int captureRate = 16000;
            string rateOverride = Environment.GetEnvironmentVariable("ECHOCRADLE_MICROPHONE_SAMPLE_RATE");
            if (!string.IsNullOrEmpty(rateOverride) && (!int.TryParse(rateOverride, out captureRate) ||
                captureRate < 16000 || captureRate > 192000 || (long)captureRate * _block.Length % 16000 != 0))
                throw new InvalidOperationException("INVALID_CAPTURE_RATE");
            _nativeBlock = captureRate == 16000 ? _block : new float[captureRate * _block.Length / 16000];
            _processor = new SpeechCaptureProcessor(_block.Length,
                Math.Max(_block.Length, (int)((float)settings["preRollSeconds"] * SpeechCaptureProcessor.SampleRate)),
                (int)((float)settings["maxUtteranceSeconds"] * SpeechCaptureProcessor.SampleRate),
                (float)settings["onsetSeconds"], (float)settings["silenceSeconds"], _vad.Process, _handoff);
            _clip = Microphone.Start(_device, true, 4, captureRate);
            if (_clip == null || _clip.channels != 1 || _clip.frequency != captureRate)
            {
                Dispose();
                throw new InvalidOperationException("MICROPHONE_UNAVAILABLE");
            }
            Debug.Log("Microphone input: " + _device + "; " + _clip.frequency + " Hz; " + _clip.channels + " channel(s).");
            for (int i = 0; i < 130; i++) _free.Enqueue(new float[_block.Length]);
            _worker = Task.Run(() =>
            {
                try
                {
                    foreach (float[] block in _blocks.GetConsumingEnumerable())
                    {
                        if (!calibration.IsComplete)
                        {
                            // Startup quiet audio is neither speech nor pre-roll. No onset events can escape.
                            calibration.Add(block);
                            if (calibration.IsComplete)
                            {
                                _vad.SetNoiseFloor(calibration.Threshold);
                                _calibrated = true;
                            }
                            _handoff.ProcessedSamples(block.Length);
                        }
                        else _processor.ProcessBlock(block);
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
            while (_handoff.TryTake(out byte[] pcm))
            {
                if (pcm == null) SpeechStarted?.Invoke();
                else Utterance?.Invoke(pcm);
                // Event handlers can cancel and dispose this capture.
                if (_disposed) return;
            }
            if (!Microphone.IsRecording(_device)) throw new InvalidOperationException("MICROPHONE_UNAVAILABLE");
            int position = Microphone.GetPosition(_device);
            if (position < 0) throw new InvalidOperationException("MICROPHONE_UNAVAILABLE");
            int available = (position - _read + _clip.samples) % _clip.samples;
            while (available >= _nativeBlock.Length)
            {
                if (!_clip.GetData(_nativeBlock, _read)) throw new InvalidOperationException("MICROPHONE_UNAVAILABLE");
                _read = (_read + _nativeBlock.Length) % _clip.samples;
                available -= _nativeBlock.Length;
                if (!ReferenceEquals(_nativeBlock, _block)) MicrophoneResampler.ConvertBlock(_nativeBlock, _block);
                if (!_free.TryDequeue(out float[] block)) throw new InvalidOperationException("CAPTURE_OVERRUN");
                Array.Copy(_block, block, _block.Length);
                _handoff.QueueSamples(block.Length);
                if (!_blocks.TryAdd(block)) throw new InvalidOperationException("CAPTURE_OVERRUN");
            }
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