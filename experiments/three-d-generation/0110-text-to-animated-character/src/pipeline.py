"""Isolated Proposal B pilot. No provisioning, shell commands or hosted fallbacks."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
GIB = 1024 ** 3
PATH_KEYS = (
    "image_python", "image_model_dir", "sf3d_python", "sf3d_checkout", "sf3d_model_dir",
    "skintokens_python", "skintokens_checkout", "skintokens_checkpoint",
    "blender_executable", "dependency_cache_dir",
)


def digest(path: Path) -> str:
    result = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            result.update(block)
    return result.hexdigest()


def validate_spec(spec: dict[str, Any]) -> None:
    if not isinstance(spec.get("id"), str) or not re.fullmatch(r"[a-z][a-z0-9_]{0,63}", spec["id"]):
        raise ValueError("Invalid character ID")
    for key in ("description", "prompt", "negative_prompt"):
        if not isinstance(spec.get(key), str) or not 1 <= len(spec[key]) <= 4000:
            raise ValueError(f"Invalid {key}")
    for key, lower, upper in (
        ("seed", 0, 2 ** 32 - 1), ("width", 256, 1024), ("height", 256, 1024),
        ("steps", 1, 50),
    ):
        if type(spec.get(key)) is not int or not lower <= spec[key] <= upper:
            raise ValueError(f"Invalid {key}")
    for key, lower, upper in (("height_m", 0.5, 3.0), ("guidance", 1.0, 20.0)):
        value = spec.get(key)
        if type(value) not in (int, float) or not lower <= value <= upper:
            raise ValueError(f"Invalid {key}")
    if spec["width"] % 8 or spec["height"] % 8:
        raise ValueError("Image dimensions must be multiples of eight")


def check_config(config: dict[str, Any], output: Path) -> dict[str, Any]:
    issues = []
    for key in PATH_KEYS:
        value = config.get(key)
        if not isinstance(value, str) or not Path(value).is_absolute() or not Path(value).exists():
            issues.append(f"missing_local_path:{key}")
    for key in ("licenses_approved", "dependencies_provisioned"):
        if config.get(key) is not True:
            issues.append(f"approval_required:{key}")
    for key in ("image_weight_revision", "sf3d_weight_revision", "skintokens_weight_revision"):
        if not isinstance(config.get(key), str) or not re.fullmatch(r"[0-9a-f]{40}", config[key]):
            issues.append(f"pin_required:{key}")
    for stage in ("sf3d", "skintokens"):
        checkout = config.get(f"{stage}_checkout")
        expected = config.get(f"{stage}_source_revision")
        if checkout and Path(checkout).is_dir():
            try:
                actual = subprocess.check_output(
                    ["git", "-C", checkout, "rev-parse", "HEAD"], text=True,
                    stderr=subprocess.DEVNULL, timeout=10,
                ).strip()
                dirty = subprocess.check_output(
                    ["git", "-C", checkout, "status", "--porcelain", "--untracked-files=no"],
                    text=True, stderr=subprocess.DEVNULL, timeout=10,
                ).strip()
                if actual != expected or dirty:
                    issues.append(f"source_revision_mismatch_or_dirty:{stage}")
            except (OSError, subprocess.SubprocessError):
                issues.append(f"unverified_source:{stage}")
    for stage in ("image", "sf3d", "skintokens"):
        executable = config.get(f"{stage}_python")
        modules = {
            "image": ("torch", "diffusers", "PIL"),
            "sf3d": ("torch", "sf3d", "rembg"),
            "skintokens": ("torch", "flash_attn", "bpy"),
        }[stage]
        if executable and Path(executable).is_file():
            code = (
                "import importlib.util,json; print(json.dumps({m:"
                f"importlib.util.find_spec(m) is not None for m in {modules!r}" + "}))"
            )
            try:
                result = subprocess.check_output([executable, "-c", code], text=True, timeout=30)
                if not all(json.loads(result).values()):
                    issues.append(f"missing_dependencies:{stage}")
            except (OSError, ValueError, subprocess.SubprocessError):
                issues.append(f"environment_probe_failed:{stage}")
    ancestor = output.resolve()
    while not ancestor.exists():
        ancestor = ancestor.parent
    free = shutil.disk_usage(ancestor).free / GIB
    budget = []
    for key in ("provisioning_remaining_gib", "scratch_gib", "reserve_gib"):
        value = config.get(key)
        if type(value) not in (float, int) or not 0 <= value <= 1000:
            issues.append(f"invalid_budget:{key}")
        else:
            budget.append(value)
    required = sum(budget)
    if free < required:
        issues.append("insufficient_disk_budget")
    timeout = config.get("stage_timeout_seconds")
    if type(timeout) is not int or not 1 <= timeout <= 7200:
        issues.append("invalid_stage_timeout")
    try:
        gpu = subprocess.check_output(
            ["nvidia-smi", "--query-gpu=name,memory.total,memory.used,driver_version",
             "--format=csv,noheader"], text=True, timeout=10,
        ).strip()
    except (OSError, subprocess.SubprocessError):
        gpu = "unavailable"
        issues.append("cuda_device_probe_failed")
    return {"issues": issues, "free_disk_gib": round(free, 3),
            "required_disk_gib": required, "gpu": gpu, "python": sys.version}


def run_stage(
    name: str, command: list[str], cwd: Path, expected: Path, output: Path,
    env: dict[str, str], timeout: int, stages: list[dict[str, Any]],
) -> None:
    start = time.monotonic()
    record: dict[str, Any] = {"name": name, "command": command, "status": "running"}
    stages.append(record)
    try:
        with (output / f"{name}.log").open("w", encoding="utf-8") as log:
            process = subprocess.Popen(
                command, cwd=cwd, env=env, stdout=log, stderr=subprocess.STDOUT,
                shell=False, start_new_session=os.name != "nt",
            )
            try:
                returncode = process.wait(timeout=timeout)
            finally:
                if process.poll() is None:
                    if os.name == "nt":
                        subprocess.run(["taskkill", "/PID", str(process.pid), "/T", "/F"],
                                       stdout=log, stderr=log, check=False, timeout=15)
                    else:
                        os.killpg(process.pid, signal.SIGKILL)
                    process.kill()
                    process.wait(timeout=15)
        record["returncode"] = returncode
        if returncode != 0 or not expected.is_file() or expected.stat().st_size == 0:
            raise RuntimeError(f"{name} failed; inspect {name}.log")
        if expected.stat().st_size > 256 * 1024 ** 2:
            raise RuntimeError(f"{name} exceeded 256 MiB artifact limit")
        record.update(status="completed", artifact=str(expected), sha256=digest(expected))
    except (OSError, subprocess.SubprocessError, RuntimeError) as exc:
        record.update(status="failed", error=str(exc))
        raise
    finally:
        record["seconds"] = round(time.monotonic() - start, 3)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=ROOT / "pipeline.local.example.json")
    parser.add_argument("--spec", type=Path, default=ROOT / "character.json")
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--run", action="store_true", help="Run only after preflight passes")
    parser.add_argument("--reference", type=Path, help="Resume from a hash-reviewed pilot image")
    args = parser.parse_args(argv)
    config = json.loads(args.config.read_text(encoding="utf-8"))
    spec = json.loads(args.spec.read_text(encoding="utf-8"))
    validate_spec(spec)
    output = args.output.resolve()
    if output.exists() and any(output.iterdir()):
        parser.error("Use a fresh output directory; never overwrite a prior attempt")
    output.mkdir(parents=True, exist_ok=True)
    manifest: dict[str, Any] = {
        "status": "preflight", "spec": spec, "config": config, "stages": [],
        "started_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "inference_network": "offline_flags_set_OS_network_isolation_not_verified",
        "character_success": False, "unity": "not_run", "animation": "not_run",
    }
    result = 0
    try:
        manifest["preflight"] = check_config(config, output)
        if manifest["preflight"]["issues"]:
            manifest["status"] = "blocked"
            # A preflight is a successful diagnostic, not a successful character.
            result = 2 if args.run else 0
        elif not args.run:
            manifest["status"] = "ready_not_run"
        else:
            env = dict(os.environ, HF_HUB_OFFLINE="1", TRANSFORMERS_OFFLINE="1",
                       HF_DATASETS_OFFLINE="1", HF_HUB_DISABLE_TELEMETRY="1",
                       DO_NOT_TRACK="1", PYTHONHASHSEED=str(spec["seed"]))
            cache = str(Path(config["dependency_cache_dir"]).resolve())
            env.update(HF_HOME=cache, U2NET_HOME=str(Path(cache) / "u2net"),
                       TORCH_HOME=str(Path(cache) / "torch"))
            timeout = config["stage_timeout_seconds"]
            stages = manifest["stages"]
            reference = output / "reference.png"
            if args.reference is None:
                run_stage("image", [config["image_python"], str(HERE / "image_worker.py"),
                          "--spec", str(args.spec.resolve()), "--model", config["image_model_dir"],
                          "--output", str(reference)], HERE, reference, output, env, timeout, stages)
                manifest["status"] = "awaiting_reference_review"
                manifest["reference_sha256"] = digest(reference)
            else:
                if digest(args.reference) != config.get("reference_review_sha256"):
                    raise RuntimeError("Reference image needs explicit hash-matched pose review")
                shutil.copyfile(args.reference, reference)
                manifest["reference_sha256"] = digest(reference)
                shape_dir = output / "shape"
                mesh = shape_dir / "0" / "mesh.glb"
                run_stage("shape", [config["sf3d_python"], "run.py", str(reference),
                          "--pretrained-model", config["sf3d_model_dir"], "--output-dir",
                          str(shape_dir), "--texture-resolution", "1024"],
                          Path(config["sf3d_checkout"]), mesh, output, env, timeout, stages)
                prepared = output / "prepared.glb"
                rigged = output / "rigged.glb"
                fbx = output / f"SK_{spec['id']}.fbx"
                run_stage("prepare", [config["blender_executable"], "--background",
                              "--factory-startup", "--python-exit-code", "1", "--python",
                              str(HERE / "blender_worker.py"), "--", "--mode", "prepare",
                              "--input", str(mesh), "--output", str(prepared), "--report",
                              str(output / "prepare_qa.json"), "--height-m", str(spec["height_m"])],
                              HERE, prepared, output, env, timeout, stages)
                run_stage("rig", [config["skintokens_python"], str(HERE / "rig_worker.py"),
                          "--checkout", config["skintokens_checkout"], "--checkpoint",
                          config["skintokens_checkpoint"], "--input", str(prepared),
                          "--output", str(rigged), "--seed", str(spec["seed"])],
                          Path(config["skintokens_checkout"]), rigged, output, env, timeout, stages)
                run_stage("export", [config["blender_executable"], "--background",
                          "--factory-startup", "--python-exit-code", "1", "--python",
                          str(HERE / "blender_worker.py"), "--", "--mode", "export", "--input",
                          str(rigged), "--output", str(fbx), "--report",
                          str(output / "export_qa.json"), "--height-m", str(spec["height_m"])],
                          HERE, fbx, output, env, timeout, stages)
                manifest["status"] = "rigged_candidate_requires_deformation_and_unity_review"
    except (OSError, ValueError, RuntimeError, subprocess.SubprocessError) as exc:
        manifest.update(status="failed", error=str(exc))
        result = 1
    finally:
        (output / "manifest.json").write_text(
            json.dumps(manifest, indent=2) + "\n", encoding="utf-8",
        )
    print(json.dumps({"status": manifest["status"], "manifest": str(output / "manifest.json")}))
    return result


if __name__ == "__main__":
    raise SystemExit(main())