"""Runtime hosts: the adapters that actually run a modality.

A host wraps a *pipeline*, not necessarily a single model (design doc). It is
responsible for loading weights, running a request, and releasing resources.
Hosts are deliberately small and swappable so the orchestrator stays generic.
"""

from __future__ import annotations

from typing import Any, Iterator, Protocol, runtime_checkable

from orchestrator.catalog import ModelDescriptor
from orchestrator.store import InstalledModel


@runtime_checkable
class RuntimeHost(Protocol):
    """The contract every modality host implements."""

    modality: str

    def load(self, model: InstalledModel) -> None:
        """Make the model ready to serve. May start a subprocess."""

    def unload(self) -> None:
        """Release the model and any resources it holds."""

    @property
    def loaded_model_id(self) -> str | None:
        """The id of the currently loaded model, or None."""

    def run(self, spec: dict[str, Any]) -> Any:
        """Run one request and return its result."""


class HostError(RuntimeError):
    """Raised by a host when a request cannot be completed."""


def describe(model: ModelDescriptor) -> str:
    """One-line human summary of a model, used in logs and the CLI."""
    return (
        f"{model.id} ({model.family}, tier {model.tier}, "
        f"quality {model.quality:.2f}, {model.license})"
    )


def iter_text_chunks(text: str, size: int = 24) -> Iterator[str]:
    """Split a finished string into pseudo-token chunks.

    Used by hosts that cannot stream natively so callers still get the same
    incremental interface. This is a *transport* detail, not a model.
    """
    for i in range(0, len(text), size):
        yield text[i : i + size]
