"""Tools the model can call during a conversation.

A *tool* is a small, typed capability the LLM may invoke mid-turn instead of
guessing. The orchestrator exposes them in the OpenAI ``tools`` format, which
llama.cpp understands, and executes the calls the model asks for.

The first concrete tool is :class:`JsonConfigTool`: a generic writer for a JSON
document validated against a schema. It is deliberately not specific to any one
config — the interview is just one instance of it.

Design notes
------------
* Tools are **pure and local**. They never touch the network and never execute
  model output as code (Golden Rule 5).
* Every tool returns a JSON-serialisable result; errors are returned as data
  (``{"ok": false, "error": ...}``) so the model can correct itself rather than
  the turn failing.
* Arguments are validated before use; a malformed call is reported back to the
  model, not raised.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Protocol, runtime_checkable


@dataclass(frozen=True)
class ToolSpec:
    """The declaration handed to the model."""

    name: str
    description: str
    parameters: dict[str, Any]

    def to_openai(self) -> dict[str, Any]:
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": self.parameters,
            },
        }


@runtime_checkable
class Tool(Protocol):
    """Something the model can call."""

    spec: ToolSpec

    def invoke(self, arguments: dict[str, Any]) -> Any:
        """Run the tool. Must not raise for bad input; return an error object."""


@dataclass
class ToolCall:
    """One call the model requested."""

    id: str
    name: str
    arguments: dict[str, Any]
    result: Any = None
    error: str | None = None


class ToolRegistry:
    """A named set of tools, plus the loop that executes model requests."""

    def __init__(self, tools: list[Tool] | None = None) -> None:
        self._tools: dict[str, Tool] = {}
        for tool in tools or []:
            self.register(tool)

    def register(self, tool: Tool) -> None:
        self._tools[tool.spec.name] = tool

    def __len__(self) -> int:
        return len(self._tools)

    def __contains__(self, name: object) -> bool:
        return name in self._tools

    def specs(self) -> list[dict[str, Any]]:
        """All tool declarations, in the OpenAI ``tools`` format."""
        return [t.spec.to_openai() for t in self._tools.values()]

    def invoke(self, name: str, arguments: dict[str, Any]) -> Any:
        """Execute one tool, converting failures into a result object."""
        tool = self._tools.get(name)
        if tool is None:
            return {"ok": False, "error": f"unknown tool: {name}"}
        try:
            return tool.invoke(arguments)
        except Exception as exc:  # noqa: BLE001 - reported to the model
            return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}


# ---------------------------------------------------------------------------
# JsonConfigTool
# ---------------------------------------------------------------------------


def _validate(value: Any, spec: dict[str, Any]) -> str | None:
    """Validate one value against one property schema. Returns an error or None."""
    expected = spec.get("type")
    if expected == "string":
        if not isinstance(value, str):
            return f"expected a string, got {type(value).__name__}"
        if "minLength" in spec and len(value) < spec["minLength"]:
            return "must not be empty"
        if "maxLength" in spec and len(value) > spec["maxLength"]:
            return f"too long ({len(value)} > {spec['maxLength']} chars)"
    elif expected == "integer":
        if not isinstance(value, int) or isinstance(value, bool):
            return f"expected an integer, got {type(value).__name__}"
    elif expected == "number":
        if not isinstance(value, (int, float)) or isinstance(value, bool):
            return f"expected a number, got {type(value).__name__}"
    elif expected == "boolean":
        if not isinstance(value, bool):
            return f"expected a boolean, got {type(value).__name__}"
    return None


class JsonConfigTool:
    """Write a JSON document, validated against a schema.

    Exposes a single tool to the model, ``<name>_set``. It sets one field and
    returns the whole document: the values set so far, which required fields are
    still missing, and any validation problems. One tool is enough because every
    call reports the full state, so the model never needs a separate read or
    validate step.

    The document is held in memory and only written to disk by :meth:`save`, so
    a half-finished interview never leaves a broken file behind.
    """

    def __init__(
        self,
        schema: dict[str, Any],
        *,
        name: str = "config",
        data: dict[str, Any] | None = None,
        path: Path | None = None,
        description: str = "the configuration document",
    ) -> None:
        self.schema = schema
        self.name = name
        self.path = Path(path) if path else None
        self.description = description
        self.data: dict[str, Any] = dict(data or {})
        self._properties: dict[str, Any] = schema.get("properties") or {}
        self._required: list[str] = list(schema.get("required") or [])
        self._allow_extra = schema.get("additionalProperties", True) is not False

    # -- tool surface ------------------------------------------------------

    @property
    def tools(self) -> list[Tool]:
        return [self._set_tool()]

    def register_into(self, registry: ToolRegistry) -> None:
        for tool in self.tools:
            registry.register(tool)

    # -- operations --------------------------------------------------------

    def missing(self) -> list[str]:
        """Required fields that are absent or empty."""
        out = []
        for name in self._required:
            value = self.data.get(name)
            if value is None or (isinstance(value, str) and not value.strip()):
                out.append(name)
        return out

    def errors(self) -> list[str]:
        """All validation problems with the current document."""
        problems: list[str] = []
        for name in self._required:
            if name in self.missing():
                problems.append(f"missing required field: {name}")
        for name, value in self.data.items():
            spec = self._properties.get(name)
            if spec is None:
                if not self._allow_extra:
                    problems.append(f"unexpected field: {name}")
                continue
            problem = _validate(value, spec)
            if problem:
                problems.append(f"{name}: {problem}")
        return problems

    def state(self) -> dict[str, Any]:
        """The document, what is set, what is missing, and any problems.

        Every tool result carries this, so the model always sees the whole
        document and never needs a separate read or validate call.
        """
        problems = self.errors()
        return {
            "config": dict(self.data),
            "missing": self.missing(),
            "complete": not self.missing(),
            "valid": not problems,
            "errors": problems,
        }

    def set_field(self, field_name: str, value: Any) -> dict[str, Any]:
        """Set one field. Returns the result plus the whole document state."""
        spec = self._properties.get(field_name)
        if spec is None:
            known = ", ".join(self._properties) or "(none)"
            return {
                "ok": False,
                "error": f"unknown field {field_name!r}; known fields: {known}",
                **self.state(),
            }
        problem = _validate(value, spec)
        if problem:
            return {"ok": False, "error": f"{field_name}: {problem}", **self.state()}

        cleaned = value.strip() if isinstance(value, str) else value
        if isinstance(cleaned, str) and not cleaned:
            return {"ok": False, "error": f"{field_name}: must not be empty", **self.state()}

        self.data[field_name] = cleaned
        return {"ok": True, "field": field_name, "value": cleaned, **self.state()}

    def save(self, path: Path | None = None) -> Path:
        """Write the document to disk. Raises if it is invalid."""
        problems = self.errors()
        if problems:
            raise ValueError("refusing to save an invalid config: " + "; ".join(problems))
        target = Path(path) if path else self.path
        if target is None:
            raise ValueError("no path given and none configured")
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(json.dumps(self.data, indent=2) + "\n", encoding="utf-8")
        return target

    # -- tool implementations ---------------------------------------------

    def _set_tool(self) -> Tool:
        # Every field stays a free string -- `style` and `story` are open text,
        # and constraining them would be wrong. What the model needs is not a
        # narrower value but a clearer *field*: a bare list of names says which
        # fields exist, never what belongs in them, so it guesses (Granite 4.2 8B
        # answered a question about `style` by writing "Medieval" into
        # `username`). The meanings go in the argument descriptions instead.
        fields = "\n".join(
            f"- {name}: {spec.get('description', '')}"
            for name, spec in self._properties.items()
        )

        def invoke(arguments: dict[str, Any]) -> Any:
            field_name = arguments.get("field")
            if not isinstance(field_name, str):
                return {
                    "ok": False,
                    "error": "argument 'field' must be a string",
                    **self.state(),
                }
            if "value" not in arguments:
                return {
                    "ok": False,
                    "error": "argument 'value' is required",
                    **self.state(),
                }
            return self.set_field(field_name, arguments["value"])

        return _FnTool(
            ToolSpec(
                name=f"{self.name}_set",
                description=(
                    f"Set or update one field of {self.description}. "
                    f"An existing value is overwritten, not appended; fields already "
                    f"filled remain editable. Call this for every new, corrected, "
                    f"or confirmed value, even if that field is not missing. "
                    f"Saying a value in your reply does not update the document; "
                    f"the tool must be called. Returns the whole document, which fields "
                    f"are still missing, and any problems.\n"
                    f"The fields, and what belongs in each:\n{fields}"
                ),
                parameters={
                    "type": "object",
                    "properties": {
                        "field": {
                            "type": "string",
                            "enum": list(self._properties),
                            "description": (
                                "Which field to set or overwrite, including an already-filled "
                                "field. Choose by what the value is, "
                                "not by the order it was given: "
                                + "; ".join(
                                    f"{name} = {spec.get('description', '')}"
                                    for name, spec in self._properties.items()
                                )
                            ),
                        },
                        "value": {
                            "type": "string",
                            "description": (
                                "The complete new value to store, replacing any previous "
                                "value in this field. Free text; preserve the user's "
                                "intended wording, resolving explicit spelling or corrections."
                            ),
                        },
                    },
                    "required": ["field", "value"],
                    "additionalProperties": False,
                },
            ),
            invoke,
        )


@dataclass
class _FnTool:
    """Adapter turning a plain function into a :class:`Tool`."""

    spec: ToolSpec
    fn: Callable[[dict[str, Any]], Any] = field(repr=False)

    def invoke(self, arguments: dict[str, Any]) -> Any:
        return self.fn(arguments)
