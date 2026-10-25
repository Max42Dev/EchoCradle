"""Visual contracts with synthetic containers, fake workers and no model inference."""
from __future__ import annotations

import json
import os
import runpy
import socket
import struct
import sys
import threading
import zlib
from pathlib import Path
from unittest.mock import Mock

import pytest

import orchestrator.visual as visual
from orchestrator import ModelOrchestrator
from orchestrator.probe import ProbeReport
from orchestrator.visual import VisualBackend, VisualCancelled, VisualError, validate_visual


@pytest.fixture(autouse=True)
def isolated_debug_environment(monkeypatch):
    monkeypatch.delenv("ECHOCRADLE_VISUAL_DEBUG_DIR", raising=False)
    monkeypatch.delenv("ECHOCRADLE_VISUAL_LOG", raising=False)


def chunk(kind: bytes, data: bytes) -> bytes:
    return struct.pack(">I", len(data)) + kind + data + struct.pack(
        ">I", zlib.crc32(kind + data) & 0xFFFFFFFF
    )


def png() -> bytes:
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", 1, 1, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(b"\0\xff\0\0")) + chunk(b"IEND", b""))


def glb(doc: dict | None = None, binary: bytes = b"") -> bytes:
    doc = {"asset": {"version": "2.0"}} if doc is None else doc
    payload = json.dumps(doc).encode()
    payload += b" " * (-len(payload) % 4)
    body = struct.pack("<I4s", len(payload), b"JSON") + payload
    if binary:
        binary += b"\0" * (-len(binary) % 4)
        body += struct.pack("<I4s", len(binary), b"BIN\0") + binary
    return struct.pack("<4sII", b"glTF", 2, len(body) + 12) + body


def animated() -> bytes:
    image = png()
    return glb({
        "asset": {"version": "2.0"}, "buffers": [{"byteLength": len(image)}],
        "bufferViews": [{"buffer": 0, "byteLength": len(image)}],
        "images": [{"bufferView": 0, "mimeType": "image/png"}],
        "textures": [{"source": 0}],
        "materials": [{"pbrMetallicRoughness": {"baseColorTexture": {"index": 0}}}],
        "skins": [{"joints": [0]}],
        "meshes": [{"primitives": [{"attributes": {
            "JOINTS_0": 0, "WEIGHTS_0": 1, "TEXCOORD_0": 2}, "material": 0}]}],
        "animations": [{"name": name, "channels": [{}]}
                       for name in ("Idle", "Walk", "Run", "Jump", "Attack")],
    }, image)


@pytest.fixture
def backend(tmp_path, monkeypatch):
    """A full fake deployment, exercising real deployment checks except git execution."""
    revision = "a" * 40
    config = {}
    for name in ("python", "blender", "image_model", "tripo_model", "tripo_checkout",
                 "rig_checkout", "rig_checkpoint", "hf_home", "foreground_model",
                 "motion_checkout", "motion_models", "bvh_addon", "animation_spec"):
        config[name] = name
    config.update({f"{key}_revision": revision for key in ("tripo", "rig", "motion")})
    config.update(timeout_seconds=60, texture_resolution=512)
    config_path = tmp_path / "automatic.local.json"
    config_path.write_text(json.dumps(config))

    def file(name):
        path = tmp_path / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("fixture")

    for name in ("python", "blender", "rig_checkpoint", "src/image_worker.py",
                 "src/triposr_worker.py", "src/rig_worker.py", "src/inspect_render.py",
                 "src/texture_bake.py", "src/motion_worker.py", "src/retarget_motion.py",
                 "src/validate_playback.py", "rig_checkout/demo.py", "rig_checkout/bpy_server.py",
                 "src/verify_result.py", "rig_checkout/experiments/skin_vae_2_10_32768/last.ckpt",
                 "rig_checkout/models/Qwen3-0.6B/config.json", "bvh_addon/io_anim_bvh/__init__.py",
                 "foreground_model/u2netp.onnx", "tripo_model/config.yaml",
                 "tripo_model/model.ckpt", "motion_models/ViT-B-32.pt",
                 "hf_home/hub/models--facebook--dino-vitb16/snapshots/rev/config.json"):
        file(name)
    for name in ("model_index.json", "scheduler/scheduler_config.json", "tokenizer/vocab.json",
                 "tokenizer/merges.txt", "tokenizer/tokenizer_config.json", "unet/config.json",
                 "vae/config.json", "text_encoder/config.json",
                 "unet/diffusion_pytorch_model.fp16.safetensors",
                 "vae/diffusion_pytorch_model.fp16.safetensors",
                 "text_encoder/model.fp16.safetensors"):
        file(f"image_model/{name}")
    for name, checkpoint in (
        ("rvq_nq6_dc512_nc512_noshare_qdp0.2", "net_best_fid.tar"),
        ("t2m_nlayer8_nhead6_ld384_ff1024_cdp0.1_rvq6ns", "latest.tar"),
        ("tres_nlayer8_ld384_ff1024_rvq6ns_cdp0.2_sw", "net_best_fid.tar"),
    ):
        file(f"motion_models/t2m/{name}/opt.txt")
        file(f"motion_models/t2m/{name}/model/{checkpoint}")
    for name in ("mean.npy", "std.npy"):
        file(f"motion_models/t2m/rvq_nq6_dc512_nc512_noshare_qdp0.2/meta/{name}")
    clips = [{"id": name, "prompt": name, "frames": 80, "fps": 20, "seed": i,
              "root_motion": "in_place", "loop": True}
             for i, name in enumerate(("Idle", "Walk", "Run", "Jump", "Attack"))]
    (tmp_path / "animation_spec").write_text(json.dumps({"clips": clips}))
    monkeypatch.setattr(visual.subprocess, "check_output", lambda args, **kw:
                        revision if "rev-parse" in args else "")
    return VisualBackend(config_path)


