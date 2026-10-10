namespace EchoCradle.Orchestration
{
    // Newtonsoft intentionally accepts JavaScript extensions. This small syntax gate excludes
    // comments, single quotes, trailing commas, undefined, hex/octal and unquoted property names.
    internal sealed class StrictOrchestratorJson
    {
        private readonly string _text;
        private int _position;

        private StrictOrchestratorJson(string text) { _text = text; }

        internal static void Validate(string text)
        {
            var parser = new StrictOrchestratorJson(text);
            parser.Space();
            if (parser.Peek() != '{') parser.Invalid();
            parser.Value(0);
            parser.Space();
            if (parser._position != text.Length) parser.Invalid();
        }

        private char Peek() => _position < _text.Length ? _text[_position] : '\0';
        private void Invalid() { throw new OrchestratorException("INVALID_JSON"); }
        private void Space()
        {
            while (Peek() == ' ' || Peek() == '\t' || Peek() == '\r' || Peek() == '\n') _position++;
        }
        private void Expect(char value)
        {
            Space();
            if (Peek() != value) Invalid();
            _position++;
        }
        private void Literal(string value)
        {
            foreach (char c in value)
            {
                if (Peek() != c) Invalid();
                _position++;
            }
        }
        private void String()
        {
            Expect('"');
            while (true)
            {
                char c = Peek();
                _position++;
                if (c == '"') return;
                if (c < 32) Invalid();
                if (c != '\\') continue;
                char escape = Peek();
                _position++;
                if (escape == 'u')
                {
                    for (int i = 0; i < 4; i++)
                    {
                        char hex = Peek();
                        if (!(hex >= '0' && hex <= '9') && !(hex >= 'a' && hex <= 'f') &&
                            !(hex >= 'A' && hex <= 'F')) Invalid();
                        _position++;
                    }
                }
                else if (escape != '"' && escape != '\\' && escape != '/' && escape != 'b' &&
                    escape != 'f' && escape != 'n' && escape != 'r' && escape != 't') Invalid();
            }
        }
        private bool Digit() => Peek() >= '0' && Peek() <= '9';
        private void Number()
        {
            if (Peek() == '-') _position++;
            if (Peek() == '0') _position++;
            else
            {
                if (Peek() < '1' || Peek() > '9') Invalid();
                while (Digit()) _position++;
            }
            if (Peek() == '.')
            {
                _position++;
                if (!Digit()) Invalid();
                while (Digit()) _position++;
            }
            if (Peek() == 'e' || Peek() == 'E')
            {
                _position++;
                if (Peek() == '+' || Peek() == '-') _position++;
                if (!Digit()) Invalid();
                while (Digit()) _position++;
            }
        }
        private void Value(int depth)
        {
            if (depth > 16) Invalid();
            Space();
            char c = Peek();
            if (c == '"') { String(); return; }
            if (c == 't') { Literal("true"); return; }
            if (c == 'f') { Literal("false"); return; }
            if (c == 'n') { Literal("null"); return; }
            if (c != '{' && c != '[') { Number(); return; }
            _position++;
            Space();
            char end = c == '{' ? '}' : ']';
            if (Peek() == end) { _position++; return; }
            while (true)
            {
                if (c == '{') { String(); Expect(':'); }
                Value(depth + 1);
                Space();
                if (Peek() == end) { _position++; return; }
                Expect(',');
                Space();
            }
        }
    }
}