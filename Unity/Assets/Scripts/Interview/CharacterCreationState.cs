using System;
using System.Collections.Generic;
using System.Security.Cryptography;
using Newtonsoft.Json.Linq;

namespace EchoCradle.Interview
{
    /// <summary>Plain C# review gates and generation provenance; never stores Unity objects.</summary>
    public sealed class CharacterCreationState
    {
        public enum Phase { Configuration, Description, GeneratingImage, ImageReview, GeneratingModel,
            StaticReview, Animating, AnimationReview, Ready }

        private readonly HashSet<uint> _seeds = new HashSet<uint>();
        private readonly JArray _requests = new JArray();
        private readonly List<string> _clips = new List<string>();
        private JObject _pending;
        private string _imageHash, _modelHash;
        private bool _staticPresented;
        public Phase Stage { get; private set; } = Phase.Configuration;
        public int DescriptionSession { get; private set; }
        public int ClipIndex { get; private set; }
        public string CurrentClip => Stage == Phase.AnimationReview ? _clips[ClipIndex] : null;
        public int ClipCount => _clips.Count;
        public bool IsReady => Stage == Phase.Ready;
        public string Description { get; private set; }

        public void ConfigurationRecorded()
        {
            Require(Phase.Configuration);
            NewDescription();
        }

        private void NewDescription()
        {
            Stage = Phase.Description;
            Description = null;
            _imageHash = _modelHash = null;
            _staticPresented = false;
            ++DescriptionSession;
        }

        public uint RequestImage(string description, string prompt = null, string negativePrompt = "")
        {
            Require(Phase.Description);
            if (string.IsNullOrWhiteSpace(description) || description.Length > 2000)
                throw new ArgumentException("A bounded player description is required.");
            Description = description;
            uint seed = Request("image", Phase.GeneratingImage, null, prompt ?? description);
            _pending["negativePrompt"] = negativePrompt;
            return seed;
        }

        public void ImageReceived(byte[] bytes, string artifactId)
        {
            Require(Phase.GeneratingImage);
            _imageHash = Receive(bytes, artifactId);
            Stage = Phase.ImageReview;
        }

        public void RejectImage()
        {
            Require(Phase.ImageReview);
            _pending["approved"] = false;
            NewDescription();
        }

        public uint ApproveImage()
        {
            Require(Phase.ImageReview);
            _pending["approved"] = true;
            return Request("image_to_3d", Phase.GeneratingModel, _imageHash, null);
        }

        public void ModelReceived(byte[] bytes, string artifactId)
        {
            Require(Phase.GeneratingModel);
            _modelHash = Receive(bytes, artifactId);
            Stage = Phase.StaticReview;
        }

        // Controller invokes only after a rendered frame, not merely after import completion.
        public void StaticPresented() { Require(Phase.StaticReview); _staticPresented = true; }

        public uint StartAnimation()
        {
            Require(Phase.StaticReview);
            if (!_staticPresented) throw new InvalidOperationException("Show the static model before animating.");
            return Request("animate_3d", Phase.Animating, _modelHash, null);
        }

        public void AnimationReceived(byte[] bytes, string artifactId, IReadOnlyList<string> clips)
        {
            Require(Phase.Animating);
            if (clips == null || clips.Count == 0) throw new InvalidOperationException("Animation returned no clips.");
            var unique = new HashSet<string>(StringComparer.Ordinal);
            foreach (string clip in clips)
                if (string.IsNullOrWhiteSpace(clip) || !unique.Add(clip))
                    throw new InvalidOperationException("Invalid animation clip list.");
            Receive(bytes, artifactId);
            _clips.AddRange(clips);
            ClipIndex = 0;
            Stage = Phase.AnimationReview;
        }

        public void NextClip()
        {
            Require(Phase.AnimationReview);
            ++ClipIndex;
            if (ClipIndex == _clips.Count) Stage = Phase.Ready;
        }

        private uint Request(string kind, Phase phase, string inputHash, string prompt)
        {
            uint seed;
            byte[] random = new byte[4];
            using (RandomNumberGenerator rng = RandomNumberGenerator.Create())
                do { rng.GetBytes(random); seed = BitConverter.ToUInt32(random, 0); } while (!_seeds.Add(seed));
            _pending = new JObject { ["kind"] = kind, ["seed"] = seed,
                ["descriptionSession"] = DescriptionSession, ["inputSha256"] = inputHash,
                ["prompt"] = prompt, ["requestedUtc"] = DateTime.UtcNow.ToString("O") };
            _requests.Add(_pending);
            Stage = phase;
            return seed;
        }

        private string Receive(byte[] bytes, string artifactId)
        {
            if (bytes == null || bytes.Length == 0 || string.IsNullOrWhiteSpace(artifactId))
                throw new ArgumentException("Empty generation result.");
            string hash = Hash(bytes);
            _pending["sha256"] = hash;
            _pending["artifactId"] = artifactId;
            _pending["byteLength"] = bytes.Length;
            _pending["file"] = hash + (Stage == Phase.GeneratingImage ? ".png" : ".glb");
            return hash;
        }

        public static string Hash(byte[] bytes)
        {
            using (SHA256 sha = SHA256.Create())
                return BitConverter.ToString(sha.ComputeHash(bytes)).Replace("-", "").ToLowerInvariant();
        }

        public JObject Snapshot() => new JObject { ["stage"] = Stage.ToString(),
            ["descriptionSession"] = DescriptionSession, ["description"] = Description,
            ["requests"] = _requests.DeepClone(), ["clips"] = new JArray(_clips),
            ["reviewedClips"] = ClipIndex, ["ready"] = IsReady };

        private void Require(Phase phase)
        {
            if (Stage != phase) throw new InvalidOperationException("Invalid character creation transition: " + Stage);
        }
    }
}