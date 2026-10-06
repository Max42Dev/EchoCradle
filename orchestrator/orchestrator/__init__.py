"""EchoCradle Model Orchestrator.

A single local service that turns *content requests* into *finished artifacts*
without the caller ever blocking. It owns:

* a **catalog** of open-weight models (what exists, what it costs, what it fits),
* a **probe** of the host machine (VRAM / RAM / disk -> capability tier),
* a **store** for downloaded weights (one constant location, shared by every
  experiment and the game),
* a **planner** that picks the best model for a request on this machine,
* a **scheduler** that runs tasks by priority and dependency,
* pluggable **hosts** that actually run a modality (text, speech, ...).

The public entry point is :class:`orchestrator.orchestrator.ModelOrchestrator`.

Design notes live in ``ideas/model-orchestrator/``.
"""

from orchestrator.catalog import Catalog, ModelDescriptor, Modality
from orchestrator.conversation import Conversation
from orchestrator.errors import (
    ModelNotAvailableError,
    OrchestratorError,
    TaskFailedError,
)
from orchestrator.hosts.speech import (
    AudioPlayer,
    AudioRecorder,
    ContinuousRecorder,
    pick_input_device,
    read_wav,
    write_wav,
)
from orchestrator.orchestrator import ModelOrchestrator
from orchestrator.probe import ProbeReport, probe
from orchestrator.store import ModelStore
from orchestrator.tasks import (
    Artifact,
    ContentRequest,
    Priority,
    TaskKind,
    TaskStatus,
)
from orchestrator.tools import JsonConfigTool, ToolCall, ToolRegistry, ToolSpec

__all__ = [
    "Artifact",
    "AudioPlayer",
    "AudioRecorder",
    "Catalog",
    "ContentRequest",
    "ContinuousRecorder",
    "Conversation",
    "JsonConfigTool",
    "ModelDescriptor",
    "ModelNotAvailableError",
    "ModelOrchestrator",
    "ModelStore",
    "Modality",
    "OrchestratorError",
    "Priority",
    "ProbeReport",
    "TaskFailedError",
    "TaskKind",
    "TaskStatus",
    "ToolCall",
    "ToolRegistry",
    "ToolSpec",
    "pick_input_device",
    "probe",
    "read_wav",
    "write_wav",
]

__version__ = "0.1.0"
