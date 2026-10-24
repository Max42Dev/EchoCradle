using System;
using System.IO;
using Newtonsoft.Json;
using Newtonsoft.Json.Linq;

namespace EchoCradle.Orchestration
{
    /// <summary>Bounded container/resource checks, not a complete image decoder or glTF semantic validator.</summary>
    internal static class VisualContainer
    {
        internal const int Limit = 32 * 1024 * 1024;

        internal static void Validate(byte[] bytes, string media)
        {
            try
            {
                if (bytes == null || bytes.Length == 0 || bytes.Length > Limit) Invalid();
                if (media == "image/png") Png(bytes);
                else if (media == "model/gltf-binary") Glb(bytes);
                else Invalid();
            }
            catch (Exception) { throw new OrchestratorException("ARTIFACT_INVALID"); }
        }

        private static void Invalid() { throw new OrchestratorException("ARTIFACT_INVALID"); }

        private static uint U32(byte[] bytes, int at, bool big = false)
        {
            if (at < 0 || at > bytes.Length - 4) Invalid();
            return big ? (uint)bytes[at] << 24 | (uint)bytes[at + 1] << 16 | (uint)bytes[at + 2] << 8 | bytes[at + 3] :
                (uint)bytes[at + 3] << 24 | (uint)bytes[at + 2] << 16 | (uint)bytes[at + 1] << 8 | bytes[at];
        }

        private static void Png(byte[] bytes)
        {
            byte[] signature = { 137, 80, 78, 71, 13, 10, 26, 10 };
            if (bytes.Length < 45) Invalid();
            for (int i = 0; i < signature.Length; i++) if (bytes[i] != signature[i]) Invalid();
            int offset = 8, colour = -1, depth = 0;
            bool data = false, dataEnded = false, palette = false, ended = false;
            long compressed = 0;
            while (offset < bytes.Length)
            {
                if (bytes.Length - offset < 12) Invalid();
                uint rawLength = U32(bytes, offset, true);
                if (rawLength > bytes.Length - offset - 12) Invalid();
                int length = (int)rawLength, start = offset + 8, end = start + length;
                for (int i = offset + 4; i < offset + 8; i++)
                    if (!(bytes[i] >= 'A' && bytes[i] <= 'Z') && !(bytes[i] >= 'a' && bytes[i] <= 'z')) Invalid();
                if ((bytes[offset + 6] & 32) != 0) Invalid();
                uint crc = uint.MaxValue;
                for (int i = offset + 4; i < end; i++)
                {
                    crc ^= bytes[i];
                    for (int bit = 0; bit < 8; bit++) crc = (crc >> 1) ^ ((crc & 1) != 0 ? 0xedb88320u : 0u);
                }
                if ((crc ^ uint.MaxValue) != U32(bytes, end, true)) Invalid();
                uint kind = U32(bytes, offset + 4, true);
                if (offset == 8 && kind != 0x49484452) Invalid();
                if (kind == 0x49484452) // IHDR
                {
                    if (offset != 8 || length != 13) Invalid();
                    uint width = U32(bytes, start, true), height = U32(bytes, start + 4, true);
                    depth = bytes[start + 8]; colour = bytes[start + 9];
                    bool validDepth = colour == 0 ? depth == 1 || depth == 2 || depth == 4 || depth == 8 || depth == 16 :
                        colour == 3 ? depth == 1 || depth == 2 || depth == 4 || depth == 8 :
                        (colour == 2 || colour == 4 || colour == 6) && (depth == 8 || depth == 16);
                    if (width == 0 || height == 0 || width > 8192 || height > 8192 ||
                        (long)width * height > 16777216 || !validDepth || bytes[start + 10] != 0 ||
                        bytes[start + 11] != 0 || bytes[start + 12] > 1) Invalid();
                }
                else if (kind == 0x504c5445) // PLTE
                {
                    if (palette || data || colour == 0 || colour == 4 || length < 3 || length > 768 ||
                        length % 3 != 0 || colour == 3 && length / 3 > (1 << depth)) Invalid();
                    palette = true;
                }
                else if (kind == 0x49444154) // IDAT
                {
                    if (dataEnded || colour == 3 && !palette) Invalid();
                    data = true; compressed += length;
                }
                else if (kind == 0x49454e44) // IEND
                {
                    if (length != 0 || !data || compressed < 6 || end + 4 != bytes.Length) Invalid();
                    ended = true;
                }
                else
                {
                    if ((bytes[offset + 4] & 32) == 0 || kind == 0x6163544c || kind == 0x6663544c || kind == 0x66644154)
                        Invalid();
                    if (data) dataEnded = true;
                }
                offset = end + 4;
            }
            if (!ended) Invalid();
        }

