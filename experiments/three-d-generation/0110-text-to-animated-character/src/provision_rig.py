"""Download pinned rig checkpoints to shared storage, not the source checkout."""
import os
from pathlib import Path


def main() -> None:
    from huggingface_hub import snapshot_download

    root = Path(os.environ["LOCALAPPDATA"]) / "EchoCradle/models/skintokens-character"
    snapshot_download(
        "VAST-AI/SkinTokens", revision="79736cad0fd84de384d5eede659b4ebd24effe33",
        local_dir=root, allow_patterns=["experiments/*/*.ckpt"], max_workers=2,
    )
    snapshot_download("Qwen/Qwen3-0.6B", revision="c1899de289a04d12100db370d81485cdf75e47ca",
                      local_dir=root / "models/Qwen3-0.6B",
                      ignore_patterns=["*.bin", "*.safetensors"], max_workers=2)


if __name__ == "__main__":
    main()