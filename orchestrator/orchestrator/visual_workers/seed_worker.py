"""Seed the proven TripoSR adapter inside its isolated GPU interpreter."""
from __future__ import annotations

import random
import runpy
import sys
from pathlib import Path


def main() -> None:
    import numpy as np
    import torch

    seed = int(sys.argv[1])
    worker = Path(sys.argv[2]).resolve()
    random.seed(seed)
    np.random.seed(seed)
    torch.manual_seed(seed)
    torch.cuda.manual_seed_all(seed)
    sys.path.insert(0, str(worker.parent))
    sys.argv = [str(worker), *sys.argv[3:]]
    runpy.run_path(str(worker), run_name="__main__")


if __name__ == "__main__":
    main()