@pytest.fixture
def runner(monkeypatch):
    calls = []

    def run(command, **kwargs):
        calls.append((command, kwargs))
        output = Path(command[command.index("--output") + 1]) if "--output" in command else None
        if any(name.endswith("image_worker.py") for name in command):
            output.write_bytes(png())
        elif any(name.endswith(("triposr_worker.py", "rig_worker.py")) for name in command):
            output.write_bytes(glb())
        elif any(name.endswith("inspect_render.py") for name in command):
            (output / "SK_visual.glb").write_bytes(glb())
        elif any(name.endswith("motion_worker.py") for name in command):
            (output / "motion.bvh").write_text("HIERARCHY")
        elif any(name.endswith("retarget_motion.py") for name in command):
            (output / "SK_visual.glb").write_bytes(animated())
        elif "--report" in command:
            Path(command[command.index("--report") + 1]).write_text("{}")

    monkeypatch.setattr(visual, "_run_process", run)
    return calls


@pytest.mark.parametrize("data,media", [(png(), "image/png"), (glb(), "model/gltf-binary"),
                                         (animated(), "model/gltf-binary")])
def test_valid_containers(data, media):
    assert validate_visual(data, media) is None


@pytest.mark.parametrize("data,media", [
    (b"", "image/png"), (png()[:-1], "image/png"), (png() + b"tail", "image/png"),
    (png()[:40] + b"bad" + png()[43:], "image/png"), (b"not png", "image/png"),
    (glb()[:-1], "model/gltf-binary"), (glb() + b"tail", "model/gltf-binary"),
    (glb({"asset": {"version": "1.0"}}), "model/gltf-binary"),
    (glb({"asset": {"version": "2.0"}, "images": [{"uri": "file:///secret"}]}),
     "model/gltf-binary"),
    (glb({"asset": {"version": "2.0"}, "buffers": [{"uri": "data:x", "byteLength": 4}]}),
     "model/gltf-binary"),
    (glb({"asset": {"version": "2.0"}, "buffers": [{"byteLength": 100}]}, b"1234"),
     "model/gltf-binary"),
    (glb({"asset": {"version": "2.0"}, "bufferViews": [{"buffer": 0, "byteLength": 4}]}),
     "model/gltf-binary"), (png(), "application/octet-stream"),
])
def test_reject_bad_containers(data, media):
    with pytest.raises(ValueError):
        validate_visual(data, media)


def test_png_zlib_bomb_and_bad_filter_rejected():
    header = chunk(b"IHDR", struct.pack(">IIBBBBB", 1, 1, 8, 2, 0, 0, 0))
    for raw in (b"x" * 100000, b"\x05\0\0\0", b"\0\0"):
        data = b"\x89PNG\r\n\x1a\n" + header + chunk(b"IDAT", zlib.compress(raw))
        with pytest.raises(ValueError):
            validate_visual(data + chunk(b"IEND", b""), "image/png")


