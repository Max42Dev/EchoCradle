"""Strict, bounded wire contracts for the local service (protocol version 1)."""

from __future__ import annotations

import json
import math
from dataclasses import dataclass, field
from typing import Any, Literal
from uuid import UUID

from jsonschema import Draft202012Validator
from jsonschema.exceptions import SchemaError


PROTOCOL_VERSION = 1
JSON_LIMIT = 64 * 1024
TOOL_LIMIT = 16 * 1024
PCM_LIMIT = 960_000
ARTIFACT_LIMIT = 64 * 1024 * 1024
TTL_SECONDS = 600
TERMINAL = frozenset({"succeeded", "failed", "cancelled"})


class ServiceError(Exception):
    """A public error with no secret, prompt, path or native traceback."""

    def __init__(self, code: str, message: str, status: int = 422) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.status = status

    def body(self, job_id: str | None = None) -> dict[str, Any]:
        return {"error": {
            "code": self.code, "message": self.message,
            "retryable": self.status in (429, 503), "job_id": job_id,
        }}


def encode(value: Any, limit: int = JSON_LIMIT) -> bytes:
    """Serialize JSON without NaN, applying a byte cap."""
    try:
        result = json.dumps(value, ensure_ascii=False, allow_nan=False,
                            separators=(",", ":")).encode("utf-8")
    except (ValueError, TypeError, RecursionError, UnicodeError) as exc:
        raise ServiceError("INVALID_JSON", "Invalid JSON value.", 400) from exc
    if len(result) > limit:
        raise ServiceError("PAYLOAD_TOO_LARGE", "JSON exceeds its byte limit.", 413)
    return result


def _pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate key")
        result[key] = value
    return result


def _constant(value: str) -> Any:
    raise ValueError("non-finite JSON")


def decode(raw: bytes | str, limit: int = JSON_LIMIT) -> dict[str, Any]:
    """Reject duplicate keys, non-finite values and excessive structural depth."""
    try:
        if len(raw.encode("utf-8") if isinstance(raw, str) else raw) > limit:
            raise ServiceError("PAYLOAD_TOO_LARGE", "Message exceeds its byte limit.", 413)
        result = json.loads(raw, object_pairs_hook=_pairs, parse_constant=_constant)
        stack = [(result, 0)]
        while stack:
            value, depth = stack.pop()
            if depth > 16:
                raise ValueError("depth")
            if isinstance(value, dict):
                stack.extend((item, depth + 1) for item in value.values())
            elif isinstance(value, list):
                stack.extend((item, depth + 1) for item in value)
            elif isinstance(value, float) and not math.isfinite(value):
                raise ValueError("non-finite number")
        if not isinstance(result, dict):
            raise ValueError("not an object")
        return result
    except (ValueError, RecursionError, UnicodeError) as exc:
        raise ServiceError("INVALID_JSON", "Expected a bounded JSON object.", 400) from exc


def fields(data: dict[str, Any], allowed: set[str], required: set[str] | None = None) -> None:
    if set(data) - allowed or (required or set()) - set(data):
        raise ServiceError("INVALID_SPEC", "Unknown or missing fields.")


def uuid_string(value: Any) -> str:
    if not isinstance(value, str):
        raise ServiceError("INVALID_SPEC", "Expected a UUID string.")
    try:
        return str(UUID(value))
    except ValueError as exc:
        raise ServiceError("INVALID_SPEC", "Expected a UUID string.") from exc


def string(value: Any, maximum: int, *, empty: bool = True) -> str:
    if not isinstance(value, str) or len(value) > maximum or (not empty and not value):
        raise ServiceError("INVALID_SPEC", "String is missing or exceeds its limit.")
    return value


def integer(value: Any, minimum: int, maximum: int) -> int:
    if type(value) is not int or not minimum <= value <= maximum:
        raise ServiceError("INVALID_SPEC", "Integer is outside its permitted range.")
    return value


def session_spec(data: dict[str, Any]) -> tuple[list[dict[str, Any]], str]:
    """Tool declarations cross the wire; implementations stay client-side."""
    fields(data, {"tools", "delivery_mode"})
    tools = data.get("tools", [])
    if not isinstance(tools, list) or len(tools) > 16:
        raise ServiceError("INVALID_SPEC", "At most sixteen tools are permitted.")
    encode({"tools": tools}, TOOL_LIMIT)
    names: set[str] = set()
    for tool in tools:
        if not isinstance(tool, dict):
            raise ServiceError("INVALID_SPEC", "Expected a tool declaration.")
        fields(tool, {"type", "function"}, {"type", "function"})
        function = tool["function"]
        if tool["type"] != "function" or not isinstance(function, dict):
            raise ServiceError("INVALID_SPEC", "Expected a function declaration.")
        fields(function, {"name", "description", "parameters"}, {"name", "parameters"})
        name = string(function["name"], 64, empty=False)
        if not all(char.isascii() and (char.isalnum() or char in "_-") for char in name):
            raise ServiceError("INVALID_SPEC", "Invalid tool name.")
        if name in names:
            raise ServiceError("INVALID_SPEC", "Duplicate tool name.")
        names.add(name)
        string(function.get("description", ""), 4096)
        schema = function["parameters"]
        if not isinstance(schema, dict) or schema.get("type") != "object":
            raise ServiceError("INVALID_SPEC", "Tool parameters must be an object schema.")
        JobSpec.parse({"kind": "text", "text": "validate", "json_schema": schema})
    mode = data.get("delivery_mode", "text")
    if mode not in ("text", "speech", "both"):
        raise ServiceError("INVALID_SPEC", "Invalid delivery mode.")
    return tools, mode


