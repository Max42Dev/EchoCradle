using System;
using System.IO;
using System.Threading.Tasks;
using Newtonsoft.Json.Linq;

namespace EchoCradle.Interview
{
    /// <summary>Explicitly opt-in local PCM evidence. No recording is written by normal launches.</summary>
    public static class SpeechDiagnostics
    {
        public static Task WriteAsync(string directory, string name, byte[] pcm, int sampleRate,
            string transcript = null) => Task.Run(() =>
        {
            Directory.CreateDirectory(directory);
            string path = Path.Combine(directory, name);
            File.WriteAllBytes(path + ".wav", Wave(pcm, sampleRate));
            double sum = 0, peak = 0;
            int nonzero = 0;
            for (int i = 0; i < pcm.Length; i += 2)
            {
                double value = (short)(pcm[i] | pcm[i + 1] << 8) / 32768.0;
                sum += value * value;
                peak = Math.Max(peak, Math.Abs(value));
                if (value != 0) nonzero++;
            }
            var report = new JObject
            {
                ["sampleRate"] = sampleRate, ["samples"] = pcm.Length / 2,
                ["durationSeconds"] = pcm.Length / (2.0 * sampleRate),
                ["rms"] = Math.Sqrt(sum / (pcm.Length / 2.0)), ["peak"] = peak,
                ["nonzeroFraction"] = nonzero / (pcm.Length / 2.0),
                ["transcript"] = transcript
            };
            File.WriteAllText(path + ".json", report.ToString());
        });

        public static byte[] Wave(byte[] pcm, int sampleRate)
        {
            if (pcm == null || pcm.Length == 0 || pcm.Length % 2 != 0)
                throw new ArgumentException("Expected nonempty mono PCM16.", nameof(pcm));
            if (sampleRate <= 0 || sampleRate > 192000)
                throw new ArgumentOutOfRangeException(nameof(sampleRate));
            using (var stream = new MemoryStream(pcm.Length + 44))
            using (var writer = new BinaryWriter(stream))
            {
                writer.Write(new[] { (byte)'R', (byte)'I', (byte)'F', (byte)'F' });
                writer.Write(pcm.Length + 36);
                writer.Write(new[] { (byte)'W', (byte)'A', (byte)'V', (byte)'E' });
                writer.Write(new[] { (byte)'f', (byte)'m', (byte)'t', (byte)' ' });
                writer.Write(16);
                writer.Write((short)1);
                writer.Write((short)1);
                writer.Write(sampleRate);
                writer.Write(sampleRate * 2);
                writer.Write((short)2);
                writer.Write((short)16);
                writer.Write(new[] { (byte)'d', (byte)'a', (byte)'t', (byte)'a' });
                writer.Write(pcm.Length);
                writer.Write(pcm);
                return stream.ToArray();
            }
        }
    }
}