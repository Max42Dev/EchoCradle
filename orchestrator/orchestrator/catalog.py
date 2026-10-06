"""The model catalog: a declarative list of open-weight models we know about.

A descriptor is *data*, not code. The planner joins it with a probe report to
decide what this machine can actually run. Nothing here downloads anything.

Licence and ``shippable`` are first-class: the design doc requires the planner
to refuse non-commercial weights in a shipped build (exp. 0103/0104).
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any


class Modality(str, Enum):
    """What a model produces. Mirrors :class:`orchestrator.tasks.TaskKind`."""

    TEXT = "text"
    TTS = "tts"
    STT = "stt"


@dataclass(frozen=True)
class ModelDescriptor:
    """Everything the orchestrator needs to know about one model.

    ``vram_gb`` is the *peak* footprint the model holds while running a request,
    not its download size (exp. 0105). ``ram_gb`` is the equivalent for CPU-only
    models. ``tier`` is the lowest machine tier that can run it comfortably.
    """

    id: str
    modality: Modality
    family: str
    tier: int
    quality: float
    license: str
    shippable: bool
    url: str
    size_gb: float
    vram_gb: float = 0.0
    ram_gb: float = 0.0
    #: Files that must exist after extraction, relative to the model directory.
    #: Empty means "the download is a single file" (see ``single_file``).
    files: tuple[str, ...] = ()
    #: True when ``url`` points at one file rather than an archive.
    single_file: bool = False
    #: Extra host-specific parameters (voice id, sample rate, ...).
    params: dict[str, Any] = field(default_factory=dict)
    notes: str = ""

    @property
    def is_cpu_only(self) -> bool:
        """True when the model never needs VRAM (speech, per exp. 0109)."""
        return self.vram_gb <= 0.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "modality": self.modality.value,
            "family": self.family,
            "tier": self.tier,
            "quality": self.quality,
            "license": self.license,
            "shippable": self.shippable,
            "url": self.url,
            "size_gb": self.size_gb,
            "vram_gb": self.vram_gb,
            "ram_gb": self.ram_gb,
            "files": list(self.files),
            "single_file": self.single_file,
            "params": self.params,
            "notes": self.notes,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> ModelDescriptor:
        return cls(
            id=data["id"],
            modality=Modality(data["modality"]),
            family=data["family"],
            tier=int(data["tier"]),
            quality=float(data["quality"]),
            license=data["license"],
            shippable=bool(data["shippable"]),
            url=data["url"],
            size_gb=float(data["size_gb"]),
            vram_gb=float(data.get("vram_gb", 0.0)),
            ram_gb=float(data.get("ram_gb", 0.0)),
            files=tuple(data.get("files", ())),
            single_file=bool(data.get("single_file", False)),
            params=dict(data.get("params", {})),
            notes=data.get("notes", ""),
        )


class Catalog:
    """An immutable, queryable set of :class:`ModelDescriptor`."""

    def __init__(self, models: list[ModelDescriptor]) -> None:
        self._models = {m.id: m for m in models}

    def __len__(self) -> int:
        return len(self._models)

    def __iter__(self):
        return iter(self._models.values())

    def get(self, model_id: str) -> ModelDescriptor:
        try:
            return self._models[model_id]
        except KeyError as exc:
            raise KeyError(f"unknown model id: {model_id!r}") from exc

    def by_modality(self, modality: Modality) -> list[ModelDescriptor]:
        """Return models of one modality, best quality first."""
        return sorted(
            (m for m in self._models.values() if m.modality is modality),
            key=lambda m: (-m.quality, m.tier),
        )

    def add(self, model: ModelDescriptor) -> None:
        self._models[model.id] = model

    @classmethod
    def from_json(cls, path: str | Path) -> Catalog:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
        return cls([ModelDescriptor.from_dict(m) for m in data["models"]])

    @classmethod
    def default(cls) -> Catalog:
        """Load the catalog shipped with the package."""
        return cls.from_json(Path(__file__).with_name("catalog.json"))
