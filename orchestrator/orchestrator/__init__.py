"""EchoCradle's synchronous local model hosts and application contracts.

Applications use ``orchestrator.client.ServiceClient.start_owned()`` or
``ServiceClient.attach()`` for the cached-only local service. Import that module
explicitly with the optional ``service`` dependencies installed; importing this
package does not import the client or require its transport/audio dependencies.

``ModelOrchestrator`` is the low-level synchronous in-process host facade for
experiments. Its host calls can block and provision missing assets. The package
provides catalog, probe, store and planner utilities, not a priority/dependency
scheduler. Broader proposals live in ``ideas/model-orchestrator/``.
"""

from orchestrator.catalog import Catalog, ModelDescriptor, Modality
from orchestrator.client_types import (
        Capabilities,
        KindCapability,
        Message,
        PcmCapability,
        TextResult,
        ToolOutcome,
        VisualResult,
)
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
from orchestrator.tools import JsonConfigTool, ToolCall, ToolRegistry, ToolSpec

__all__ = [
    "AudioPlayer",
    "AudioRecorder",
    "Capabilities",
    "Catalog",
    "ContinuousRecorder",
    "Conversation",
    "JsonConfigTool",
    "KindCapability",
    "Message",
    "ModelDescriptor",
    "ModelNotAvailableError",
    "ModelOrchestrator",
    "ModelStore",
    "Modality",
    "OrchestratorError",
    "PcmCapability",
    "ProbeReport",
    "TaskFailedError",
    "TextResult",
    "ToolCall",
    "ToolOutcome",
    "ToolRegistry",
    "ToolSpec",
    "VisualResult",
    "pick_input_device",
    "probe",
    "read_wav",
    "write_wav",
]

__version__ = "0.1.0"
