---
description: 'Python conventions for local AI tooling (ComfyUI workflows, content pipelines, MCP scripts).'
applyTo: '**/*.py'
---

# Python Conventions (EchoCradle)

## Style
- Follow PEP 8; 4-space indentation; max line length 100.
- `snake_case` for functions, variables, modules; `PascalCase` for classes.
- Type-hint function signatures.
- Use `pathlib.Path` over string paths.
- Prefer f-strings for formatting.

## Local AI tooling
- Never hardcode model paths — read from config or environment variables.
- Cache generated content keyed by a hash of the prompt/parameters.
- Validate LLM JSON output against a schema (e.g. `pydantic`) before use.
- Treat model output as untrusted; never `eval`/`exec` it.
- Keep prompts in separate template files, not inline in logic.

## ComfyUI
- Store workflows as JSON in version control.
- Use `PARAM_*` placeholders to expose parameters.
- Keep resolutions modest (512–1024) during iteration.

## Ollama
- Default host `http://127.0.0.1:11434`.
- Use `format: "json"` for structured output and validate the result.
- Cap `num_predict` to bound generation time.
