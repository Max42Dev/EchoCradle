using System;

namespace EchoCradle.Interview
{
    /// <summary>Allocation-free fixed-duration native capture to 16 kHz conversion.</summary>
    public static class MicrophoneResampler
    {
        public static void ConvertBlock(float[] native, float[] output)
        {
            if (native == null || output == null || output.Length == 0 || native.Length < output.Length)
                throw new ArgumentException("Expected a native-rate block at least as long as the output.");
            double ratio = native.Length / (double)output.Length;
            for (int i = 0; i < output.Length; i++)
            {
                double position = i * ratio;
                int first = (int)position;
                int second = Math.Min(first + 1, native.Length - 1);
                output[i] = native[first] + (native[second] - native[first]) * (float)(position - first);
            }
        }
    }
}