@pytest.mark.parametrize("width,height", [(8193, 1), (1, 8193), (8192, 8192)])
def test_glb_embedded_png_dimensions_rejected(width: int, height: int) -> None:
    image = (b"\x89PNG\r\n\x1a\n"
             + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0))
             + chunk(b"IDAT", zlib.compress(b"\0\xff\0\0")) + chunk(b"IEND", b""))
    with pytest.raises(ValueError, match="PNG IHDR"):
        validate_visual(embedded_image(image), "model/gltf-binary")


def embedded_image(image: bytes, media_type: str = "image/png") -> bytes:
    # Nonzero offset and trailing bytes ensure validation uses the exact bufferView slice.
    return glb({
        "asset": {"version": "2.0"}, "buffers": [{"byteLength": len(image) + 8}],
        "bufferViews": [{"buffer": 0, "byteOffset": 4, "byteLength": len(image)}],
        "images": [{"bufferView": 0, "mimeType": media_type}],
    }, b"head" + image + b"tail")


@pytest.mark.parametrize("image", [
    b"not png", png()[:-1], png() + b"tail", png()[:40] + b"bad" + png()[43:],
    b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", 1, 1, 8, 2, 0, 0, 0))
    + chunk(b"IDAT", zlib.compress(b"x" * 100000)) + chunk(b"IEND", b""),
])
def test_glb_malformed_embedded_png_rejected(image: bytes) -> None:
    with pytest.raises(ValueError):
        validate_visual(embedded_image(image), "model/gltf-binary")


def test_glb_embedded_png_exact_slice_and_jpeg_rejection() -> None:
    validate_visual(embedded_image(png()), "model/gltf-binary")
    with pytest.raises(ValueError, match="PNG"):
        validate_visual(embedded_image(b"\xff\xd8\xff\xd9", "image/jpeg"), "model/gltf-binary")


def test_limit(monkeypatch):
    monkeypatch.setattr(visual, "MAX_VISUAL_BYTES", 20)
    with pytest.raises(ValueError, match="32 MiB"):
        validate_visual(png(), "image/png")


def test_worker_network_guard_allows_only_loopback(monkeypatch):
    original_connect = Mock()
    original_bind = Mock()
    original_resolve = Mock(return_value=[])
    # Register restoration before executing the worker-only customization in this test.
    monkeypatch.setattr(socket.socket, "connect", original_connect)
    monkeypatch.setattr(socket.socket, "connect_ex", Mock())
    monkeypatch.setattr(socket.socket, "bind", original_bind)
    monkeypatch.setattr(socket, "getaddrinfo", original_resolve)
    guard = Path(visual.__file__).with_name("visual_workers") / "sitecustomize.py"
    runpy.run_path(str(guard))
    fake = Mock(family=socket.AF_INET)
    with pytest.raises(OSError, match="cached-only"):
        socket.socket.connect(fake, ("huggingface.co", 443))
    with pytest.raises(OSError, match="cached-only"):
        socket.getaddrinfo("huggingface.co", 443)
    socket.socket.connect(fake, ("127.0.0.1", 59876))
    original_connect.assert_called_once_with(fake, ("127.0.0.1", 59876))
    socket.socket.bind(fake, ("0.0.0.0", 59876))
    original_bind.assert_called_once_with(fake, ("127.0.0.1", 59876))
    socket.getaddrinfo("0.0.0.0", 59876)
    original_resolve.assert_called_once_with("127.0.0.1", 59876)


def test_availability_and_missing_deployment(backend, tmp_path):
    assert all(item["available"] for item in backend.availability().values())
    (tmp_path / "src/image_worker.py").unlink()
    state = backend.availability()
    assert not state["image"]["available"]
    assert state["image_to_3d"]["available"]
    assert "image_worker.py" in state["image"]["reasons"][0]
    assert not VisualBackend(tmp_path / "missing.json").availability()["image"]["available"]


def test_pinned_source_rejected(backend, monkeypatch):
    monkeypatch.setattr(visual.subprocess, "check_output", lambda *a, **kw: "dirty")
    assert not backend.availability()["animate_3d"]["available"]


