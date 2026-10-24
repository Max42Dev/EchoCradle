"""Offline Diffusers reference generation. Run in an isolated CUDA environment."""

from __future__ import annotations

import argparse
import json
from pathlib import Path


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--spec", type=Path, required=True)
    parser.add_argument("--model", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    spec = json.loads(args.spec.read_text(encoding="utf-8"))
    import torch
    from diffusers import DiffusionPipeline

    if not args.model.is_dir() or not torch.cuda.is_available():
        raise RuntimeError("A provisioned local Diffusers directory and CUDA are required")
    torch.cuda.reset_peak_memory_stats()
    pipeline = DiffusionPipeline.from_pretrained(
        str(args.model), torch_dtype=torch.float16, local_files_only=True, variant="fp16",
        safety_checker=None, feature_extractor=None, requires_safety_checker=False,
    )
    pipeline.to("cuda")
    generator = torch.Generator(device="cuda").manual_seed(spec["seed"])
    image = pipeline(
        prompt=spec["prompt"], negative_prompt=spec["negative_prompt"],
        width=spec["width"], height=spec["height"], num_inference_steps=spec["steps"],
        guidance_scale=spec["guidance"], generator=generator,
    ).images[0]
    image.save(args.output)
    print(json.dumps({
        "cuda_peak_allocated_bytes": torch.cuda.max_memory_allocated(),
        "cuda_peak_reserved_bytes": torch.cuda.max_memory_reserved(),
        "torch": torch.__version__, "seed": spec["seed"],
    }))


if __name__ == "__main__":
    main()