"""Cheap foreground/framing gate, NOT an anatomical or semantic pose classifier."""
from __future__ import annotations

import argparse
import json
from pathlib import Path


def assess(mask: object) -> dict:
    import numpy as np

    foreground = np.asarray(mask) > 127
    rows, columns = np.where(foreground)
    if not len(rows):
        return {"pass": False, "issues": ["empty_foreground"]}
    height, width = foreground.shape
    bounds = [int(columns.min()), int(rows.min()), int(columns.max()), int(rows.max())]
    occupancy = float(foreground.mean())
    margin = min(bounds[0] / width, bounds[1] / height,
                 (width - 1 - bounds[2]) / width, (height - 1 - bounds[3]) / height)
    issues = []
    if margin < 0.01:
        issues.append("foreground_touches_frame")
    if not 0.06 <= occupancy <= 0.65:
        issues.append("foreground_occupancy")
    return {"pass": not issues, "issues": issues, "bounds": bounds,
            "occupancy": occupancy, "minimum_margin": margin,
            "anatomy_pose_identity": "not_verified"}


def main() -> None:
    from PIL import Image
    from rembg import new_session, remove

    parser = argparse.ArgumentParser()
    parser.add_argument("--input", type=Path, required=True)
    parser.add_argument("--report", type=Path, required=True)
    args = parser.parse_args()
    rgba = remove(Image.open(args.input).convert("RGBA"), session=new_session("u2netp"))
    rgba.save(args.report.parent / "foreground.png")
    rgba.getchannel("A").save(args.report.parent / "foreground_mask.png")
    report = assess(rgba.getchannel("A"))
    args.report.write_text(json.dumps(report, indent=2))
    print(json.dumps(report))


if __name__ == "__main__":
    main()