@pytest.mark.parametrize("name", [
    "src/verify_result.py", "rig_checkout/experiments/skin_vae_2_10_32768/last.ckpt",
])
@pytest.mark.parametrize("empty", [False, True])
def test_animation_transitive_dependencies_required(backend, tmp_path, name, empty):
    dependency = tmp_path / name
    if empty:
        dependency.write_bytes(b"")
    else:
        dependency.unlink()
    state = backend.availability()
    assert not state["animate_3d"]["available"]
    assert str(dependency) in state["animate_3d"]["reasons"][0]
    assert state["image"]["available"] and state["image_to_3d"]["available"]


def test_shippable_rejected(backend, runner, tmp_path):
    backend.shippable_only = True
    assert not backend.availability()["image"]["available"]
    with pytest.raises(VisualError, match="shippable_only"):
        backend.generate_image("test", tmp_path / "out.png", seed=1)
    assert not runner


def test_image_command_offline_and_cleanup(backend, runner, tmp_path):
    out = tmp_path / "result.png"
    assert backend.generate_image("hi; not shell", out, seed=42, negative_prompt="no") == out
    validate_visual(out.read_bytes(), "image/png")
    args, kwargs = runner[0]
    assert args[0] == str(tmp_path / "python")
    assert args[1] == str(tmp_path / "src/image_worker.py")
    env = kwargs["env"]
    assert env["HF_HUB_OFFLINE"] == env["TRANSFORMERS_OFFLINE"] == "1"
    assert env["HF_HOME"] == str(tmp_path / "hf_home")
    assert env["U2NET_HOME"] == str(tmp_path / "foreground_model")
    assert "visual_workers" in env["PYTHONPATH"]
    assert not kwargs["cwd"].exists()
    assert not list(tmp_path.glob(".visual-*"))


@pytest.mark.parametrize("debug_enabled", [False, True])
@pytest.mark.parametrize("failure", ["git_missing", "git_failed", "preflight", "config",
                                    "output_parent", "scratch"])
def test_early_failure_diagnostics(
    backend, runner, tmp_path, monkeypatch, debug_enabled: bool, failure: str,
) -> None:
    root = tmp_path / "debug"
    if debug_enabled:
        monkeypatch.setenv("ECHOCRADLE_VISUAL_DEBUG_DIR", str(root))
    secret = "private-token-must-not-be-serialized"
    out = tmp_path / "result.glb"
    out.write_bytes(b"previous")
    cause_type = ValueError
    expected_type = "VisualError"
    if failure.startswith("git"):
        cause_type = (FileNotFoundError if failure == "git_missing"
                      else visual.subprocess.CalledProcessError)
        error = (FileNotFoundError(secret) if failure == "git_missing"
                 else visual.subprocess.CalledProcessError(1, ["git", secret], output=secret))
        monkeypatch.setattr(visual.subprocess, "check_output", Mock(side_effect=error))
    elif failure == "preflight":
        (tmp_path / "tripo_model/model.ckpt").unlink()
    elif failure == "config":
        backend = VisualBackend(tmp_path / "missing.json")
    elif failure == "output_parent":
        out = out / "result.glb"  # Parent is an existing file.
        expected_type = "FileExistsError"
    else:
        monkeypatch.setattr(visual.tempfile, "TemporaryDirectory",
                            Mock(side_effect=PermissionError(secret)))
        expected_type = "PermissionError"
    before = set(tmp_path.rglob("*"))
    with pytest.raises((VisualError, OSError)) as caught:
        backend.generate_3d(tmp_path / "unused.png", out, seed=42)
    if expected_type == "VisualError":
        assert isinstance(caught.value.__cause__, cause_type)
        assert "Visual stage unavailable" in str(caught.value)
    assert not runner
    assert (tmp_path / "result.glb").read_bytes() == b"previous"
    if debug_enabled:
        debug, = root.iterdir()
        assert "-image_to_3d-" in debug.name
        manifest_text = (debug / "manifest.json").read_text()
        assert json.loads(manifest_text) == {
            "stage": "image_to_3d", "seed": 42, "status": "failed",
            "error": {"type": expected_type}, "workers": 0,
        }
        assert secret not in manifest_text
        assert set(debug.iterdir()) == {debug / "manifest.json"}
    else:
        assert set(tmp_path.rglob("*")) == before
    assert not list(tmp_path.glob(".visual-*"))


