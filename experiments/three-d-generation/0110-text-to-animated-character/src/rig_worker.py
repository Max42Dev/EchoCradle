"""Seed a pinned, trusted SkinTokens checkout; model output is never executable."""

from __future__ import annotations

import argparse
import random
import runpy
import sys
from pathlib import Path


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--checkout", type=Path, required=True)
    parser.add_argument("--checkpoint", type=Path, required=True)
    parser.add_argument("--input", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--seed", type=int, required=True)
    args = parser.parse_args()
    import numpy as np
    import torch

    random.seed(args.seed)
    np.random.seed(args.seed)
    torch.manual_seed(args.seed)
    torch.cuda.manual_seed_all(args.seed)
    sys.path.insert(0, str(args.checkout))
    sys.argv = [str(args.checkout / "demo.py"), "--input", str(args.input),
                "--output", str(args.output), "--model_ckpt", str(args.checkpoint),
                "--use_transfer"]
    # Full prediction mode: the preceding SF3D mesh does not have an existing skeleton.
    runpy.run_path(str(args.checkout / "demo.py"), run_name="__main__")


if __name__ == "__main__":
    main()