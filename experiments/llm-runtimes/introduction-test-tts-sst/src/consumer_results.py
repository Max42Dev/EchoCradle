"""JSON reporting for immutable application snapshots (without deepcopy)."""

from collections.abc import Mapping
from dataclasses import fields, is_dataclass
from typing import Any


def json_value(value: Any) -> Any:
    """Detach frozen dataclasses, mappings and tuples into JSON containers."""
    if is_dataclass(value) and not isinstance(value, type):
        return {field.name: json_value(getattr(value, field.name)) for field in fields(value)}
    if isinstance(value, Mapping):
        return {key: json_value(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [json_value(item) for item in value]
    return value