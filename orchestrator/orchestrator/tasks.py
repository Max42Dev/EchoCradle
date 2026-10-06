"""The task model: requests, priorities, states and artifacts.

A request is submitted, returns immediately with an id, and is later resolved to
one or more :class:`Artifact` paths. Nothing here blocks.
"""

from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any


class TaskKind(str, Enum):
    """The modality of a task. Extend as hosts are added."""

    TEXT = "text"
    TTS = "tts"
    STT = "stt"


class Priority(int, Enum):
    """Priority bands, highest first. Bands avoid starvation (design doc)."""

    INTERACTIVE = 0
    FOREGROUND = 1
    BACKGROUND = 2
    IDLE = 3


class TaskState(str, Enum):
    QUEUED = "queued"
    BLOCKED = "blocked"
    LOADING_MODEL = "loading-model"
    RUNNING = "running"
    DONE = "done"
    FAILED = "failed"
    CANCELLED = "cancelled"


@dataclass(frozen=True)
class ContentRequest:
    """A request for content. ``spec`` is modality-specific and free-form."""

    kind: TaskKind
    spec: dict[str, Any] = field(default_factory=dict)
    priority: Priority = Priority.FOREGROUND
    depends_on: tuple[str, ...] = ()
    seed: int | None = None
    #: Pin a specific catalog model instead of letting the planner choose.
    model_id: str | None = None


@dataclass(frozen=True)
class Artifact:
    """A file produced by a task."""

    kind: str
    path: Path
    meta: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {"kind": self.kind, "path": str(self.path), "meta": self.meta}


@dataclass
class TaskStatus:
    """The observable state of a task."""

    id: str
    kind: TaskKind
    state: TaskState
    priority: Priority
    progress: float = 0.0
    model_id: str | None = None
    artifacts: list[Artifact] = field(default_factory=list)
    error: str | None = None
    created_at: float = field(default_factory=time.time)
    started_at: float | None = None
    finished_at: float | None = None

    @property
    def is_terminal(self) -> bool:
        return self.state in (TaskState.DONE, TaskState.FAILED, TaskState.CANCELLED)

    @property
    def duration_s(self) -> float | None:
        if self.started_at is None:
            return None
        end = self.finished_at if self.finished_at is not None else time.time()
        return end - self.started_at

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "kind": self.kind.value,
            "state": self.state.value,
            "priority": self.priority.name,
            "progress": round(self.progress, 3),
            "model_id": self.model_id,
            "artifacts": [a.to_dict() for a in self.artifacts],
            "error": self.error,
            "duration_s": None if self.duration_s is None else round(self.duration_s, 3),
        }


def new_task_id() -> str:
    """A short, sortable-ish unique id."""
    return uuid.uuid4().hex[:12]