@pytest.mark.parametrize("outcome", ["success", "failure", "cancelled", "invalid", "timeout"])
def test_debug_preserves_complete_scratch_and_safe_manifest(
    backend, tmp_path, monkeypatch, outcome,
):
    root = tmp_path / "debug"
    monkeypatch.setenv("ECHOCRADLE_VISUAL_DEBUG_DIR", str(root))
    monkeypatch.setenv("HF_TOKEN", "private-token")
    out = tmp_path / "result.png"
    out.write_bytes(b"previous")
    scratches = []
    environments = []

    def run(command, **kwargs):
        scratch = kwargs["cwd"]
        scratches.append(scratch)
        environments.append(kwargs["env"])
        (scratch / "nested").mkdir(exist_ok=True)
        (scratch / "nested" / "intermediate.bin").write_bytes(b"intermediate")
        Path(command[-1]).write_bytes(b"bad" if outcome == "invalid" else png())
        if outcome == "failure":
            raise VisualError("private-token must not appear in manifest")
        if outcome == "cancelled":
            raise VisualCancelled("private-token")
        if outcome == "timeout":
            raise TimeoutError("private-token")

    monkeypatch.setattr(visual, "_run_process", run)
    if outcome == "success":
        backend.generate_image("prompt", out, seed=42)
        assert out.read_bytes() == png()
    else:
        with pytest.raises((VisualError, ValueError, TimeoutError)):
            backend.generate_image("prompt", out, seed=42)
        assert out.read_bytes() == b"previous"
    debug, = root.iterdir()
    assert "-image-" in debug.name and debug.name[:8].isdigit()
    snapshot = debug / "scratch"
    assert (snapshot / "nested" / "intermediate.bin").read_bytes() == b"intermediate"
    assert (snapshot / "reference.png").read_bytes() == (
        b"bad" if outcome == "invalid" else png()
    )
    assert json.loads((snapshot / "spec.json").read_text())["prompt"] == "prompt"
    manifest_text = (debug / "manifest.json").read_text()
    manifest = json.loads(manifest_text)
    expected = outcome if outcome in ("success", "cancelled") else "failed"
    assert manifest["status"] == expected
    assert manifest["stage"] == "image" and manifest["seed"] == 42
    assert manifest["workers"] == 1
    assert manifest["error"] == (None if outcome == "success" else {
        "type": {"failure": "VisualError", "cancelled": "VisualCancelled",
                 "invalid": "ValueError", "timeout": "TimeoutError"}[outcome]
    })
    assert "private-token" not in manifest_text
    assert environments[0]["ECHOCRADLE_VISUAL_LOG"] == str(debug / "worker-001")
    assert environments[0]["PYTHONUNBUFFERED"] == "1"
    assert all(not scratch.exists() for scratch in scratches)
    assert not list(tmp_path.glob(".visual-*"))


def test_debug_unique_stages_and_sequential_worker_logs(backend, runner, tmp_path, monkeypatch):
    root = tmp_path / "debug"
    monkeypatch.setenv("ECHOCRADLE_VISUAL_DEBUG_DIR", str(root))
    for _ in range(2):
        backend.generate_image("prompt", tmp_path / "image.png", seed=9)
    mesh = tmp_path / "mesh.glb"
    mesh.write_bytes(glb())
    backend.animate_3d(mesh, tmp_path / "animated.glb", seed=9)
    assert len(list(root.iterdir())) == 3
    logs = [kwargs["env"]["ECHOCRADLE_VISUAL_LOG"] for _, kwargs in runner]
    assert len(set(logs)) == len(logs)
    animation = next(root.glob("*-animate_3d-*"))
    assert logs[2:] == [str(animation / f"worker-{index:03d}") for index in range(1, 10)]
    assert (animation / "scratch" / "input.glb").is_file()
    assert not list(tmp_path.glob(".visual-*"))


def test_no_debug_ignores_inherited_worker_log(backend, runner, tmp_path, monkeypatch):
    log = tmp_path / "untrusted-log"
    monkeypatch.setenv("ECHOCRADLE_VISUAL_LOG", str(log))
    backend.generate_image("prompt", tmp_path / "image.png", seed=9)
    assert "ECHOCRADLE_VISUAL_LOG" not in runner[0][1]["env"]
    assert not list(tmp_path.glob("*.log"))


