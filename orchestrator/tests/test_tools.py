"""The generic JSON config tool and the tool registry."""

from __future__ import annotations

import json

import pytest

from orchestrator.tools import JsonConfigTool, ToolRegistry, ToolSpec

SCHEMA = {
    "type": "object",
    "required": ["username", "style"],
    "additionalProperties": False,
    "properties": {
        "username": {"type": "string", "minLength": 1, "maxLength": 10},
        "style": {"type": "string", "minLength": 1},
        "story": {"type": "string"},
    },
}


def _tool(**kwargs) -> JsonConfigTool:
    return JsonConfigTool(SCHEMA, name="config", **kwargs)


def test_exposes_one_tool():
    names = {t.spec.name for t in _tool().tools}
    assert names == {"config_set"}


def test_specs_are_openai_shaped():
    registry = ToolRegistry()
    _tool().register_into(registry)
    specs = registry.specs()
    assert len(specs) == 1
    for spec in specs:
        assert spec["type"] == "function"
        assert spec["function"]["name"]
        assert "parameters" in spec["function"]


def test_set_declaration_explicitly_describes_corrections_and_overwrite():
    registry = ToolRegistry()
    _tool().register_into(registry)
    declaration = registry.specs()[0]["function"]
    description = declaration["description"]
    assert "existing value is overwritten" in description
    assert "corrected" in description
    assert "even if that field is not missing" in description
    assert "the tool must be called" in description
    properties = declaration["parameters"]["properties"]
    assert "already-filled" in properties["field"]["description"]
    assert "replacing any previous" in properties["value"]["description"]


def test_invalid_correction_preserves_previous_value():
    tool = _tool(data={"username": "Ada"})
    registry = ToolRegistry()
    tool.register_into(registry)
    result = registry.invoke("config_set", {"field": "username", "value": ""})
    assert not result["ok"]
    assert tool.data["username"] == "Ada"


def test_set_field_stores_and_reports_missing():
    tool = _tool()
    result = tool.set_field("username", "Ada")
    assert result["ok"]
    assert result["config"] == {"username": "Ada"}
    assert result["missing"] == ["style"]


def test_set_field_rejects_unknown_field():
    result = _tool().set_field("nope", "x")
    assert not result["ok"]
    assert "unknown field" in result["error"]


def test_set_field_rejects_wrong_type():
    result = _tool().set_field("username", 5)
    assert not result["ok"]
    assert "string" in result["error"]


def test_set_field_rejects_empty_string():
    result = _tool().set_field("username", "   ")
    assert not result["ok"]


def test_set_field_rejects_too_long():
    result = _tool().set_field("username", "x" * 11)
    assert not result["ok"]
    assert "too long" in result["error"]


def test_set_field_trims_whitespace():
    result = _tool().set_field("username", "  Ada  ")
    assert result["value"] == "Ada"


def test_set_field_overwrites_an_existing_value():
    # The player may correct a value ("my name is Max, not Mex"), so setting a
    # field that is already set must replace it, not be rejected.
    tool = _tool()
    tool.set_field("username", "Mex")
    result = tool.set_field("username", "Max")

    assert result["ok"]
    assert result["config"]["username"] == "Max"
    assert tool.data["username"] == "Max"


def test_set_field_overwrite_via_the_registry():
    # Same thing through the tool the model actually calls.
    tool = _tool()
    registry = ToolRegistry()
    tool.register_into(registry)

    registry.invoke("config_set", {"field": "username", "value": "Mex"})
    result = registry.invoke("config_set", {"field": "username", "value": "Max"})

    assert result["ok"]
    assert result["config"]["username"] == "Max"


def test_set_field_overwrite_does_not_duplicate_or_leave_the_old_value():
    tool = _tool()
    tool.set_field("username", "Mex")
    tool.set_field("username", "Max")

    assert list(tool.data) == ["username"]
    assert "Mex" not in tool.data.values()


def test_missing_tracks_required_fields():
    tool = _tool()
    assert tool.missing() == ["username", "style"]
    tool.set_field("username", "Ada")
    assert tool.missing() == ["style"]


def test_errors_reports_unexpected_field():
    tool = _tool(data={"username": "Ada", "style": "x", "bogus": "y"})
    assert any("bogus" in e for e in tool.errors())