        private static void Glb(byte[] bytes)
        {
            if (bytes.Length < 20 || U32(bytes, 0) != 0x46546c67 || U32(bytes, 4) != 2 ||
                U32(bytes, 8) != bytes.Length || U32(bytes, 16) != 0x4e4f534a) Invalid();
            uint jsonSize = U32(bytes, 12);
            if (jsonSize == 0 || jsonSize % 4 != 0 || jsonSize > bytes.Length - 20) Invalid();
            string text = OrchestratorWire.Utf8.GetString(bytes, 20, (int)jsonSize);
            StrictOrchestratorJson.Validate(text);
            JObject doc;
            using (var reader = new JsonTextReader(new StringReader(text)))
            {
                reader.DateParseHandling = DateParseHandling.None;
                reader.MaxDepth = 17;
                doc = JObject.Load(reader, new JsonLoadSettings { DuplicatePropertyNameHandling = DuplicatePropertyNameHandling.Error });
                if (reader.Read()) Invalid();
            }
            if (!(doc["asset"] is JObject asset) || (string)asset["version"] != "2.0") Invalid();
            Resources(doc);
            int binaryStart = 20 + (int)jsonSize, binarySize = 0;
            if (binaryStart < bytes.Length)
            {
                uint size = U32(bytes, binaryStart);
                if (size % 4 != 0 || size > bytes.Length - binaryStart - 8 ||
                    binaryStart + 8L + size != bytes.Length || U32(bytes, binaryStart + 4) != 0x004e4942) Invalid();
                binarySize = (int)size; binaryStart += 8;
            }
            JArray buffers = Array(doc, "buffers"), views = Array(doc, "bufferViews"), images = Array(doc, "images");
            if (buffers.Count > 1) Invalid();
            int bufferSize = 0;
            if (buffers.Count == 1)
            {
                if (!(buffers[0] is JObject buffer)) { Invalid(); return; }
                bufferSize = OrchestratorWire.Integer(buffer["byteLength"], 1, Limit);
                if (binarySize < bufferSize || binarySize - bufferSize > 3) Invalid();
            }
            else if (binarySize != 0) Invalid();
            foreach (JToken item in views)
            {
                if (!(item is JObject view)) { Invalid(); return; }
                OrchestratorWire.Integer(view["buffer"], 0, 0);
                int start = view["byteOffset"] == null ? 0 : OrchestratorWire.Integer(view["byteOffset"], 0, Limit);
                int size = OrchestratorWire.Integer(view["byteLength"], 1, Limit);
                if (start + (long)size > bufferSize) Invalid();
            }
            foreach (JToken item in images)
            {
                if (!(item is JObject image) || (string)item["mimeType"] != "image/png" || views.Count == 0)
                { Invalid(); return; }
                int index = OrchestratorWire.Integer(image["bufferView"], 0, views.Count - 1);
                JToken view = views[index];
                int start = view["byteOffset"] == null ? 0 : (int)view["byteOffset"];
                int size = (int)view["byteLength"];
                byte[] embedded = new byte[size];
                Buffer.BlockCopy(bytes, binaryStart + start, embedded, 0, size);
                Png(embedded);
            }
        }

        private static JArray Array(JObject doc, string name)
        {
            if (doc[name] == null) return new JArray();
            if (!(doc[name] is JArray array)) { Invalid(); return null; }
            return array;
        }

        private static void Resources(JToken node)
        {
            if (node is JObject obj)
            {
                if (obj["uri"] != null || obj["extensionsRequired"] != null &&
                    (!(obj["extensionsRequired"] is JArray required) || required.Count != 0)) Invalid();
                if (obj["extensions"] != null)
                {
                    if (!(obj["extensions"] is JObject extensions)) { Invalid(); return; }
                    foreach (JProperty property in extensions.Properties())
                        if (property.Name != "KHR_materials_unlit" && property.Name != "KHR_materials_specular" &&
                            property.Name != "KHR_materials_ior" && property.Name != "KHR_texture_transform" &&
                            property.Name != "KHR_materials_emissive_strength") Invalid();
                }
                foreach (JProperty property in obj.Properties()) Resources(property.Value);
            }
            else if (node is JArray array) foreach (JToken item in array) Resources(item);
            else if (node.Type == JTokenType.Float && (double.IsNaN((double)node) || double.IsInfinity((double)node))) Invalid();
        }
    }
}