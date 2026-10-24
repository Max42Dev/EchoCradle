"""One-command, provisioned local fallback pipeline; no human review or GUI required."""
from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

from pipeline import digest, run_stage, validate_spec
from verify_result import inspect_glb
from animation_spec import validate_clips
from debug_capture import capture_debug

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent


def execute(config: dict, spec: dict, output: Path) -> dict:
    validate_spec(spec)
    if output.exists() and any(output.iterdir()):
        raise ValueError("Use a fresh output directory")
    output.mkdir(parents=True, exist_ok=True)
    manifest = {"status": "running", "stages": [], "spec": spec, "config": config,
                "human_interventions": 0, "attempts": [], "production_success": False,
                "motion": "not_run",
                "unity": "not_run", "anatomical_quality": "not_verified"}
    start = time.monotonic()
    try:
        for key in ("python", "blender", "image_model", "tripo_model", "tripo_checkout",
                    "rig_checkout", "rig_checkpoint", "hf_home", "foreground_model",
                    "motion_checkout", "motion_models", "bvh_addon", "animation_spec"):
            if not isinstance(config.get(key), str) or not Path(config[key]).exists():
                raise ValueError(f"Missing provisioned local path: {key}")
        clips = validate_clips(json.loads(Path(config["animation_spec"]).read_text()))
        resolution = config.get("texture_resolution", 1024)
        if type(resolution) is not int or resolution not in (512, 1024, 2048):
            raise ValueError("Texture resolution must be 512, 1024 or 2048")
        retries = config.get("reference_attempts", 2)
        timeout = config.get("timeout_seconds", 600)
        if type(retries) is not int or not 1 <= retries <= 3:
            raise ValueError("reference_attempts must be 1..3")
        if type(timeout) is not int or not 1 <= timeout <= 1800:
            raise ValueError("timeout_seconds must be 1..1800")
        if shutil.disk_usage(output).free < 2 * 1024 ** 3:
            raise RuntimeError("Need at least 2 GiB free scratch/reserve")
        for key in ("tripo", "rig", "motion"):
            checkout = config[f"{key}_checkout"]
            revision = subprocess.check_output(
                ["git", "-C", checkout, "rev-parse", "HEAD"], text=True, timeout=10,
            ).strip()
            if revision != config[f"{key}_revision"]:
                raise ValueError(f"Pinned source mismatch: {key}")
            dirty = subprocess.check_output(
                ["git", "-C", checkout, "status", "--porcelain", "--untracked-files=no"],
                text=True, timeout=10,
            ).strip()
            if dirty:
                raise ValueError(f"Tracked source changes: {key}")
        env = dict(os.environ, HF_HUB_OFFLINE="1", TRANSFORMERS_OFFLINE="1",
                   HF_HUB_DISABLE_TELEMETRY="1", HF_HOME=config["hf_home"],
                   U2NET_HOME=config["foreground_model"], DO_NOT_TRACK="1")
        py = config["python"]
        stages = manifest["stages"]
        spec_path = output / "input_spec.json"
        spec_path.write_text(json.dumps(spec, indent=2))
        reference = None
        for index in range(retries):
            attempt = output / f"reference_{index}"
            attempt.mkdir()
            candidate_spec = dict(spec, seed=(spec["seed"] + index) % (2 ** 32))
            spec_path = attempt / "spec.json"
            spec_path.write_text(json.dumps(candidate_spec, indent=2))
            image = attempt / "reference.png"
            run_stage(f"image_{index}", [py, str(HERE / "image_worker.py"), "--spec",
                      str(spec_path), "--model", config["image_model"], "--output", str(image)],
                      HERE, image, output, env, timeout, stages)
            qa = attempt / "framing_qa.json"
            run_stage(f"framing_{index}", [py, str(HERE / "reference_qa.py"), "--input",
                      str(image), "--report", str(qa)], HERE, qa, output, env, timeout, stages)
            report = json.loads(qa.read_text())
            manifest["attempts"].append({"seed": candidate_spec["seed"], "qa": report,
                                        "image_sha256": digest(image)})
            if report["pass"]:
                reference = image
                break
        if reference is None:
            raise RuntimeError("Bounded automatic framing retries exhausted")
        mesh = output / "mesh.glb"
        run_stage("shape", [py, str(HERE / "triposr_worker.py"), "--checkout",
                  config["tripo_checkout"], "--model", config["tripo_model"], "--input",
                  str(reference), "--output", str(mesh)], HERE, mesh, output, env, timeout, stages)
        rigged = output / "rigged.glb"
        run_stage("rig", [py, str(HERE / "rig_worker.py"), "--checkout", config["rig_checkout"],
                  "--checkpoint", config["rig_checkpoint"], "--input", str(mesh), "--output",
                  str(rigged), "--seed", str(candidate_spec["seed"])], Path(config["rig_checkout"]),
                  rigged, output, env, timeout, stages)
        asset = output / f"SK_{spec['id']}.glb"
        prepared_dir = output / "prepared"
        prepared_dir.mkdir()
        run_stage("export_render", [config["blender"], "--background", "--factory-startup",
                  "--python-exit-code", "1", "--python", str(HERE / "inspect_render.py"),
                  "--", "--input", str(rigged), "--output", str(prepared_dir), "--spec",
                  str(spec_path), "--source-up", "triposr-z", "--texture-resolution",
                  str(resolution)], HERE, prepared_dir / asset.name,
                  output, env, timeout, stages)
        manifest["deformation"] = json.loads((prepared_dir / "rig_inspection.json").read_text())[
            "deformation_smoke"]
        manifest["textures"] = json.loads((prepared_dir / "texture_report.json").read_text())
        motion_set = []
        for clip in clips:
            directory = output / "motions" / clip["id"]
            directory.mkdir(parents=True)
            request = directory / "spec.json"
            request.write_text(json.dumps(clip, indent=2))
            bvh = directory / "motion.bvh"
            run_stage(f"motion_{clip['id']}", [py, str(HERE / "motion_worker.py"), "--checkout",
                      config["motion_checkout"], "--models", config["motion_models"], "--spec",
                      str(request), "--output", str(directory)], HERE, bvh,
                      output, env, timeout, stages)
            motion_set.append(dict(clip, bvh=str(bvh)))
        motion_path = output / "motion_set.json"
        motion_path.write_text(json.dumps(motion_set, indent=2))
        run_stage("retarget", [config["blender"], "--background", "--factory-startup",
                  "--python-exit-code", "1", "--python", str(HERE / "retarget_motion.py"),
                  "--", "--character", str(prepared_dir / asset.name), "--motion-set",
                  str(motion_path), "--asset-id", spec["id"], "--bvh-addon", config["bvh_addon"],
                  "--output", str(output)], HERE, asset, output, env, timeout, stages)
        playback = output / "playback_report.json"
        run_stage("playback", [config["blender"], "--background", "--factory-startup",
                  "--python-exit-code", "1", "--python", str(HERE / "validate_playback.py"),
                  "--", "--input", str(asset), "--clips", str(motion_path), "--report",
                  str(playback)], HERE, playback, output, env, timeout, stages)
        manifest["playback"] = json.loads(playback.read_text())
        manifest["export_qa"] = inspect_glb(asset, require_textures=True,
                                            expected_animations=[c["id"] for c in clips])
        manifest["motion"] = "local_text_to_motion_retarget_structural_pass"
        manifest["status"] = "automatic_structural_candidate_pass"
        manifest["artifacts"] = [{"file": str(p.relative_to(output)), "sha256": digest(p),
                                  "bytes": p.stat().st_size}
                                 for p in sorted(output.rglob("*"))
                                 if p.suffix in (".glb", ".fbx", ".png", ".blend")]
    except (OSError, ValueError, RuntimeError, subprocess.SubprocessError) as exc:
        manifest.update(status="failed", error=str(exc))
    finally:
        # Optional visual logging also captures partial/failed runs; never changes asset success.
        if config.get("debug_capture", True) and (output / "input_spec.json").is_file():
            manifest["debug_capture"] = capture_debug(config, output, spec, env, timeout)
        manifest["seconds"] = round(time.monotonic() - start, 3)
        (output / "automatic_manifest.json").write_text(json.dumps(manifest, indent=2))
    return manifest


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--spec", type=Path, default=ROOT / "character.pilot.json")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    result = execute(json.loads(args.config.read_text()), json.loads(args.spec.read_text()),
                     args.output.resolve())
    print(json.dumps({"status": result["status"], "seconds": result["seconds"],
                      "manifest": str(args.output / "automatic_manifest.json")}))
    return 0 if result["status"] == "automatic_structural_candidate_pass" else 1


if __name__ == "__main__":
    raise SystemExit(main())