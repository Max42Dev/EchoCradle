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

        /// <summary>Configuration is a draft until the character review transaction finishes.</summary>
        public Task SaveDraftAsync(JObject values, InterviewState state, CancellationToken token)
        {
            JObject snapshot = (JObject)values.DeepClone();
            state.ValidateComplete(snapshot);
            return Task.Run(() =>
            {
                token.ThrowIfCancellationRequested();
                AtomicWrite(_configPath, snapshot);
                _session["status"] = "character_creation";
                AtomicWrite(_sessionPath, _session);
            }, token);
        }

        public Task SaveAppearanceAsync(CharacterCreationState flow, byte[] bytes, string extension,
            CancellationToken token)
        {
            JObject snapshot = flow.Snapshot();
            byte[] copy = bytes == null ? null : (byte[])bytes.Clone();
            if (extension != ".png" && extension != ".glb") throw new ArgumentException("Invalid appearance format.");
            return Task.Run(() =>
            {
                token.ThrowIfCancellationRequested();
                string directory = Path.Combine(Folder, "appearance");
                Directory.CreateDirectory(directory);
                if (copy != null)
                {
                    string file = Path.Combine(directory, CharacterCreationState.Hash(copy) + extension);
                    if (!File.Exists(file))
                    {
                        string temporary = file + ".tmp";
                        try
                        {
                            using (var stream = new FileStream(temporary, FileMode.CreateNew, FileAccess.Write, FileShare.None))
                            { stream.Write(copy, 0, copy.Length); stream.Flush(true); }
                            token.ThrowIfCancellationRequested();
                            File.Move(temporary, file);
                        }
                        finally { if (File.Exists(temporary)) File.Delete(temporary); }
                    }
                }
                token.ThrowIfCancellationRequested();
                AtomicWrite(Path.Combine(directory, "appearance.json"), snapshot);
            }, token);
        }

        public async Task CompleteCharacterAsync(CharacterCreationState flow, CancellationToken token)
        {
            if (!flow.IsReady) throw new InvalidOperationException("Review every animation before completing the save.");
            await SaveAppearanceAsync(flow, null, ".glb", token);
            await Task.Run(() =>
            {
                token.ThrowIfCancellationRequested();
                if (!File.Exists(_configPath)) throw new InvalidOperationException("Missing draft configuration.");
                JObject appearance = JObject.Parse(File.ReadAllText(Path.Combine(Folder, "appearance", "appearance.json")));
                foreach (JObject request in (JArray)appearance["requests"])
                {
                    if (request["sha256"] == null) throw new InvalidOperationException("Unfinished generation.");
                    string file = Path.Combine(Folder, "appearance", SafeName((string)request["file"]));
                    if (!File.Exists(file) || CharacterCreationState.Hash(File.ReadAllBytes(file)) != (string)request["sha256"])
                        throw new InvalidOperationException("Missing or corrupt appearance artifact.");
                }
                token.ThrowIfCancellationRequested();
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