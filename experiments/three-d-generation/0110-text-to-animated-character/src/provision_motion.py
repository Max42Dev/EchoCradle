"""Explicit research-only MoMask provisioning in the shared model store."""
from __future__ import annotations

import hashlib
import json
import os
import urllib.request
import zipfile
from pathlib import Path


def main() -> None:
    import gdown

    root = Path(os.environ["LOCALAPPDATA"]) / "EchoCradle/models/momask-character"
    root.mkdir(parents=True, exist_ok=True)
    archive = root / "humanml3d_models.zip"
    if not (root / "t2m").exists():
        gdown.download(id="1vXS7SHJBgWPt59wupQ5UUzhFObrnGkQ0", output=str(archive))
        with zipfile.ZipFile(archive) as bundle:
            for info in bundle.infolist():
                target = (root / "t2m" / info.filename).resolve()
                if not target.is_relative_to(root.resolve()) or info.file_size > 100_000_000:
                    raise ValueError("Unexpected checkpoint archive entry")
            bundle.extractall(root / "t2m")
        archive.unlink()
    clip = root / "ViT-B-32.pt"
    expected = "40d365715913c9da98579312b702a82c18be219cc2a73407c4526f58eba950af"
    if not clip.exists():
        urllib.request.urlretrieve(
            f"https://openaipublic.azureedge.net/clip/models/{expected}/ViT-B-32.pt", clip,
        )
    if hashlib.sha256(clip.read_bytes()).hexdigest() != expected:
        raise ValueError("CLIP checkpoint hash mismatch")
    inventory = [{"file": str(p.relative_to(root)), "bytes": p.stat().st_size,
                  "sha256": hashlib.sha256(p.read_bytes()).hexdigest()}
                 for p in root.rglob("*") if p.is_file()]
    (root / "provisioning.json").write_text(json.dumps(inventory, indent=2))
    print(json.dumps({"store": str(root), "files": len(inventory)}))


if __name__ == "__main__":
    main()