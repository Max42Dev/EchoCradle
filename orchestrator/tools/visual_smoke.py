"""Run the cached 0110 pipeline through the real owned-service Python client."""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import time
import traceback
from datetime import datetime, timezone
from pathlib import Path

from orchestrator.client import ServiceClient


async def execute(config: Path, spec_path: Path, output: Path) -> None:
    spec = json.loads(spec_path.read_text(encoding="utf-8"))
    output.mkdir(parents=True, exist_ok=False)
    os.environ["ECHOCRADLE_VISUAL_DEBUG_DIR"] = str(output / "debug")
    (output / "step_0_input.json").write_text(json.dumps(spec, indent=2), encoding="utf-8")
    manifest = {"started": datetime.now(timezone.utc).isoformat(), "stages": [],
                "config": str(config), "status": "running", "real_inference": True}
    started = time.monotonic()

    def save() -> None:
        (output / "manifest.json").write_text(
            json.dumps(manifest, indent=2), encoding="utf-8"
        )

    save()
    try:
        print(f"RESULTS {output}", flush=True)
        with ServiceClient.start_owned(profile="visual", visual_config=config) as client:
            (output / "capabilities.txt").write_text(
                repr(client.capabilities()), encoding="utf-8"
            )
            previous = None
            for index, (kind, filename) in enumerate([
                ("image", "step_1_reference.png"),
                ("image_to_3d", "step_2_mesh.glb"),
                ("animate_3d", "step_3_animated.glb"),
            ], 1):
                tick = time.monotonic()
                print(f"START {index} {kind}", flush=True)
                if kind == "image":
                    result = await client.generate_image(
                        spec["prompt"], seed=spec["seed"],
                        negative_prompt=spec.get("negative_prompt", ""),
                        deadline_ms=1_800_000,
                    )
                elif kind == "image_to_3d":
                    result = await client.generate_3d(
                        previous, seed=spec["seed"], deadline_ms=1_800_000
                    )
                else:
                    result = await client.animate_3d(
                        previous, seed=spec["seed"], deadline_ms=1_800_000
                    )
                (output / filename).write_bytes(result.payload)
                record = {"kind": kind, "seconds": round(time.monotonic() - tick, 3),
                          "file": filename, "bytes": len(result.payload),
                          "sha256": hashlib.sha256(result.payload).hexdigest(),
                          "artifact_id": result.artifact_id}
                manifest["stages"].append(record)
                save()
                print(f"PASS {kind} {record['seconds']}s {record['bytes']} bytes", flush=True)
                previous = result
        manifest["status"] = "passed"
    except BaseException as exc:
        manifest["status"] = "failed"
        manifest["error_type"] = type(exc).__name__
        (output / "failure.txt").write_text(traceback.format_exc(), encoding="utf-8")
        raise
    finally:
        manifest["seconds"] = round(time.monotonic() - started, 3)
        save()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", required=True, type=Path)
    parser.add_argument("--spec", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    asyncio.run(execute(args.config.resolve(), args.spec.resolve(), args.output.resolve()))


if __name__ == "__main__":
    main()