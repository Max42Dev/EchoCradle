"""The one place that decides where the orchestrator keeps its files.

**The model store location is a constant.** Every experiment and the game must
resolve to the *same* directory so weights are downloaded once and reused. Do
not let callers invent their own paths; import :func:`model_store_dir` instead.

Layout (Windows)::

    %LOCALAPPDATA%\\EchoCradle\\
        models\\        <- downloaded weights, shared by everything
        cache\\         <- content-addressed generated artifacts
        logs\\

On other platforms the same layout is used under the XDG data directory, so the
code stays portable even though Windows is the shipping target.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

APP_DIR_NAME = "EchoCradle"

#: Environment variable that overrides the store root (tests, CI, power users).
ENV_ROOT = "ECHOCRADLE_HOME"


def app_dir() -> Path:
    """Return the per-user application directory, creating it if needed."""
    override = os.environ.get(ENV_ROOT)
    if override:
        root = Path(override)
    elif sys.platform == "win32":
        base = os.environ.get("LOCALAPPDATA") or (Path.home() / "AppData" / "Local")
        root = Path(base) / APP_DIR_NAME
    else:
        base = os.environ.get("XDG_DATA_HOME") or (Path.home() / ".local" / "share")
        root = Path(base) / APP_DIR_NAME
    root.mkdir(parents=True, exist_ok=True)
    return root


def model_store_dir() -> Path:
    """Return the shared model store directory (constant across experiments)."""
    path = app_dir() / "models"
    path.mkdir(parents=True, exist_ok=True)
    return path


def cache_dir() -> Path:
    """Return the content-addressed artifact cache directory."""
    path = app_dir() / "cache"
    path.mkdir(parents=True, exist_ok=True)
    return path


def logs_dir() -> Path:
    """Return the log directory."""
    path = app_dir() / "logs"
    path.mkdir(parents=True, exist_ok=True)
    return path