def test_set_result_reports_validity():
    tool = _tool(data={"username": "Ada"})
    result = tool.set_field("style", "nature")
    assert result["valid"]
    assert result["errors"] == []
    assert result["complete"]


def test_set_result_returns_full_state():
    registry = ToolRegistry()
    tool = _tool(data={"username": "Ada"})
    tool.register_into(registry)
    result = registry.invoke("config_set", {"field": "style", "value": "nature"})
    assert result["config"] == {"username": "Ada", "style": "nature"}
    assert result["missing"] == []
    assert result["complete"]
    assert result["valid"]
    assert result["errors"] == []


def test_failed_set_still_returns_state():
    tool = _tool(data={"username": "Ada"})
    result = tool.set_field("nope", "x")
    assert not result["ok"]
    assert result["config"] == {"username": "Ada"}
    assert result["missing"] == ["style"]


def test_set_tool_via_registry():
    registry = ToolRegistry()
    _tool().register_into(registry)
    result = registry.invoke("config_set", {"field": "username", "value": "Ada"})
    assert result["ok"]


def test_set_tool_requires_value():
    registry = ToolRegistry()
    _tool().register_into(registry)
    result = registry.invoke("config_set", {"field": "username"})
    assert not result["ok"]


def test_unknown_tool_returns_error_not_raises():
    registry = ToolRegistry()
    result = registry.invoke("nope", {})
    assert not result["ok"]
    assert "unknown tool" in result["error"]


def test_set_field_still_accepts_values_that_look_like_choices():
    """The `field` enum stays bare names; the *value* is always free text."""
    tool = _tool()
    result = tool.set_field("style", "medieval (dark and stormy)")
    assert result["ok"]
    assert tool.data["style"] == "medieval (dark and stormy)"


def test_set_tool_describes_what_belongs_in_each_field():
    registry = ToolRegistry()
    schema = {
        "type": "object",
        "required": ["username"],
        "properties": {
            "username": {"type": "string", "description": "What the player wants to be called."},
            "style": {"type": "string", "description": "The world's style, e.g. medieval."},
        },
    }
    JsonConfigTool(schema, name="config").register_into(registry)
    set_spec = next(s for s in registry.specs() if s["function"]["name"] == "config_set")
    field_arg = set_spec["function"]["parameters"]["properties"]["field"]
    # Bare names only -- `style` must remain an open field, not an enum of values.
    assert field_arg["enum"] == ["username", "style"]
    # The meanings are attached so the model can map an answer to the right field.
    assert "What the player wants to be called." in field_arg["description"]
    assert "The world's style, e.g. medieval." in field_arg["description"]
    assert "What the player wants to be called." in set_spec["function"]["description"]


def test_tool_exception_is_captured():
    class Boom:
        spec = ToolSpec("boom", "explodes", {"type": "object", "properties": {}})

        def invoke(self, arguments):
            raise ValueError("kaboom")

    registry = ToolRegistry([Boom()])
    result = registry.invoke("boom", {})
    assert not result["ok"]
    assert "kaboom" in result["error"]


def test_save_refuses_invalid_document(tmp_path):
    tool = _tool(path=tmp_path / "c.json")
    tool.set_field("username", "Ada")
    with pytest.raises(ValueError):
        tool.save()


def test_save_writes_valid_document(tmp_path):
    target = tmp_path / "c.json"
    tool = _tool(path=target)
    tool.set_field("username", "Ada")
    tool.set_field("style", "nature")
    written = tool.save()
    assert written == target
    assert json.loads(target.read_text(encoding="utf-8")) == {
        "username": "Ada",
        "style": "nature",
    }


def test_save_without_path_raises():
    tool = _tool()
    tool.set_field("username", "Ada")
    tool.set_field("style", "nature")
    with pytest.raises(ValueError):
        tool.save()


def test_tool_is_generic_over_schemas():
    """The same class works for a completely different document."""
    other = {
        "type": "object",
        "required": ["hp"],
        "properties": {"hp": {"type": "integer"}, "name": {"type": "string"}},
    }
    tool = JsonConfigTool(other, name="npc")
    assert tool.set_field("hp", 12)["ok"]
    assert not tool.set_field("hp", "twelve")["ok"]
    assert tool.missing() == []