@pytest.mark.parametrize("exit_code", [0, 7])
def test_real_cpu_worker_stdout_stderr_preserved(tmp_path, exit_code):
    log = tmp_path / "worker-001"
    env = dict(os.environ, ECHOCRADLE_VISUAL_PYTHON=sys.executable,
               ECHOCRADLE_VISUAL_LOG=str(log))
    command = [sys.executable, "-c",
               "import sys; print('worker output', flush=True); "
               f"print('actual worker failure', file=sys.stderr); sys.exit({exit_code})"]
    if exit_code:
        with pytest.raises(VisualError, match="exit code 7"):
            visual._run_process(command, cwd=tmp_path, env=env, timeout=10, should_stop=None)
    else:
        visual._run_process(command, cwd=tmp_path, env=env, timeout=10, should_stop=None)
    assert "worker output" in Path(f"{log}.stdout.log").read_text()
    assert "actual worker failure" in Path(f"{log}.stderr.log").read_text()
    # Streams must be closed even after a failing worker (Windows disallows open-file deletion).
    Path(f"{log}.stdout.log").unlink()
    Path(f"{log}.stderr.log").unlink()


def test_independent_handoff_full_animation(backend, runner, tmp_path):
    image = backend.generate_image("human", tmp_path / "image.png", seed=9)
    mesh = backend.generate_3d(image, tmp_path / "mesh.glb", seed=9)
    asset = backend.animate_3d(mesh, tmp_path / "animated.glb", seed=9)
    validate_visual(asset.read_bytes(), "model/gltf-binary")
    assert len(runner) == 11  # image, reconstruction, rig, prep, 5 motions, retarget, playback
    commands = [args for args, _ in runner]
    assert any("seed_worker.py" in arg for arg in commands[1])
    rig = commands[2]
    assert rig[-2:] == ["--seed", "9"]
    assert runner[2][1]["cwd"] == tmp_path / "rig_checkout"
    assert image.is_file() and mesh.is_file()  # Reusable, never consumed/deleted.
    assert not list(tmp_path.glob(".visual-*"))


def test_inputs_validated_before_native_import(backend, runner, tmp_path):
    source = tmp_path / "bad.png"
    source.write_bytes(b"bad")
    with pytest.raises(ValueError):
        backend.generate_3d(source, tmp_path / "mesh.glb", seed=0)
    source.write_bytes(glb({"asset": {"version": "2.0"}, "images": [{"uri": "https://x"}]}))
    with pytest.raises(ValueError):
        backend.animate_3d(source, tmp_path / "anim.glb", seed=0)
    assert not runner


@pytest.mark.parametrize("seed", [True, -1, 2 ** 32, 1.0])
def test_bad_seed(backend, runner, tmp_path, seed):
    with pytest.raises(ValueError, match="seed"):
        backend.generate_image("hi", tmp_path / "out", seed=seed)
    assert not runner


def test_failure_and_late_cancellation_preserve_output(backend, tmp_path, monkeypatch):
    out = tmp_path / "result.png"
    out.write_bytes(b"previous")
    cancelled = False

    def fail(command, **kwargs):
        Path(command[-1]).write_bytes(png())
        raise VisualError("fake worker failure")

    monkeypatch.setattr(visual, "_run_process", fail)
    with pytest.raises(VisualError):
        backend.generate_image("test", out, seed=0)
    assert out.read_bytes() == b"previous"

    def finish(command, **kwargs):
        nonlocal cancelled
        Path(command[-1]).write_bytes(png())
        cancelled = True

    monkeypatch.setattr(visual, "_run_process", finish)
    with pytest.raises(VisualCancelled):
        backend.generate_image("test", out, seed=0, should_stop=lambda: cancelled)
    assert out.read_bytes() == b"previous"
    assert not list(tmp_path.glob(".visual-*"))


def test_timeout_budget_and_bad_output(backend, tmp_path, monkeypatch):
    def run(command, **kwargs):
        assert 0 < kwargs["timeout"] <= 60
        Path(command[-1]).write_bytes(b"invalid")

    monkeypatch.setattr(visual, "_run_process", run)
    with pytest.raises(ValueError):
        backend.generate_image("test", tmp_path / "out", seed=0)
    assert not (tmp_path / "out").exists()


