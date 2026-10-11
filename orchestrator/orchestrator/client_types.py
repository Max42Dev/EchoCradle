"""Small application contracts; results are deeply immutable JSON snapshots."""

from __future__ import annotations

from dataclasses import dataclass
from types import MappingProxyType
from typing import Any, Literal, Mapping, Protocol, TypedDict


JsonValue = None | bool | int | float | str | tuple["JsonValue", ...] | Mapping[str, "JsonValue"]
TerminalState = Literal["succeeded", "failed", "cancelled"]


def freeze_json(value: Any) -> JsonValue:
    """Detach from mutable wire/registry values (validated before freezing)."""
    if isinstance(value, Mapping):
        return MappingProxyType({key: freeze_json(item) for key, item in value.items()})
    if isinstance(value, (list, tuple)):
        return tuple(freeze_json(item) for item in value)
    return value


class Message(TypedDict):
    role: Literal["system", "user", "assistant"]
    content: str


class ToolRegistry(Protocol):
    def specs(self) -> list[dict[str, Any]]: ...
    def invoke(self, name: str, arguments: dict[str, Any]) -> Any: ...


class KindCapability(TypedDict, total=False):
    state: Literal["ready", "warming", "unsupported", "unprovisioned", "unavailable"]
    model_id: str
    sample_rate: int
    speaker_id: int
    partials: bool


class PcmCapability(TypedDict):
    format: str
    channels: int
    input_sample_rate: int


class Capabilities(TypedDict):
    service_instance_id: str
    protocol_version: int
    profile: Literal["text", "voice"]
    state: str
    cached_only: bool
    shippable_only: bool
    portfolio_revision: int
    kinds: dict[str, KindCapability]
    probe: dict[str, Any]
    limits: dict[str, int]
    pcm: PcmCapability
    features: dict[str, bool]
    startup_error: dict[str, Any] | None
    requested: dict[str, Any]
    actual: dict[str, Any]


@dataclass(frozen=True)
class ToolOutcome:
    """Server attempt facts and independent evidence of this client's execution.

    ``completed`` means the service received a result, not that a gameplay side
    effect succeeded. ``result_delivered`` means a response was observed to a model
    continuation containing the result, NOT registry receipt or a socket send.
    Rejected attempts can deliver an error result. Unknown outcomes never imply
    side effects were undone.
    A locally executed call missing from the final snapshot has no known model ID.
    """

    model_call_id: str | None
    dispatch_id: str | None
    name: str
    arguments: JsonValue
    result: JsonValue
    outcome: Literal["rejected", "completed", "unknown"]
    result_delivered: bool
    executed_locally: bool = False

    def __post_init__(self) -> None:
        object.__setattr__(self, "arguments", freeze_json(self.arguments))
        object.__setattr__(self, "result", freeze_json(self.result))


@dataclass(frozen=True)
class TextResult:
    state: TerminalState
    text: str
    job_id: str
    turn_id: str | None
    calls: tuple[ToolOutcome, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "calls", tuple(self.calls))