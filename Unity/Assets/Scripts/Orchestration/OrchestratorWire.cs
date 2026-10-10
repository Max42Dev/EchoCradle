using System;
using System.IO;
using System.Net;
using System.Text;
using Newtonsoft.Json;
using Newtonsoft.Json.Linq;

namespace EchoCradle.Orchestration
{
    internal static class OrchestratorWire
    {
        internal const int JsonLimit = 65536;
        internal const int ToolLimit = 16384;
        internal const int PcmLimit = 960000;
        internal static readonly UTF8Encoding Utf8 = new UTF8Encoding(false, true);

        internal static Uri Origin(string url)
        {
            if (string.IsNullOrEmpty(url) || url.IndexOfAny(new[] { '?', '#' }) >= 0)
                throw new OrchestratorException("INVALID_ORIGIN");
            foreach (char c in url)
                if (char.IsWhiteSpace(c) || char.IsControl(c))
                    throw new OrchestratorException("INVALID_ORIGIN");
            if (!Uri.TryCreate(url, UriKind.Absolute, out Uri uri) ||
                (uri.Scheme != "http" && uri.Scheme != "https") ||
                uri.UserInfo.Length != 0 || uri.AbsolutePath != "/" ||
                uri.Port < 1 || uri.Port > 65535)
                throw new OrchestratorException("INVALID_ORIGIN");
            // Literal loopback only: no DNS rebinding or proxy/environment-dependent routing.
            string host = uri.Host.Trim('[', ']');
            if (host == "localhost")
                return new UriBuilder(uri) { Host = "127.0.0.1" }.Uri;
            if ((host != "127.0.0.1" && host != "::1") ||
                !IPAddress.TryParse(host, out IPAddress address) || !IPAddress.IsLoopback(address))
                throw new OrchestratorException("INVALID_ORIGIN");
            return uri;
        }

        internal static void Token(string token)
        {
            if (string.IsNullOrEmpty(token) || token.Length > 4096)
                throw new OrchestratorException("INVALID_TOKEN");
            foreach (char c in token)
                if (c < 33 || c > 126)
                    throw new OrchestratorException("INVALID_TOKEN");
        }

        internal static string Id(JToken value)
        {
            if (value?.Type != JTokenType.String || !Guid.TryParse((string)value, out Guid id) ||
                (string)value != id.ToString("D"))
                throw new OrchestratorException("PROTOCOL_ERROR");
            return id.ToString("D");
        }

        internal static int Integer(JToken value, int min, int max)
        {
            if (value?.Type != JTokenType.Integer)
                throw new OrchestratorException("PROTOCOL_ERROR");
            try
            {
                long number = (long)value;
                if (number >= min && number <= max) return (int)number;
            }
            catch (Exception) { }
            throw new OrchestratorException("PROTOCOL_ERROR");
        }

        internal static string Text(JToken value, int max = 24576)
        {
            if (value?.Type != JTokenType.String || ((string)value).Length > max)
                throw new OrchestratorException("PROTOCOL_ERROR");
            return (string)value;
        }

        private static void Validate(JToken value, int depth)
        {
            if (depth > 16) throw new OrchestratorException("INVALID_JSON");
            if (value.Type == JTokenType.Float)
            {
                double number = (double)value;
                if (double.IsNaN(number) || double.IsInfinity(number))
                    throw new OrchestratorException("INVALID_JSON");
            }
            if (value.Type == JTokenType.Undefined || value.Type == JTokenType.Raw ||
                value.Type == JTokenType.Constructor || value.Type == JTokenType.Bytes ||
                value.Type == JTokenType.Date)
                throw new OrchestratorException("INVALID_JSON");
            if (value is JObject obj)
                foreach (JProperty property in obj.Properties()) Validate(property.Value, depth + 1);
            else if (value is JArray array)
                foreach (JToken item in array) Validate(item, depth + 1);
        }

        internal static byte[] Encode(JToken value, int limit = JsonLimit)
        {
            try
            {
                Validate(value, 0);
                byte[] bytes = Utf8.GetBytes(value.ToString(Formatting.None));
                if (bytes.Length > limit) throw new OrchestratorException("PAYLOAD_LIMIT");
                return bytes;
            }
            catch (OrchestratorException) { throw; }
            catch (Exception) { throw new OrchestratorException("INVALID_JSON"); }
        }

        internal static JObject Decode(byte[] bytes)
        {
            if (bytes.Length > JsonLimit) throw new OrchestratorException("PAYLOAD_LIMIT");
            try
            {
                string text = Utf8.GetString(bytes);
                StrictOrchestratorJson.Validate(text);
                using (var reader = new JsonTextReader(new StringReader(text)))
                {
                    reader.DateParseHandling = DateParseHandling.None;
                    reader.MaxDepth = 17;
                    JObject result = JObject.Load(reader, new JsonLoadSettings
                    {
                        DuplicatePropertyNameHandling = DuplicatePropertyNameHandling.Error,
                        CommentHandling = CommentHandling.Load
                    });
                    if (reader.Read()) throw new OrchestratorException("INVALID_JSON");
                    Validate(result, 0);
                    return result;
                }
            }
            catch (OrchestratorException) { throw; }
            catch (Exception) { throw new OrchestratorException("INVALID_JSON"); }
        }
    }
}