def test_stage_deadline_no_publication(backend, tmp_path, monkeypatch):
    clock = [0.0]
    monkeypatch.setattr(visual.time, "monotonic", lambda: clock[0])

    def run(command, **kwargs):
        Path(command[-1]).write_bytes(png())
        clock[0] = 61.0

    monkeypatch.setattr(visual, "_run_process", run)
    with pytest.raises(TimeoutError):
        backend.generate_image("test", tmp_path / "out", seed=0)
    assert not (tmp_path / "out").exists()
    assert not list(tmp_path.glob(".visual-*"))


def test_server_owned_image_and_motion_specs(backend, tmp_path, monkeypatch, runner):
    original = visual._run_process
    specs = []

    def capture(command, **kwargs):
        if "--spec" in command:
            specs.append(json.loads(Path(command[command.index("--spec") + 1]).read_text()))
        original(command, **kwargs)

    monkeypatch.setattr(visual, "_run_process", capture)
    image = backend.generate_image("custom prompt", tmp_path / "im.png", seed=42,
                                   negative_prompt="custom negative")
    assert specs[0] == dict(prompt="custom prompt", negative_prompt="custom negative", seed=42,
                            width=512, height=512, steps=35, guidance=8.0)
    mesh = backend.generate_3d(image, tmp_path / "mesh.glb", seed=42)
    backend.animate_3d(mesh, tmp_path / "anim.glb", seed=42)
    assert [item["seed"] for item in specs if "frames" in item] == [42, 43, 44, 45, 46]


def test_motion_failure_cleans_scratch_and_never_publishes(backend, tmp_path, monkeypatch, runner):
    mesh = tmp_path / "mesh.glb"
    mesh.write_bytes(glb())
    original = visual._run_process

    def run(command, **kwargs):
        if any(arg.endswith("motion_worker.py") for arg in command):
            raise VisualCancelled("cancelled during motion")
        original(command, **kwargs)

    monkeypatch.setattr(visual, "_run_process", run)
    with pytest.raises(VisualCancelled):
        backend.animate_3d(mesh, tmp_path / "anim.glb", seed=0)
    assert mesh.is_file()
    assert not (tmp_path / "anim.glb").exists()
    assert not list(tmp_path.glob(".visual-*"))


@pytest.fixture
def facade(monkeypatch, tmp_path):
    import orchestrator.orchestrator as module

    for name in ("TextHost", "TtsHost", "SttHost"):
        host = Mock(loaded_model_id=None, base_url=None, _process=None)
        monkeypatch.setattr(module, name, Mock(return_value=host))
    return ModelOrchestrator(store=Mock(), report=ProbeReport(ram_gb=16), cache=tmp_path)


def test_facade_missing_optional_and_fail_closed(facade, tmp_path):
    assert facade.visual is None
    with pytest.raises(VisualError, match="not configured"):
        facade.generate_image("test", tmp_path / "out", seed=0)
    facade.visual = Mock()
    facade.text.unload.side_effect = RuntimeError("unload failed")
    with pytest.raises(VisualError, match="unload"):
        facade.generate_image("test", tmp_path / "out", seed=0)
    facade.visual.generate_image.assert_not_called()
    facade.text.unload.side_effect = None
    facade.text.base_url = "http://localhost:1234"
    with pytest.raises(VisualError, match="external"):
        facade.generate_image("test", tmp_path / "out", seed=0)


def test_facade_unloads_forwards_and_reloads_lazily(facade, tmp_path, monkeypatch):
    facade.visual = Mock()
    out = tmp_path / "out"
    facade.visual.generate_image.return_value = out
    facade.text.loaded_model_id = "resident"
    facade.text.unload.side_effect = lambda: setattr(facade.text, "loaded_model_id", None)
    assert facade.generate_image("hi", out, seed=2, negative_prompt="no") == out
    facade.visual.generate_image.assert_called_once_with(
        "hi", out, seed=2, negative_prompt="no", should_stop=None
    )
    ensure = Mock()
    monkeypatch.setattr(facade, "ensure_model", ensure)
    facade.chat([])
    ensure.assert_called_once()
    facade.shippable_only = True
    with pytest.raises(VisualError, match="shippable_only"):
        facade.animate_3d(out, out, seed=0)


