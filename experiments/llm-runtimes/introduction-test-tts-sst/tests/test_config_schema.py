"""The config schema and its validator."""

from __future__ import annotations

from config_schema import (
    is_complete,
    missing_fields,
    normalize,
    validate_config,
)


def test_valid_config_has_no_errors():
    data = {"username": "Ada", "style": "medieval", "ai_name": "Vex"}
    assert validate_config(data) == []


def test_missing_required_field_is_reported():
    errors = validate_config({"username": "Ada", "style": "medieval"})
    assert any("ai_name" in e for e in errors)


def test_unexpected_field_is_reported():
    errors = validate_config(
        {"username": "Ada", "style": "medieval", "ai_name": "Vex", "extra": "nope"}
    )
    assert any("extra" in e for e in errors)


def test_wrong_type_is_reported():
    errors = validate_config({"username": 5, "style": "medieval", "ai_name": "Vex"})
    assert any("username" in e for e in errors)


def test_empty_string_is_reported():
    errors = validate_config({"username": "", "style": "medieval", "ai_name": "Vex"})
    assert any("username" in e for e in errors)


def test_non_object_is_reported():
    assert validate_config(["not", "an", "object"])


def test_story_is_optional():
    assert validate_config({"username": "Ada", "style": "nature", "ai_name": "Vex"}) == []


def test_is_complete_requires_non_empty_required_fields():
    assert is_complete({"username": "Ada", "style": "nature", "ai_name": "Vex"})
    assert not is_complete({"username": "Ada", "style": "  ", "ai_name": "Vex"})


def test_missing_fields_in_ask_order():
    assert missing_fields({"username": "Ada"}) == ["style", "ai_name"]


def test_normalize_trims_and_drops_empty_optional():
    result = normalize(
        {"username": "  Ada  ", "style": "nature", "ai_name": "Vex", "story": "   "}
    )
    assert result == {"username": "Ada", "style": "nature", "ai_name": "Vex"}
