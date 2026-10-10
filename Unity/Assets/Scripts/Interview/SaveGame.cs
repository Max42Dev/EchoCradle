using System;
using System.IO;
using System.Security.Cryptography;
using System.Text;
using System.Threading;
using System.Threading.Tasks;
using Newtonsoft.Json;
using Newtonsoft.Json.Linq;

namespace EchoCradle.Interview
{
    public sealed class SaveGame
    {
        private readonly string _configPath, _sessionPath;
        private readonly JObject _session;
        public string Folder { get; }

        private SaveGame(string root, JObject settings)
        {
            string id = Guid.NewGuid().ToString("N");
            Folder = Path.Combine(root, SafeName((string)settings["folder"]), id);
            _configPath = Path.Combine(Folder, SafeName((string)settings["configFile"]));
            _sessionPath = Path.Combine(Folder, SafeName((string)settings["sessionFile"]));
            if (_configPath == _sessionPath) throw new ArgumentException("Save filenames must differ.");
            byte[] random = new byte[4];
            using (RandomNumberGenerator rng = RandomNumberGenerator.Create()) rng.GetBytes(random);
            _session = new JObject { ["id"] = id, ["seed"] = BitConverter.ToUInt32(random, 0),
                ["createdUtc"] = DateTime.UtcNow.ToString("O"), ["status"] = "interview" };
        }

        private static string SafeName(string value)
        {
            if (string.IsNullOrWhiteSpace(value) || value == "." || value == ".." ||
                value.IndexOfAny(Path.GetInvalidFileNameChars()) >= 0 || value.IndexOfAny(new[] { '/', '\\' }) >= 0)
                throw new ArgumentException("Invalid save filename.");
            return value;
        }

        public static Task<SaveGame> CreateAsync(string root, JObject settings, CancellationToken token)
        {
            var save = new SaveGame(root, settings);
            return Task.Run(() =>
            {
                token.ThrowIfCancellationRequested();
                Directory.CreateDirectory(save.Folder);
                AtomicWrite(save._sessionPath, save._session);
                return save;
            }, token);
        }

        public Task CompleteAsync(JObject values, InterviewState state, CancellationToken token)
        {
            JObject snapshot = (JObject)values.DeepClone();
            return Task.Run(() =>
            {
                token.ThrowIfCancellationRequested();
                state.ValidateComplete(snapshot);
                AtomicWrite(_configPath, snapshot);
                // A ready manifest is the commit marker. Partial/cancelled sessions are never loadable.
                _session["status"] = "ready";
                _session["completedUtc"] = DateTime.UtcNow.ToString("O");
                AtomicWrite(_sessionPath, _session);
            }, token);
        }

        private static void AtomicWrite(string path, JObject data)
        {
            string temporary = path + ".tmp";
            try
            {
                using (var stream = new FileStream(temporary, FileMode.CreateNew, FileAccess.Write, FileShare.None))
                using (var writer = new StreamWriter(stream, new UTF8Encoding(false), 1024, true))
                {
                    writer.Write(data.ToString(Formatting.Indented));
                    writer.Flush();
                    stream.Flush(true);
                }
                if (File.Exists(path)) File.Replace(temporary, path, null);
                else File.Move(temporary, path);
            }
            finally { if (File.Exists(temporary)) File.Delete(temporary); }
        }
    }
}