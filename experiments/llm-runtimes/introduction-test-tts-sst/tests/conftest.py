"""Make ``src/`` and the orchestrator package importable for these tests."""

from __future__ import annotations

import sys
from pathlib import Path

_SRC = Path(__file__).resolve().parents[1] / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

# The orchestrator lives at the repo root. Add its *package parent*, not the repo
# root: the repo root holds a folder also called "orchestrator", which would
# shadow the package as an empty namespace package.
_ORCHESTRATOR_PARENT = Path(__file__).resolve().parents[4] / "orchestrator"
if _ORCHESTRATOR_PARENT.is_dir() and str(_ORCHESTRATOR_PARENT) not in sys.path:
    sys.path.insert(0, str(_ORCHESTRATOR_PARENT))
