"""Native Windows TripoSR adapter: CPU skimage extraction, GPU neural inference.

The torchmcubes-compatible shim is deliberate and recorded, not an upstream claim.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
import types
from pathlib import Path


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--checkout", type=Path, required=True)
    parser.add_argument("--model", type=Path, required=True)
    parser.add_argument("--input", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    import numpy as np
    import torch
    from PIL import Image
    from skimage.measure import marching_cubes

    shim = types.ModuleType("torchmcubes")

    def extract(volume: torch.Tensor, level: float) -> tuple[torch.Tensor, torch.Tensor]:
        vertices, faces, _, _ = marching_cubes(volume.detach().cpu().numpy(), level)
        # Upstream flips torchmcubes ZYX output back to XYZ. Supply ZYX here.
        return (torch.tensor(vertices[:, [2, 1, 0]].copy(), dtype=torch.float32),
                torch.tensor(faces.copy(), dtype=torch.long))

    shim.marching_cubes = extract
    sys.modules["torchmcubes"] = shim
    sys.path.insert(0, str(args.checkout))
    from tsr.system import TSR
    from tsr.utils import resize_foreground
    from rembg import new_session, remove

    start = time.monotonic()
    torch.cuda.reset_peak_memory_stats()
    model = TSR.from_pretrained(str(args.model), config_name="config.yaml", weight_name="model.ckpt")
    model.renderer.set_chunk_size(4096)
    model.to("cuda")
    image = remove(Image.open(args.input).convert("RGBA"), session=new_session("u2netp"))
    args.output.parent.mkdir(parents=True, exist_ok=True)
    image.save(args.output.parent / "foreground.png")
    image = resize_foreground(image, 0.85)
    rgba = np.asarray(image).astype(np.float32) / 255
    rgb = rgba[:, :, :3] * rgba[:, :, 3:4] + (1 - rgba[:, :, 3:4]) * 0.5
    image = Image.fromarray((rgb * 255).astype(np.uint8))
    args.output.parent.mkdir(parents=True, exist_ok=True)
    image.save(args.output.parent / "reconstruction_input.png")
    with torch.no_grad():
        scene = model([image], device="cuda")
        meshes = model.extract_mesh(scene, True, resolution=192)
    meshes[0].export(args.output)
    report = {"seconds": time.monotonic() - start, "vertices": len(meshes[0].vertices),
              "triangles": len(meshes[0].faces), "appearance": "neural_vertex_colors",
              "adapter": "skimage_CPU_marching_cubes", "device": "cuda",
              "peak_allocated_bytes": torch.cuda.max_memory_allocated()}
    (args.output.parent / "shape_metrics.json").write_text(json.dumps(report, indent=2))
    print(json.dumps(report))


if __name__ == "__main__":
    main()