def test_generator_holds_gpu_lane_until_closed(facade, monkeypatch):
    monkeypatch.setattr(facade, "_ensure_loaded", Mock())
    facade.text.stream_chat.return_value = iter(["one", "two"])
    stream = facade.stream_chat([])
    assert next(stream) == "one"
    acquired = []

    def try_lock():
        ok = facade.gpu_lock.acquire(blocking=False)
        acquired.append(ok)
        if ok:
            facade.gpu_lock.release()

    thread = threading.Thread(target=try_lock)
    thread.start()
    thread.join(timeout=2)
    assert acquired == [False]
    stream.close()
    thread = threading.Thread(target=try_lock)
    thread.start()
    thread.join(timeout=2)
    assert acquired == [False, True]


@pytest.mark.parametrize("failure", ["cancel", "timeout", "exit", "assign"])
def test_owned_process_tree_cleanup(monkeypatch, tmp_path, failure):
    """No real child launched: test gate assignment ordering and tree cleanup on every exit."""
    events = []

    class Pipe:
        closed = False

        def write(self, data):
            events.append("gate")

        def flush(self):
            pass

        def close(self):
            self.closed = True

    process = Mock(pid=123, stdin=Pipe(), returncode=4 if failure == "exit" else None)
    process.poll.return_value = 4 if failure == "exit" else None
    process.wait.return_value = 0
    monkeypatch.setattr(visual.os, "name", "nt")
    monkeypatch.setattr(visual.subprocess, "CREATE_NEW_PROCESS_GROUP", 512, raising=False)
    monkeypatch.setattr(visual.subprocess, "Popen", Mock(return_value=process))

    class Job:
        def __init__(self, pid):
            events.append("assign")
            if failure == "assign":
                raise OSError("cannot assign")

        def close(self):
            events.append("kill-tree")

    monkeypatch.setattr(visual, "_WindowsJob", Job)
    polls = iter([False, failure == "cancel", True])
    with pytest.raises((VisualError, TimeoutError, OSError)):
        visual._run_process(["worker"], cwd=tmp_path, env={"ECHOCRADLE_VISUAL_PYTHON": "py"},
                            timeout=0 if failure == "timeout" else 1,
                            should_stop=lambda: next(polls, True))
    if failure == "assign":
        assert events == ["assign"]
        process.kill.assert_called_once()
    else:
        assert events == ["assign", "gate", "kill-tree"]
    process.wait.assert_called()


@pytest.mark.parametrize("failure", ["write", "flush", "close", "assign", "job"])
def test_process_teardown_errors_still_close_job_and_reap(monkeypatch, tmp_path, failure):
    events = []

    class Pipe:
        closed = False

        def write(self, data):
            if failure == "write":
                raise BrokenPipeError("gate write failed")

        def flush(self):
            if failure == "flush":
                raise BrokenPipeError("gate flush failed")

        def close(self):
            events.append("close-pipe")
            # Buffered close can raise while flushing, including during teardown.
            raise BrokenPipeError("gate close failed")

    process = Mock(pid=123, stdin=Pipe())
    process.poll.return_value = None
    process.kill.side_effect = lambda: events.append("kill")
    process.wait.side_effect = lambda **kwargs: events.append("wait")
    monkeypatch.setattr(visual.os, "name", "nt")
    monkeypatch.setattr(visual.subprocess, "CREATE_NEW_PROCESS_GROUP", 512, raising=False)
    monkeypatch.setattr(visual.subprocess, "Popen", Mock(return_value=process))

    class Job:
        def __init__(self, pid):
            if failure == "assign":
                raise OSError("cannot assign")

        def close(self):
            events.append("close-job")
            if failure == "job":
                raise OSError("cannot close job")

    monkeypatch.setattr(visual, "_WindowsJob", Job)
    message = {"write": "write", "flush": "flush", "close": "close",
               "assign": "assign", "job": "close job"}[failure]
    with pytest.raises(OSError, match=message):
        visual._run_process(["worker"], cwd=tmp_path,
                            env={"ECHOCRADLE_VISUAL_PYTHON": "py"},
                            timeout=1, should_stop=None)
    assert events[-2:] == (["kill", "wait"] if failure == "assign" else ["close-job", "wait"])
    process.wait.assert_called_once_with(timeout=10)