"""Typed errors the orchestrator raises.

Callers (the game, an experiment) should be able to distinguish "this machine
cannot run that" from "the model crashed" from "you asked for something that
does not exist", so each gets its own type.
"""

from __future__ import annotations


class OrchestratorError(Exception):
    """Base class for every orchestrator failure."""


class ModelNotAvailableError(OrchestratorError):
    """No model in the catalog can satisfy the request on this machine.

    This is the *expected* failure on a weak host: the caller should fall back
    to a placeholder rather than retry.
    """


class ModelDownloadError(OrchestratorError):
    """A model could not be fetched or failed its integrity check."""


class TaskFailedError(OrchestratorError):
    """A task ran but the host raised."""

    def __init__(self, task_id: str, reason: str) -> None:
        super().__init__(f"task {task_id} failed: {reason}")
        self.task_id = task_id
        self.reason = reason


class DependencyError(OrchestratorError):
    """A task graph is invalid (cycle, unknown dependency, wrong kind)."""
