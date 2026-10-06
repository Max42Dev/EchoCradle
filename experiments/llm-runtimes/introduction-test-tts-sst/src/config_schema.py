"""The config schema the interview fills, and a tiny validator for it.

The schema is a plain JSON Schema document so it can be handed straight to the
text host as ``response_format`` (schema-constrained decoding) *and* used to
validate the final file. LLM output is untrusted (Golden Rule 5), so the same
schema is enforced twice: once by the decoder, once by us.

The validator is deliberately small and dependency-free — it supports only the
keywords this schema uses.
"""

from __future__ import annotations

from typing import Any

#: The document the interview produces.
CONFIG_SCHEMA: dict[str, Any] = {
    "type": "object",
    "required": ["username", "style", "ai_name"],
    "additionalProperties": False,
    "properties": {
        "username": {
            "type": "string",
            "minLength": 1,
            "maxLength": 40,
            "description": "What the player wants to be called.",
        },
        "style": {
            "type": "string",
            "minLength": 1,
            "maxLength": 120,
            "description": (
                "The world's style, e.g. medieval, sci-fi-steampunk, nature. "
                "Open field: any short phrase is valid."
            ),
        },
        "ai_name": {
            "type": "string",
            "minLength": 1,
            "maxLength": 40,
            "description": "The name of the AI companion.",
        },
        "story": {
            "type": "string",
            "maxLength": 2000,
            "description": "Optional backstory. May be empty or absent.",
        },
    },
}

#: Fields the interview must fill before it can finish.
REQUIRED_FIELDS: tuple[str, ...] = ("username", "style", "ai_name")

#: Fields the interview asks about, in order.
FIELD_ORDER: tuple[str, ...] = ("username", "style", "ai_name", "story")

#: Human-readable prompts used when the LLM has not filled a field yet.
FIELD_QUESTIONS: dict[str, str] = {
    "username": "What should I call you?",
    "style": "What kind of world are we building? (medieval, sci-fi-steampunk, nature, ...)",
    "ai_name": "And what will you name me?",
    "story": "Anything else I should know about your story? (optional)",
}


def _check_type(value: Any, expected: str) -> bool:
    if expected == "string":
        return isinstance(value, str)
    if expected == "object":
        return isinstance(value, dict)
    if expected == "boolean":
        return isinstance(value, bool)
    if expected == "integer":
        return isinstance(value, int) and not isinstance(value, bool)
    if expected == "number":
        return isinstance(value, (int, float)) and not isinstance(value, bool)
    return True


def validate_config(data: Any) -> list[str]:
    """Validate ``data`` against :data:`CONFIG_SCHEMA`.

    Returns a list of human-readable problems; empty means valid.
    """
    errors: list[str] = []

    if not _check_type(data, "object"):
        return [f"expected an object, got {type(data).__name__}"]

    properties: dict[str, Any] = CONFIG_SCHEMA["properties"]
    required: list[str] = CONFIG_SCHEMA["required"]

    for name in required:
        if name not in data:
            errors.append(f"missing required field: {name}")

    if CONFIG_SCHEMA.get("additionalProperties") is False:
        for name in data:
            if name not in properties:
                errors.append(f"unexpected field: {name}")

    for name, value in data.items():
        spec = properties.get(name)
        if spec is None:
            continue
        if not _check_type(value, spec["type"]):
            errors.append(f"{name}: expected {spec['type']}, got {type(value).__name__}")
            continue
        if spec["type"] == "string":
            if "minLength" in spec and len(value) < spec["minLength"]:
                errors.append(f"{name}: must not be empty")
            if "maxLength" in spec and len(value) > spec["maxLength"]:
                errors.append(
                    f"{name}: too long ({len(value)} > {spec['maxLength']} chars)"
                )

    return errors


def is_complete(data: dict[str, Any]) -> bool:
    """True when every required field is present and non-empty."""
    return all(
        isinstance(data.get(f), str) and data[f].strip() for f in REQUIRED_FIELDS
    )


def missing_fields(data: dict[str, Any]) -> list[str]:
    """Required fields that are still empty, in ask order."""
    return [
        f
        for f in FIELD_ORDER
        if f in REQUIRED_FIELDS and not (isinstance(data.get(f), str) and data[f].strip())
    ]


def normalize(data: dict[str, Any]) -> dict[str, Any]:
    """Trim strings and drop empty optional fields, ready to write to disk."""
    out: dict[str, Any] = {}
    for name in FIELD_ORDER:
        value = data.get(name)
        if isinstance(value, str):
            value = value.strip()
            if value:
                out[name] = value
    return out
