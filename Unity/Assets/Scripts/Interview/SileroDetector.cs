using System;
using System.IO;
using System.Runtime.InteropServices;
using Newtonsoft.Json.Linq;

namespace EchoCradle.Interview
{
    /// <summary>Sherpa 1.13.8 C ABI. Inference is called only on the capture worker.</summary>
    public sealed class SileroDetector : IDisposable
    {
        [StructLayout(LayoutKind.Sequential)]
        private struct Model
        {
            public IntPtr Path;
            public float Threshold, Silence, Speech;
            public int Window;
            public float Maximum;
        }
        [StructLayout(LayoutKind.Sequential)]
        private struct Config
        {
            public Model Silero;
            public int SampleRate, Threads;
            public IntPtr Provider;
            public int Debug;
            public Model Ten;
        }
        [UnmanagedFunctionPointer(CallingConvention.Cdecl)] private delegate IntPtr Create(ref Config config, float seconds);
        [UnmanagedFunctionPointer(CallingConvention.Cdecl)] private delegate void Release(IntPtr detector);
        [UnmanagedFunctionPointer(CallingConvention.Cdecl)] private delegate void Feed(IntPtr detector, float[] samples, int count);
        [UnmanagedFunctionPointer(CallingConvention.Cdecl)] private delegate int Query(IntPtr detector);
        [DllImport("kernel32", CharSet = CharSet.Unicode, SetLastError = true)] private static extern IntPtr LoadLibraryEx(string path, IntPtr file, uint flags);
        [DllImport("kernel32", CharSet = CharSet.Ansi)] private static extern IntPtr GetProcAddress(IntPtr module, string name);
        [DllImport("kernel32")] private static extern bool FreeLibrary(IntPtr module);
        private IntPtr _library, _detector;
        private Release _destroy, _pop;
        private Feed _feed;
        private Query _detected, _empty;
        private readonly float[] _window = new float[512];
        private int _count;
        private readonly double _target, _peakLimit, _maximumGain, _alpha;
        private double _runningRms;

        public SileroDetector(JObject settings)
        {
            string directory = Environment.ExpandEnvironmentVariables((string)settings["nativeDirectory"]);
            string model = Environment.ExpandEnvironmentVariables((string)settings["model"]);
            if (!Path.IsPathRooted(directory) || !Path.IsPathRooted(model) || !File.Exists(model))
                throw new InvalidOperationException("VAD_NOT_PROVISIONED");
            _target = (double)settings["targetRms"];
            _peakLimit = (double)settings["peakLimit"];
            _maximumGain = (double)settings["maxGain"];
            _alpha = Math.Exp(-512 / (16000 * (double)settings["gainTimeSeconds"]));
            IntPtr modelPath = Marshal.StringToCoTaskMemUTF8(model);
            IntPtr provider = Marshal.StringToCoTaskMemUTF8("cpu");
            try
            {
                // Absolute trusted local path; dependency lookup limited to DLL directory and System32.
                _library = LoadLibraryEx(Path.Combine(directory, "sherpa-onnx-c-api.dll"), IntPtr.Zero, 0x100 | 0x800);
                if (_library == IntPtr.Zero) throw new InvalidOperationException("VAD_RUNTIME_UNAVAILABLE");
                _destroy = Function<Release>("SherpaOnnxDestroyVoiceActivityDetector");
                _pop = Function<Release>("SherpaOnnxVoiceActivityDetectorPop");
                _feed = Function<Feed>("SherpaOnnxVoiceActivityDetectorAcceptWaveform");
                _detected = Function<Query>("SherpaOnnxVoiceActivityDetectorDetected");
                _empty = Function<Query>("SherpaOnnxVoiceActivityDetectorEmpty");
                var config = new Config
                {
                    Silero = new Model { Path = modelPath, Threshold = (float)settings["threshold"],
                        Silence = (float)settings["minSilenceSeconds"], Speech = (float)settings["minSpeechSeconds"],
                        Window = 512, Maximum = 20 },
                    SampleRate = 16000, Threads = 1, Provider = provider
                };
                _detector = Function<Create>("SherpaOnnxCreateVoiceActivityDetector")(ref config, 30);
                if (_detector == IntPtr.Zero) throw new InvalidOperationException("VAD_MODEL_INVALID");
            }
            catch { Dispose(); throw; }
            finally { Marshal.FreeCoTaskMem(modelPath); Marshal.FreeCoTaskMem(provider); }
        }

        private T Function<T>(string name) where T : Delegate
        {
            IntPtr address = GetProcAddress(_library, name);
            if (address == IntPtr.Zero) throw new InvalidOperationException("VAD_ABI_MISMATCH");
            return Marshal.GetDelegateForFunctionPointer<T>(address);
        }

        public bool Process(float[] block)
        {
            bool detected = false;
            foreach (float value in block)
            {
                _window[_count++] = value;
                if (_count < 512) continue;
                double sum = 0, peak = 0;
                foreach (float sample in _window) { sum += sample * sample; peak = Math.Max(peak, Math.Abs(sample)); }
                double rms = Math.Sqrt(sum / 512);
                _runningRms = _runningRms == 0 ? rms : _alpha * _runningRms + (1 - _alpha) * rms;
                double gain = peak == 0 ? 1 : Math.Min(_maximumGain,
                    Math.Min(_target / Math.Max(rms, _runningRms), _peakLimit / peak));
                for (int i = 0; i < 512; i++) _window[i] *= (float)gain;
                _feed(_detector, _window, 512);
                detected |= _detected(_detector) != 0;
                while (_empty(_detector) == 0) _pop(_detector);
                _count = 0;
            }
            return detected || _detected(_detector) != 0;
        }

        public void Dispose()
        {
            if (_detector != IntPtr.Zero) { _destroy(_detector); _detector = IntPtr.Zero; }
            if (_library != IntPtr.Zero) { FreeLibrary(_library); _library = IntPtr.Zero; }
        }
    }
}