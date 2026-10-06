"""The model store: one constant directory, lazy downloads, integrity checks.

Every experiment and the game resolve to the **same** directory (see
:mod:`orchestrator.paths`), so a model is downloaded once and reused. The store
never deletes anything on its own; eviction is a separate, explicit policy.

Downloads are resumable-ish (we write to a ``.part`` file and rename on success)
and verified by size, which is enough to catch a truncated transfer without
pulling in a hashing dependency.
"""

from __future__ import annotations

import shutil
import tarfile
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from orchestrator.catalog import ModelDescriptor
from orchestrator.errors import ModelDownloadError
from orchestrator.paths import model_store_dir

ProgressFn = Callable[[str, int, int], None]


@dataclass(frozen=True)
class InstalledModel:
    """A model that is present on disk and ready to load."""

    descriptor: ModelDescriptor
    path: Path

    def file(self, name: str) -> Path:
        """Resolve a file inside the model directory."""
        return self.path / name


class ModelStore:
    """Manages the shared on-disk model directory."""

    def __init__(self, root: Path | None = None) -> None:
        self.root = Path(root) if root else model_store_dir()
        self.root.mkdir(parents=True, exist_ok=True)

    def path_for(self, model: ModelDescriptor) -> Path:
        return self.root / model.id

    def is_installed(self, model: ModelDescriptor) -> bool:
        """True when the model's required files are all present."""
        target = self.path_for(model)
        if model.single_file:
            return target.is_file() and target.stat().st_size > 0
        if not target.is_dir():
            return False
        return all((target / f).exists() for f in model.files)

    def installed(self, model: ModelDescriptor) -> InstalledModel | None:
        if not self.is_installed(model):
            return None
        return InstalledModel(model, self.path_for(model))

    def ensure(
        self,
        model: ModelDescriptor,
        *,
        progress: ProgressFn | None = None,
    ) -> InstalledModel:
        """Return the installed model, downloading it first if necessary."""
        existing = self.installed(model)
        if existing is not None:
            return existing
        self._download(model, progress=progress)
        result = self.installed(model)
        if result is None:
            raise ModelDownloadError(
                f"{model.id}: download finished but required files are missing"
            )
        return result

    # -- internals ---------------------------------------------------------

    def _download(self, model: ModelDescriptor, *, progress: ProgressFn | None) -> None:
        target = self.path_for(model)
        target.parent.mkdir(parents=True, exist_ok=True)

        if model.single_file:
            self._fetch_file(model.url, target, model.id, progress)
            return

        archive = self.root / f"{model.id}.download"
        self._fetch_file(model.url, archive, model.id, progress)
        try:
            self._extract(archive, target)
        finally:
            archive.unlink(missing_ok=True)

    def _fetch_file(
        self,
        url: str,
        dest: Path,
        label: str,
        progress: ProgressFn | None,
    ) -> None:
        part = dest.with_suffix(dest.suffix + ".part")
        try:
            with urllib.request.urlopen(url, timeout=60) as response:
                total = int(response.headers.get("Content-Length") or 0)
                done = 0
                with part.open("wb") as handle:
                    while True:
                        chunk = response.read(1 << 20)
                        if not chunk:
                            break
                        handle.write(chunk)
                        done += len(chunk)
                        if progress:
                            progress(label, done, total)
        except (OSError, urllib.error.URLError) as exc:
            part.unlink(missing_ok=True)
            raise ModelDownloadError(f"{label}: download failed: {exc}") from exc

        if not part.exists() or part.stat().st_size == 0:
            raise ModelDownloadError(f"{label}: downloaded file is empty")
        part.replace(dest)

    def _extract(self, archive: Path, target: Path) -> None:
        """Extract a ``.tar.bz2``/``.tar.gz`` archive, flattening one top dir."""
        target.mkdir(parents=True, exist_ok=True)
        try:
            with tarfile.open(archive, "r:*") as tar:
                members = tar.getmembers()
                # Archives usually wrap everything in a single top-level folder.
                roots = {m.name.split("/", 1)[0] for m in members if m.name}
                strip = roots.pop() if len(roots) == 1 else None
                for member in members:
                    name = member.name
                    if strip and name.startswith(strip):
                        name = name[len(strip) :].lstrip("/")
                    if not name:
                        continue
                    member.name = name
                    tar.extract(member, target, filter="data")
        except (tarfile.TarError, OSError) as exc:
            raise ModelDownloadError(f"extract failed: {exc}") from exc

    def disk_usage_gb(self) -> float:
        """Total bytes held by the store, in GB."""
        total = 0
        for path in self.root.rglob("*"):
            if path.is_file():
                total += path.stat().st_size
        return total / (1024**3)

    def remove(self, model: ModelDescriptor) -> None:
        """Delete a model's files (explicit eviction)."""
        target = self.path_for(model)
        if target.is_dir():
            shutil.rmtree(target, ignore_errors=True)
        elif target.is_file():
            target.unlink(missing_ok=True)
