using System;

namespace EchoCradle.Interview
{
    /// <summary>Startup-only median native-PCM RMS measurement; never modifies captured audio.</summary>
    public sealed class NoiseFloorCalibration
    {
        private readonly double[] _levels;
        private readonly double _multiple;
        private int _count;
        public bool IsComplete => _count == _levels.Length;
        public double Threshold { get; private set; }

        public NoiseFloorCalibration(int blockSamples, double seconds, double multiple)
        {
            if (blockSamples <= 0 || double.IsNaN(seconds) || double.IsInfinity(seconds) ||
                seconds <= 0 || seconds > 5 || double.IsNaN(multiple) ||
                double.IsInfinity(multiple) || multiple <= 0 || multiple > 100)
                throw new ArgumentOutOfRangeException(nameof(seconds));
            _levels = new double[(int)Math.Ceiling(seconds * 16000 / blockSamples)];
            _multiple = multiple;
        }

        public void Add(float[] block)
        {
            if (block == null || block.Length == 0) throw new ArgumentException("Empty calibration block.");
            if (IsComplete) throw new InvalidOperationException("Calibration already complete.");
            double sum = 0;
            foreach (float sample in block)
            {
                if (float.IsNaN(sample) || float.IsInfinity(sample))
                    throw new ArgumentException("Nonfinite calibration audio.");
                sum += (double)sample * sample;
            }
            _levels[_count++] = Math.Sqrt(sum / block.Length);
            if (!IsComplete) return;
            Array.Sort(_levels);
            int middle = _levels.Length / 2;
            double median = _levels.Length % 2 == 0 ?
                (_levels[middle - 1] + _levels[middle]) / 2 : _levels[middle];
            Threshold = median * _multiple;
        }

        public static double InputGain(double rms, double runningRms, double peak,
            double threshold, double target, double peakLimit, double maximumGain)
        {
            if (peak <= 0) return 1;
            if (rms <= threshold) return Math.Min(1, peakLimit / peak);
            return Math.Min(maximumGain, Math.Min(target / Math.Max(rms, runningRms), peakLimit / peak));
        }
    }
}