"""Provision the alternate stylized reference model after two failed SD1.5 poses."""
import os
from pathlib import Path


def main() -> None:
    from huggingface_hub import snapshot_download

    snapshot_download(
        "Lykon/dreamshaper-8", revision="a7e52b98680b1ba8ff7bce97c7f9f2e2e5337917",
        local_dir=Path(os.environ["LOCALAPPDATA"]) / "EchoCradle/models/dreamshaper-character",
        allow_patterns=["model_index.json", "scheduler/*", "tokenizer/*", "*/config.json",
                        "unet/diffusion_pytorch_model.fp16.safetensors",
                        "vae/diffusion_pytorch_model.fp16.safetensors",
                        "text_encoder/model.fp16.safetensors"], max_workers=2,
    )


if __name__ == "__main__":
    main()