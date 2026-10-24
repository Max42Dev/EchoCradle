"""Optional visual debug log of every available character-generation stage."""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import shutil
import subprocess

from pipeline import digest, run_stage

HERE = Path(__file__).resolve().parent
RESULTS = HERE.parent / "results"


def capture_debug(
    config: dict, output: Path, spec: dict, env: dict[str, str], timeout: int,
    results: Path = RESULTS,
) -> dict:
    """Best-effort evidence only; retain successful steps even if another preview fails."""
    records: list[dict] = []
    report = {"status": "running", "required_for_pipeline": False,
              "source_run": str(output), "steps": records}
    try:
        results.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now(timezone.utc).strftime("%Y-%m-%d_%H-%M-%S-%fZ")
        folder = results / f"{spec['id']}_debug_{stamp}"
        folder.mkdir()
        report["folder"] = str(folder)
        logs = folder / "logs"
        logs.mkdir()
        for path in output.glob("*.log"):
            shutil.copy2(path, logs / path.name)
        clips_path = output / "motion_set.json"
        if not clips_path.is_file():
            clips_path = Path(config["animation_spec"])
            document = json.loads(clips_path.read_text(encoding="utf-8"))
            clips_path = folder / "requested_clips.json"
            clips_path.write_text(json.dumps(document["clips"], indent=2), encoding="utf-8")
        clips = json.loads(clips_path.read_text(encoding="utf-8"))
        encoder = None

        def add(label: str, sources: list[Path], mode: str | None = None,
                extra: list[str] | None = None, note: str = "") -> None:
            nonlocal encoder
            index = len(records)
            record = {"step": index, "label": label, "status": "running", "files": [],
                      "note": note, "sources": [str(p) for p in sources]}
            records.append(record)
            try:
                available = [p for p in sources if p.is_file()]
                if not available:
                    record["status"] = "unavailable"
                    return
                for path in available:
                    target = folder / f"step_{index}_{label}_{path.name}"
                    shutil.copy2(path, target)
                    record["files"].append(target.name)
                if mode:
                    if encoder is None:
                        encoder = subprocess.check_output(
                            [config["python"], "-c",
                             "import imageio_ffmpeg; print(imageio_ffmpeg.get_ffmpeg_exe())"],
                            text=True, env=env, timeout=30,
                        ).strip()
                        if not Path(encoder).is_file():
                            raise ValueError("Local video encoder not found")
                    extension = ".png" if mode == "static" else ".mp4"
                    preview = folder / f"step_{index}_{label}{extension}"
                    command = [config["blender"], "--background", "--factory-startup",
                               "--python-exit-code", "1", "--python",
                               str(HERE / "render_review.py"), "--", "--input",
                               str(available[0]), "--clips", str(clips_path), "--output",
                               str(preview), "--ffmpeg", encoder, "--mode", mode]
                    if extra:
                        command.extend(extra)
                    stages: list[dict] = []
                    record["render"] = stages
                    run_stage(f"step_{index}", command, HERE, preview, logs, env,
                              timeout, stages)
                    record["files"].append(preview.name)
                record["status"] = "completed"
            except (OSError, ValueError, RuntimeError, subprocess.SubprocessError) as exc:
                if mode:
                    extension = ".png" if mode == "static" else ".mp4"
                    (folder / f"step_{index}_{label}{extension}").unlink(missing_ok=True)
                record.update(status="failed", error=str(exc))

        add("input", [output / "input_spec.json", Path(config["animation_spec"])])
        attempts = sorted(output.glob("reference_*"))
        for attempt in attempts:
            add(f"picture_{attempt.name}", [attempt / "reference.png", attempt / "spec.json"])
            add(f"foreground_{attempt.name}", [attempt / "foreground.png",
                                               attempt / "foreground_mask.png",
                                               attempt / "framing_qa.json"],
                note="RGBA cutout and alpha mask from the actual framing worker, when saved.")
        if not (output / "foreground.png").is_file() and attempts:
            # Old runs did not save this intermediate. Recreate only for debug and label honestly.
            accepted_attempt = attempts[-1]
            for attempt in attempts:
                qa = attempt / "framing_qa.json"
                if qa.is_file() and json.loads(qa.read_text(encoding="utf-8")).get("pass"):
                    accepted_attempt = attempt
                    break
            accepted = accepted_attempt / "reference.png"
            recovered = folder / "recreated_foreground"
            recovered.mkdir()
            try:
                run_stage("recreate_foreground", [config["python"],
                          str(HERE / "reference_qa.py"), "--input", str(accepted),
                          "--report", str(recovered / "framing_qa.json")], HERE,
                          recovered / "foreground.png", logs, env, timeout, [])
                add("background_removed", [recovered / "foreground.png",
                                           recovered / "foreground_mask.png"],
                    note="Recreated with U2Netp for this historical run; not an original capture.")
            except (OSError, ValueError, RuntimeError, subprocess.SubprocessError) as exc:
                records.append({"step": len(records), "label": "background_removed",
                                "status": "failed", "files": [], "error": str(exc)})
            shutil.rmtree(recovered, ignore_errors=True)
        else:
            add("background_removed", [output / "foreground.png"])
        add("reconstruction_input", [output / "reconstruction_input.png"])
        add("mesh", [output / "mesh.glb", output / "shape_metrics.json"], "static",
            ["--source-up", "triposr-z"])
        add("rig", [output / "rigged.glb"], "static",
            ["--source-up", "triposr-z", "--bones"])
        prepared = output / "prepared"
        add("uv_albedo", sorted(prepared.glob("T_*.png")) + [prepared / "texture_report.json"])
        add("textured_rest", [prepared / "preview_front.png",
                              prepared / "preview_three_quarter.png",
                              prepared / "rig_inspection.json",
                              prepared / f"SK_{spec['id']}.glb"],
            note="Original Blender render before the deformation smoke test.")
        add("deformation_smoke", [prepared / f"SK_{spec['id']}.glb"], "smoke",
            note="Authored rig smoke test, not AI-generated movement.")
        for clip in clips:
            motion = output / "motions" / clip["id"]
            add(f"motion_{clip['id']}", [motion / "motion.bvh", motion / "spec.json",
                                       motion / "motion_report.json",
                                       motion / "motion_joints.npy",
                                       motion / "motion_bvh_joints.npy"], "bvh",
                ["--clip", clip["id"], "--bvh-addon", config["bvh_addon"]],
                note="Raw generated motion fitted to its source skeleton, before retargeting.")
        asset = output / f"SK_{spec['id']}.glb"
        for clip in clips:
            add(f"retarget_{clip['id']}", [asset], "character", ["--clip", clip["id"]])
        add("playback_validation", [output / "retarget_report.json",
                                     output / "playback_report.json"])
        add("final_video", [asset, output / f"SK_{spec['id']}.fbx",
                            output / f"{spec['id']}.blend"], "character",
            note="All exported clips in order. Visual evidence, not anatomical certification.")
        report["status"] = "completed" if all(
            r["status"] == "completed" for r in records) else "partial"
        for record in records:
            record["artifacts"] = [{"file": name, "sha256": digest(folder / name),
                                     "bytes": (folder / name).stat().st_size}
                                    for name in record["files"]]
        (folder / "debug_manifest.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
        lines = ["# Character pipeline visual debug log", "",
                 f"Source run: `{output}`", "",
                 "Preview failures do not affect pipeline success. Open numbered PNG/MP4 files.",
                 ""]
        for record in records:
            lines += [f"## Step {record['step']}: {record['label']} ({record['status']})", "",
                      record.get("note", ""), ""]
            for name in record["files"]:
                lines.append(f"- [{name}]({name})")
            if record.get("error"):
                lines.append(f"Error: {record['error']}")
            lines.append("")
        (folder / "README.md").write_text("\n".join(lines), encoding="utf-8")
    except (OSError, KeyError, ValueError, RuntimeError, subprocess.SubprocessError) as exc:
        report.update(status="failed", error=str(exc))
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--run", type=Path, required=True, help="Existing pipeline attempt directory")
    args = parser.parse_args()
    config = json.loads(args.config.read_text(encoding="utf-8"))
    output = args.run.resolve()
    spec = json.loads((output / "input_spec.json").read_text(encoding="utf-8"))
    env = dict(os.environ, HF_HUB_OFFLINE="1", TRANSFORMERS_OFFLINE="1",
               U2NET_HOME=config["foreground_model"], HF_HOME=config["hf_home"])
    report = capture_debug(config, output, spec, env, config.get("timeout_seconds", 600))
    print(json.dumps({key: value for key, value in report.items() if key != "steps"}))
    return 0 if report["status"] in ("completed", "partial") else 1


if __name__ == "__main__":
    raise SystemExit(main())