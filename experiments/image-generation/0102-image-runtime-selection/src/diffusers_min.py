#!/usr/bin/env python3
"""Minimal in-process diffusers txt2img with EXPLICIT VRAM load/unload.

!!! REQUIRES torch (CUDA build) + diffusers + accelerate + Pillow. !!!
None of those are installed on the reference box yet (see ../README.md Setup),
so a normal run exits with a clear message instead of an ImportError traceback.
Use --dry-run to print the plan and the GPU-memory math right now, with no
dependencies at all.

This is the reference implementation for option A in experiment 0102: run the
diffusion pipeline *inside* the orchestrator process so the scheduler owns
VRAM directly and can load/unload on demand.

GPU memory math (fp16, batch 1) -- the numbers behind the README table:
  SD 1.5 :  UNet ~0.86 G params -> ~1.7 GB
            CLIP ~0.12 G         -> ~0.25 GB
            VAE  ~0.08 G         -> ~0.16 GB
            weights total        ~2.1 GB
            + activations at 512^2 batch 1 ~0.5-1.5 GB     => ~3-5 GB peak
  SDXL   :  UNet ~2.6 G          -> ~5.2 GB
            2x CLIP ~1.4 GB ; VAE ~0.3 GB ; weights ~6.9 GB
            + activations ~1-2 GB                           => ~8-10 GB peak
`enable_model_cpu_offload()` keeps only the component currently in use on the
GPU, so peak falls to roughly the largest single component (SDXL UNet ~5.2 GB)
plus activations. `enable_sequential_cpu_offload()` pushes peak under ~2 GB but
is dramatically slower (weights shuttle per submodule).

Why the model can be shared with ComfyUI (disk saver): `--from-single-file`
loads the *same* .safetensors checkpoint ComfyUI uses, so you do not keep a
second copy of the weights in diffusers format.

Usage (PowerShell):
  python diffusers_min.py --dry-run
  python diffusers_min.py --prompt "gilded iron axe icon" --seed 1234 `
      --width 512 --height 512 --out out/axe_sd15_512.png
  python diffusers_min.py --model "C:\\models\\sd_xl_base_1.0.safetensors" `
      --from-single-file --offload model --width 1024 --height 1024
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

# Never hardcode model paths in the orchestrator (see python.instructions.md):
# this is only a default, overridable by CLI flag or environment variable.
DEFAULT_MODEL = os.environ.get("ECHOCRADLE_SD_MODEL", "runwayml/stable-diffusion-v1-5")

# Published fp16 weight sizes, GiB, used for the --dry-run memory estimate.
WEIGHT_GIB = {"sd15": 2.1, "sdxl": 6.9}
ACTIVATION_GIB = {"sd15": 1.5, "sdxl": 2.0}


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Minimal diffusers txt2img with VRAM unload.")
    parser.add_argument("--model", default=DEFAULT_MODEL, help="HF repo id or local checkpoint path")
    parser.add_argument("--prompt", default="a gilded iron axe icon, game item, plain background")
    parser.add_argument("--negative", default="blurry, lowres, watermark, text")
    parser.add_argument("--steps", type=int, default=20)
    parser.add_argument("--guidance", type=float, default=7.5)
    parser.add_argument("--seed", type=int, default=1234)
    parser.add_argument("--width", type=int, default=512)
    parser.add_argument("--height", type=int, default=512)
    parser.add_argument("--out", type=Path, default=Path("out/diffusers_image.png"))
    parser.add_argument(
        "--offload",
        choices=("none", "model", "sequential"),
        default="model",
        help="none = whole pipeline on cuda; model = enable_model_cpu_offload; "
        "sequential = enable_sequential_cpu_offload (slowest, least VRAM)",
    )
    parser.add_argument(
        "--from-single-file",
        action="store_true",
        help="Treat --model as a single-file .safetensors (share with ComfyUI)",
    )
    parser.add_argument("--local-files-only", action="store_true", help="Never hit the network")
    parser.add_argument("--dry-run", action="store_true", help="Print the plan; import nothing")
    return parser.parse_args(argv)


def print_plan(args: argparse.Namespace) -> None:
    """Print the intended run and a coarse VRAM estimate without importing torch."""
    family = "sdxl" if max(args.width, args.height) >= 768 else "sd15"
    weights = WEIGHT_GIB[family]
    activation = ACTIVATION_GIB[family]
    offload_note = {
        "none": f"~{weights + activation:.1f} GiB (everything resident)",
        "model": f"~{weights * 0.75 + activation:.1f} GiB (largest component resident at a time)",
        "sequential": "~1.5-2.5 GiB (submodule-by-submodule, very slow)",
    }[args.offload]
    print("diffusers plan (dry run, nothing imported):")
    print(f"  model      : {args.model}  ({'single file' if args.from_single_file else 'HF repo'})")
    print(f"  size       : {args.width}x{args.height}  steps={args.steps}  seed={args.seed}")
    print(f"  offload    : {args.offload}  -> estimated peak VRAM {offload_note}")
    print(f"  fp16 weights ~{weights:.1f} GiB ; activations ~{activation:.1f} GiB")
    print(f"  output     : {args.out}")
    print("  unload     : del pipe; gc.collect(); torch.cuda.empty_cache()")
    print("  NOTE: no torch/diffusers on this box yet -- see ../README.md 'How to run'")


def run(args: argparse.Namespace) -> int:
    # Imported lazily so --dry-run and a missing dependency both behave nicely.
    try:
        import gc

        import torch
        from diffusers import StableDiffusionPipeline
    except ImportError as exc:
        print(
            f"diffusers_min: torch + diffusers are required ({exc}).\n"
            "Install with:\n"
            "  pip install torch --index-url https://download.pytorch.org/whl/cu126\n"
            "  pip install diffusers transformers accelerate safetensors pillow\n"
            "Or run 'python diffusers_min.py --dry-run' to see the plan only.",
            file=sys.stderr,
        )
        return 2

    if not torch.cuda.is_available():
        print("diffusers_min: CUDA is not available to torch.", file=sys.stderr)
        return 2

    torch.cuda.reset_peak_memory_stats()

    load_kwargs: dict[str, object] = {
        "torch_dtype": torch.float16,
        "safety_checker": None,
        "requires_safety_checker": False,
        "local_files_only": args.local_files_only,
    }
    if args.from_single_file:
        pipe = StableDiffusionPipeline.from_single_file(args.model, **load_kwargs)
    else:
        pipe = StableDiffusionPipeline.from_pretrained(args.model, **load_kwargs)

    # Device placement: this is the whole point of running in-process.
    if args.offload == "model":
        pipe.enable_model_cpu_offload()  # only the active component sits on the GPU
        generator_device = "cpu"
    elif args.offload == "sequential":
        pipe.enable_sequential_cpu_offload()  # least VRAM, very slow
        generator_device = "cpu"
    else:
        pipe = pipe.to("cuda")
        generator_device = "cuda"

    pipe.enable_vae_slicing()  # small win for multi-image batches, no cost at batch 1

    # Determinism: seed a generator on a device that matches the run.
    generator = torch.Generator(device=generator_device).manual_seed(args.seed)

    image = pipe(
        prompt=args.prompt,
        negative_prompt=args.negative,
        num_inference_steps=args.steps,
        guidance_scale=args.guidance,
        width=args.width,
        height=args.height,
        generator=generator,
    ).images[0]

    args.out.parent.mkdir(parents=True, exist_ok=True)
    image.save(args.out)

    peak = torch.cuda.max_memory_allocated() / 1024**3
    reserved = torch.cuda.memory_reserved() / 1024**3
    print(f"saved {args.out}")
    print(f"peak VRAM allocated {peak:.2f} GiB (reserved {reserved:.2f} GiB)")

    # --- explicit VRAM release: the reason option A wins on scheduling -------------
    del pipe
    gc.collect()
    torch.cuda.empty_cache()  # returns cached blocks to the driver, keeps CUDA context
    torch.cuda.reset_peak_memory_stats()
    after = torch.cuda.memory_allocated() / 1024**3
    after_reserved = torch.cuda.memory_reserved() / 1024**3
    print(f"after unload allocated {after:.2f} GiB (reserved {after_reserved:.2f} GiB)")
    print("note: ~0.3-0.6 GiB CUDA context remains and is not reclaimable")
    return 0


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    if args.dry_run:
        print_plan(args)
        return 0
    return run(args)


if __name__ == "__main__":
    raise SystemExit(main())