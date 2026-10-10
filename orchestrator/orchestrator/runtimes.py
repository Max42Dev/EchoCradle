"""Pinned native runtime deployments, separate from model weights and host code."""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit


@dataclass(frozen=True)
class RuntimeDescriptor:
    """One trusted deployment of a native inference engine."""

    id: str
    version: str
    variant: str
    executable: str
    archives: tuple[str, ...]

    def __post_init__(self) -> None:
        for value in (self.id, self.version, self.variant):
            if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]*", value):
                raise ValueError("Runtime id, version and variant must be safe path components")
            if value.lower() == "latest":
                raise ValueError("Runtime deployments must be pinned, not latest")
        if (not isinstance(self.executable, str)
                or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]*", self.executable)):
            raise ValueError("Runtime executable must be a filename")
        if not self.archives:
            raise ValueError("Runtime must declare at least one archive")
        filenames = set()
        for url in self.archives:
            if not isinstance(url, str):
                raise ValueError("Runtime archive URLs must be strings")
            parsed = urlsplit(url)
            filename = Path(parsed.path).name
            if (parsed.scheme != "https" or not parsed.hostname or parsed.username
                    or parsed.password or parsed.query or parsed.fragment
                    or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]*\.zip", filename)):
                raise ValueError("Runtime archives must be HTTPS ZIP URLs without credentials")
            if filename in filenames:
                raise ValueError("Runtime archive filenames must be unique")
            filenames.add(filename)

    def cache_dir(self, root: Path) -> Path:
        """Isolate incompatible builds and hardware variants in the shared store."""
        return root / self.id / self.version / self.variant


class RuntimeCatalog:
    """Native engines provisioned by host adapters; no downloads happen here."""

    def __init__(self, runtimes: list[RuntimeDescriptor]) -> None:
        self._runtimes = {runtime.id: runtime for runtime in runtimes}
        if len(self._runtimes) != len(runtimes):
            raise ValueError("Duplicate runtime ids")

    def get(self, runtime_id: str) -> RuntimeDescriptor:
        return self._runtimes[runtime_id]

    @classmethod
    def from_json(cls, path: str | Path) -> RuntimeCatalog:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
        if not isinstance(data, dict) or set(data) != {"runtimes"}:
            raise ValueError("Runtime config must contain only a runtimes list")
        if not isinstance(data["runtimes"], list):
            raise ValueError("runtimes must be a list")
        runtimes = []
        for entry in data["runtimes"]:
            if (not isinstance(entry, dict)
                    or set(entry) != {"id", "version", "variant", "executable", "archives"}
                    or not isinstance(entry["archives"], list)):
                raise ValueError("Invalid runtime descriptor fields")
            runtimes.append(RuntimeDescriptor(**{**entry, "archives": tuple(entry["archives"])}))
        return cls(runtimes)

    @classmethod
    def default(cls, path: str | Path | None = None) -> RuntimeCatalog:
        """Explicit path, then environment override, then the packaged defaults."""
        selected = path or os.environ.get("ECHOCRADLE_RUNTIME_CONFIG")
        return cls.from_json(selected or Path(__file__).with_name("runtimes.json"))