@dataclass(frozen=True)
class JobSpec:
    kind: Literal["text", "dialogue", "tts", "stt"]
    session_id: str | None = None
    turn_id: str | None = None
    messages: list[dict[str, str]] = field(default_factory=list)
    text: str | None = None
    input_artifact_id: str | None = None
    max_tokens: int = 512
    temperature: float = 0.7
    deadline_ms: int = 30_000
    idempotency_key: str | None = None
    streaming: bool = False
    json_schema: dict[str, Any] | None = None

    @classmethod
    def parse(cls, data: dict[str, Any]) -> JobSpec:
        fields(data, set(cls.__dataclass_fields__), {"kind"})
        kind = data["kind"]
        if kind not in ("text", "dialogue", "tts", "stt"):
            raise ServiceError("UNSUPPORTED_KIND", "Unsupported job kind.")
        messages = data.get("messages", [])
        if not isinstance(messages, list) or len(messages) > 64:
            raise ServiceError("INVALID_SPEC", "At most 64 messages are permitted.")
        for message in messages:
            if not isinstance(message, dict):
                raise ServiceError("INVALID_SPEC", "Expected a message object.")
            fields(message, {"role", "content"}, {"role", "content"})
            if message["role"] not in ("system", "user", "assistant"):
                raise ServiceError("INVALID_SPEC", "Invalid message role.")
            string(message["content"], 16_384)
        temperature = data.get("temperature", 0.7)
        if (type(temperature) not in (int, float) or not math.isfinite(temperature)
                or not 0 <= temperature <= 2):
            raise ServiceError("INVALID_SPEC", "Temperature must be finite and within 0..2.")
        streaming = data.get("streaming", False)
        if type(streaming) is not bool:
            raise ServiceError("INVALID_SPEC", "Streaming must be boolean.")
        schema = data.get("json_schema")
        if schema is not None:
            if kind != "text" or not isinstance(schema, dict):
                raise ServiceError("INVALID_SPEC", "Schemas require a text job.")
            schema = decode(encode(schema, TOOL_LIMIT), TOOL_LIMIT)
            # No external references: validation must never access network or files.
            stack = [schema]
            while stack:
                node = stack.pop()
                if isinstance(node, dict):
                    if any(key in node for key in ("$ref", "$dynamicRef", "$recursiveRef")):
                        raise ServiceError("INVALID_SPEC", "Schema references are not supported.")
                    stack.extend(node.values())
                elif isinstance(node, list):
                    stack.extend(node)
            try:
                Draft202012Validator.check_schema(schema)
            except SchemaError as exc:
                raise ServiceError("INVALID_SPEC", "Invalid response schema.") from exc
        text = data.get("text")
        if text is not None:
            string(text, 300 if kind == "tts" else 16_384, empty=False)
        artifact = data.get("input_artifact_id")
        if kind == "stt":
            if artifact is None or text is not None or messages or streaming:
                raise ServiceError("INVALID_SPEC", "STT requires only a PCM artifact input.")
        elif artifact is not None:
            raise ServiceError("INVALID_SPEC", "PCM input is only valid for STT.")
        if kind == "tts" and (not text or messages or streaming):
            raise ServiceError("INVALID_SPEC", "TTS requires text and is not socket streaming.")
        if kind in ("text", "dialogue") and not messages and not text:
            raise ServiceError("INVALID_SPEC", "Text jobs require messages or text.")
        return cls(
            kind=kind,
            session_id=(uuid_string(data["session_id"])
                        if data.get("session_id") is not None else None),
            turn_id=(uuid_string(data["turn_id"])
                     if data.get("turn_id") is not None else None),
            messages=messages, text=text,
            input_artifact_id=uuid_string(artifact) if artifact is not None else None,
            max_tokens=integer(data.get("max_tokens", 512), 1, 512),
            temperature=float(temperature),
            deadline_ms=integer(data.get("deadline_ms", 30_000), 1000, 120_000),
            idempotency_key=(string(data["idempotency_key"], 128, empty=False)
                             if data.get("idempotency_key") is not None else None),
            streaming=streaming, json_schema=schema,
        )


def envelope(data: dict[str, Any], expected_seq: int) -> tuple[str, dict[str, Any]]:
    fields(data, {"v", "seq", "type", "payload", "service_instance_id", "session_id",
                  "job_id", "turn_id"}, {"v", "seq", "type", "payload"})
    if type(data["v"]) is not int or data["v"] != PROTOCOL_VERSION:
        raise ServiceError("PROTOCOL_ERROR", "Protocol version must be 1.", 400)
    if integer(data["seq"], 1, 2**64 - 1) != expected_seq:
        raise ServiceError("PROTOCOL_ERROR", "Sequence must be contiguous.", 400)
    event = string(data["type"], 64, empty=False)
    payload = data["payload"]
    if not isinstance(payload, dict):
        raise ServiceError("PROTOCOL_ERROR", "Payload must be an object.", 400)
    return event, payload