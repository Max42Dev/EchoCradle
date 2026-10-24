"""Explicit online provisioning into the orchestrator's shared store."""

from __future__ import annotations

import json
import os
from pathlib import Path


def main() -> None:
    from huggingface_hub import HfApi, snapshot_download

    root = Path(os.environ["LOCALAPPDATA"]) / "EchoCradle" / "models"
    selections = [
        ("stable-diffusion-v1-5/stable-diffusion-v1-5", "sd15-character",
         ["model_index.json", "scheduler/*", "tokenizer/*", "*/config.json",
          "unet/diffusion_pytorch_model.fp16.safetensors",
          "vae/diffusion_pytorch_model.fp16.safetensors",
          "text_encoder/model.fp16.safetensors"]),
        ("stabilityai/TripoSR", "triposr-character", ["config.yaml", "model.ckpt"]),
    ]
    records = []
    for repo, name, patterns in selections:
        revision = HfApi().model_info(repo).sha
        target = root / name
        snapshot_download(repo, revision=revision, local_dir=target,
                          allow_patterns=patterns, max_workers=2)
        records.append({"repository": repo, "revision": revision, "path": str(target)})
    print(json.dumps(records, indent=2))


if __name__ == "__main__":
    main()