"""Cached experimental 0110 workers; no inference libraries enter this interpreter.

``availability()`` returns per-stage ``{available: bool, reasons: list[str]}``.
Configuration is trusted, server-owned automatic.local.json, not request data.
"""
from __future__ import annotations

import json
import math
import os
import re
import shutil
import signal
import struct
import subprocess
import tempfile
import threading
import time
import zlib
from collections.abc import Callable
from contextlib import ExitStack
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

MAX_VISUAL_BYTES = 32 * 1024 * 1024
Stop = Callable[[], bool] | None


class VisualError(RuntimeError):
    """Deployment or worker failure (never a successful artifact)."""


class VisualCancelled(VisualError):
    """The caller cancelled before publication and cleanup has finished."""


def _json(data: bytes) -> Any:
    def pairs(items: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in items:
            if key in result:
                raise ValueError("Duplicate JSON key")
            result[key] = value
        return result

    def constant(value: str) -> None:
        raise ValueError(f"Non-finite JSON value: {value}")

    def number(value: str) -> float:
        result = float(value)
        if not math.isfinite(result):
            raise ValueError("Non-finite JSON number")
        return result

    try:
        return json.loads(data.decode("utf-8"), object_pairs_hook=pairs,
                          parse_constant=constant, parse_float=number)
    except (UnicodeError, RecursionError, OverflowError) as exc:
        raise ValueError("Invalid visual JSON") from exc


def validate_visual(data: bytes, media_type: str) -> None:
    """Validate a <=32 MiB PNG or self-contained GLB; raise ValueError on failure.

    Container/resource validation, not a full glTF semantic or native-import safety proof.
    GLB resources must use the BIN chunk, never URI/data URI/extension resources.
    Embedded images must be PNG, matching the experiment's exported textures.
    PNG CRCs, chunk ordering, bounded zlib stream and scanline filters are checked.
    """
    if not isinstance(data, bytes) or not 0 < len(data) <= MAX_VISUAL_BYTES:
        raise ValueError("Visual payload must be 1..32 MiB")
    if media_type == "image/png":
        _validate_png(data)
    elif media_type == "model/gltf-binary":
        _glb(data)
    else:
        raise ValueError("Unsupported visual media type")


def _validate_png(data: bytes) -> None:
    if not data.startswith(b"\x89PNG\r\n\x1a\n"):
        raise ValueError("Invalid PNG signature")
    offset, kinds, compressed = 8, [], bytearray()
    width = height = depth = colour = interlace = 0
    palette = 0
    while offset < len(data):
        if offset + 12 > len(data):
            raise ValueError("Truncated PNG chunk")
        size, kind = struct.unpack_from(">I4s", data, offset)
        end = offset + 12 + size
        if end > len(data) or not re.fullmatch(b"[A-Za-z]{4}", kind) or kind[2] & 32:
            raise ValueError("Invalid PNG chunk")
        payload = data[offset + 8:end - 4]
        crc = struct.unpack_from(">I", data, end - 4)[0]
        if zlib.crc32(kind + payload) & 0xFFFFFFFF != crc:
            raise ValueError("PNG CRC mismatch")
        if not kinds and kind != b"IHDR":
            raise ValueError("PNG must start with IHDR")
        if kind == b"IHDR":
            if kinds or size != 13:
                raise ValueError("Invalid PNG IHDR")
            width, height, depth, colour, compression, filtering, interlace = struct.unpack(
                ">IIBBBBB", payload
            )
            depths = {0: (1, 2, 4, 8, 16), 2: (8, 16), 3: (1, 2, 4, 8),
                      4: (8, 16), 6: (8, 16)}
            if (not 1 <= width <= 8192 or not 1 <= height <= 8192
                    or width * height > 16_777_216 or depth not in depths.get(colour, ())
                    or compression or filtering or interlace not in (0, 1)):
                raise ValueError("Unsupported PNG IHDR")
        elif kind == b"PLTE":
            if (b"PLTE" in kinds or b"IDAT" in kinds or colour in (0, 4)
                    or not 3 <= size <= 768 or size % 3):
                raise ValueError("Invalid PNG palette")
            palette = size // 3
            if colour == 3 and palette > 2 ** depth:
                raise ValueError("PNG palette exceeds bit depth")
        elif kind == b"IDAT":
            if (b"IDAT" in kinds and kinds[-1] != b"IDAT") or (colour == 3 and not palette):
                raise ValueError("Invalid PNG IDAT order")
            compressed.extend(payload)
        elif kind == b"IEND":
            if size or b"IDAT" not in kinds or end != len(data):
                raise ValueError("Invalid PNG end")
        elif kind in (b"acTL", b"fcTL", b"fdAT") or not kind[0] & 32:
            raise ValueError("Unsupported PNG critical/animation chunk")
        kinds.append(kind)
        offset = end
    if not kinds or kinds[-1] != b"IEND":
        raise ValueError("Missing PNG IEND")
    channels = {0: 1, 2: 3, 3: 1, 4: 2, 6: 4}[colour]
    passes = [(0, 0, 1, 1)] if not interlace else [
        (0, 0, 8, 8), (4, 0, 8, 8), (0, 4, 4, 8), (2, 0, 4, 4),
        (0, 2, 2, 4), (1, 0, 2, 2), (0, 1, 1, 2),
    ]
    rows = []
    for x, y, dx, dy in passes:
        w, h = max(0, (width - x + dx - 1) // dx), max(0, (height - y + dy - 1) // dy)
        if w and h:
            rows.extend([1 + (w * channels * depth + 7) // 8] * h)
    expected = sum(rows)
    if expected > 128 * 1024 * 1024:
        raise ValueError("PNG decompression limit")
    try:
        stream = zlib.decompressobj()
        raw = stream.decompress(compressed, expected + 1)
        if (len(raw) != expected or not stream.eof or stream.unused_data
                or stream.unconsumed_tail):
            raise ValueError("Invalid PNG zlib stream")
    except zlib.error as exc:
        raise ValueError("Invalid PNG compression") from exc
    offset = 0
    for size in rows:
        if raw[offset] > 4:
            raise ValueError("Invalid PNG scanline filter")
        offset += size


def _glb(data: bytes) -> dict[str, Any]:
    if len(data) < 20 or struct.unpack_from("<4sII", data) != (b"glTF", 2, len(data)):
        raise ValueError("Invalid GLB header")
    chunks: list[tuple[bytes, bytes]] = []
    offset = 12
    while offset < len(data):
        if offset + 8 > len(data):
            raise ValueError("Truncated GLB chunk")
        size, kind = struct.unpack_from("<I4s", data, offset)
        if size % 4 or offset + 8 + size > len(data):
            raise ValueError("Invalid GLB chunk length")
        chunks.append((kind, data[offset + 8:offset + 8 + size]))
        offset += size + 8
    if [kind for kind, _ in chunks] not in ([b"JSON"], [b"JSON", b"BIN\0"]):
        raise ValueError("GLB requires JSON then optional BIN")
    doc = _json(chunks[0][1])
    if not isinstance(doc, dict) or not isinstance(doc.get("asset"), dict):
        raise ValueError("Missing glTF asset")
    if doc["asset"].get("version") != "2.0":
        raise ValueError("Unsupported glTF version")

    def resources(value: Any, depth: int = 0) -> None:
        if depth > 64:
            raise ValueError("GLB nesting limit")
        if isinstance(value, dict):
            # Reject extension resource loaders as well as standard external references.
            if "uri" in value or value.get("extensionsRequired"):
                raise ValueError("GLB must be self-contained without required extensions")
            for key, child in value.items():
                if key == "extensions" and child:
                    allowed = {"KHR_materials_unlit", "KHR_materials_specular",
                               "KHR_materials_ior", "KHR_texture_transform",
                               "KHR_materials_emissive_strength"}
                    if not isinstance(child, dict) or set(child) - allowed:
                        raise ValueError("Unsupported GLB extension")
                resources(child, depth + 1)
        elif isinstance(value, list):
            for child in value:
                resources(child, depth + 1)

    resources(doc)
    binary = chunks[1][1] if len(chunks) == 2 else b""
    buffers = doc.get("buffers", [])
    if not isinstance(buffers, list) or len(buffers) > 1:
        raise ValueError("Invalid GLB buffers")
    length = 0
    if buffers:
        if not isinstance(buffers[0], dict):
            raise ValueError("Invalid GLB buffer")
        length = buffers[0].get("byteLength")
        if type(length) is not int or length <= 0 or not length <= len(binary) <= length + 3:
            raise ValueError("GLB BIN length mismatch")
    elif binary:
        raise ValueError("Unreferenced GLB BIN")
    views = doc.get("bufferViews", [])
    if not isinstance(views, list):
        raise ValueError("Invalid GLB bufferViews")
    for view in views:
        if not isinstance(view, dict):
            raise ValueError("Invalid GLB bufferView")
        start, size = view.get("byteOffset", 0), view.get("byteLength")
        if (type(view.get("buffer")) is not int or view["buffer"] != 0 or not buffers
                or type(start) is not int or type(size) is not int
                or start < 0 or size <= 0 or start + size > length):
            raise ValueError("GLB bufferView out of bounds")
    images = doc.get("images", [])
    if not isinstance(images, list):
        raise ValueError("Invalid GLB images")
    for image in images:
        if (not isinstance(image, dict) or type(image.get("bufferView")) is not int
                or not 0 <= image["bufferView"] < len(views)
                or image.get("mimeType") != "image/png"):
            raise ValueError("GLB image must be embedded PNG")
        view = views[image["bufferView"]]
        start = view.get("byteOffset", 0)
        _validate_png(binary[start:start + view["byteLength"]])
    return doc


def _read_visual(path: Path, media_type: str) -> bytes:
    with path.open("rb") as stream:
        data = stream.read(MAX_VISUAL_BYTES + 1)
    validate_visual(data, media_type)
    return data


def _stop(should_stop: Stop) -> None:
    if should_stop is not None and should_stop():
        raise VisualCancelled("Visual generation cancelled")


class _WindowsJob:
    """Kill-on-close Job Object: includes SkinTokens' new-process-group child.

    The stdlib launcher waits for a pipe gate, so assignment precedes any child spawn.
    Assignment failure fails closed, before the gate is released.
    """

    def __init__(self, pid: int) -> None:
        import ctypes
        from ctypes import wintypes

        class Basic(ctypes.Structure):
            _fields_ = [("process_time", ctypes.c_int64), ("job_time", ctypes.c_int64),
                        ("flags", wintypes.DWORD), ("min_ws", ctypes.c_size_t),
                        ("max_ws", ctypes.c_size_t), ("active", wintypes.DWORD),
                        ("affinity", ctypes.c_size_t), ("priority", wintypes.DWORD),
                        ("scheduling", wintypes.DWORD)]

        class IO(ctypes.Structure):
            _fields_ = [(name, ctypes.c_uint64) for name in
                        ("read_ops", "write_ops", "other_ops", "read", "write", "other")]

        class Extended(ctypes.Structure):
            _fields_ = [("basic", Basic), ("io", IO), ("process_memory", ctypes.c_size_t),
                        ("job_memory", ctypes.c_size_t), ("peak_process", ctypes.c_size_t),
                        ("peak_job", ctypes.c_size_t)]

        api = ctypes.WinDLL("kernel32", use_last_error=True)
        for name, args, result in (
            ("CreateJobObjectW", [ctypes.c_void_p, wintypes.LPCWSTR], wintypes.HANDLE),
            ("SetInformationJobObject", [wintypes.HANDLE, ctypes.c_int,
                                         ctypes.c_void_p, wintypes.DWORD], wintypes.BOOL),
            ("OpenProcess", [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD], wintypes.HANDLE),
            ("AssignProcessToJobObject", [wintypes.HANDLE, wintypes.HANDLE], wintypes.BOOL),
            ("TerminateJobObject", [wintypes.HANDLE, wintypes.UINT], wintypes.BOOL),
            ("QueryInformationJobObject", [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p,
                                           wintypes.DWORD, ctypes.c_void_p], wintypes.BOOL),
            ("CloseHandle", [wintypes.HANDLE], wintypes.BOOL),
        ):
            function = getattr(api, name)
            function.argtypes, function.restype = args, result
        self.api = api
        self.handle = api.CreateJobObjectW(None, None)
        if not self.handle:
            raise ctypes.WinError(ctypes.get_last_error())
        info = Extended()
        info.basic.flags = 0x2000  # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        process = api.OpenProcess(0x0100 | 0x0001, False, pid)
        try:
            if (not process or not api.SetInformationJobObject(
                    self.handle, 9, ctypes.byref(info), ctypes.sizeof(info))
                    or not api.AssignProcessToJobObject(self.handle, process)):
                raise ctypes.WinError(ctypes.get_last_error())
        except BaseException:
            self.close()
            raise
        finally:
            if process:
                api.CloseHandle(process)

    def close(self) -> None:
        if self.handle:
            import ctypes
            from ctypes import wintypes

            class Accounting(ctypes.Structure):
                _fields_ = [(name, ctypes.c_int64) for name in
                            ("user", "kernel", "period_user", "period_kernel")] + [
                                (name, wintypes.DWORD) for name in
                                ("faults", "total", "active", "terminated")]

            try:
                if not self.api.TerminateJobObject(self.handle, 1):
                    raise ctypes.WinError(ctypes.get_last_error())
                deadline = time.monotonic() + 10
                tick = threading.Event()
                while True:
                    accounting = Accounting()
                    if not self.api.QueryInformationJobObject(
                            self.handle, 1, ctypes.byref(accounting),
                            ctypes.sizeof(accounting), None):
                        raise ctypes.WinError(ctypes.get_last_error())
                    if accounting.active == 0:
                        break
                    if time.monotonic() >= deadline:
                        raise VisualError("Visual process tree did not terminate")
                    tick.wait(0.02)
            finally:
                self.api.CloseHandle(self.handle)
                self.handle = None


def _run_process(command: list[str], *, cwd: Path, env: dict[str, str],
                 timeout: float, should_stop: Stop) -> None:
    """Optionally stream worker output to trusted, server-owned debug files."""
    with ExitStack() as files:
        log = env.get("ECHOCRADLE_VISUAL_LOG")
        stdout = files.enter_context(Path(f"{log}.stdout.log").open("wb")) if log else None
        stderr = files.enter_context(Path(f"{log}.stderr.log").open("wb")) if log else None
        _run_worker(command, cwd=cwd, env=env, timeout=timeout, should_stop=should_stop,
                    stdout=stdout, stderr=stderr)


def _run_worker(command: list[str], *, cwd: Path, env: dict[str, str],
                timeout: float, should_stop: Stop, stdout: Any, stderr: Any) -> None:
    """Run a gated process tree; no shell, captured pipes, downloads or detached children."""
    _stop(should_stop)
    launcher = Path(__file__).with_name("visual_workers") / "launch.py"
    # The configured worker interpreter is used even for the stdlib-only launcher.
    args = [env["ECHOCRADLE_VISUAL_PYTHON"], str(launcher), *command]
    flags = {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP} if os.name == "nt" else {
        "start_new_session": True
    }
    process = subprocess.Popen(args, cwd=cwd, env=env, stdin=subprocess.PIPE,
                               stdout=stdout if stdout is not None else subprocess.DEVNULL,
                               stderr=stderr if stderr is not None else subprocess.DEVNULL,
                               **flags)
    job = None
    try:
        if os.name == "nt":
            job = _WindowsJob(process.pid)
        assert process.stdin is not None
        process.stdin.write(b"go\n")
        process.stdin.flush()
        process.stdin.close()
        deadline = time.monotonic() + timeout
        while process.poll() is None:
            _stop(should_stop)
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError("Visual worker deadline exceeded")
            try:
                process.wait(timeout=min(0.1, remaining))
            except subprocess.TimeoutExpired:
                pass
        _stop(should_stop)
        if process.returncode:
            raise VisualError(f"Visual worker failed with exit code {process.returncode}")
    finally:
        try:
            if process.stdin is not None and not process.stdin.closed:
                try:
                    process.stdin.close()
                except OSError:
                    # Buffered close may flush into an exited launcher. Preserve the
                    # original failure, but always terminate the tree and reap below.
                    pass
        finally:
            try:
                if job is not None:
                    job.close()  # Wait for descendants before scratch removal, even on success.
                elif os.name != "nt":
                    try:
                        os.killpg(process.pid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass
                elif process.poll() is None:
                    process.kill()  # Job assignment failed; gate was never released.
            finally:
                try:
                    process.wait(timeout=10)
                except subprocess.TimeoutExpired as exc:
                    raise VisualError(
                        "Worker tree did not terminate; refusing publication"
                    ) from exc


class VisualBackend:
    """Blocking cached-only 0110 stages. Missing deployment is diagnostic, not startup failure."""

    def __init__(self, config: Path, *, shippable_only: bool = False) -> None:
        self.config_path = Path(config).resolve()
        self.source = self.config_path.parent / "src"
        self.shippable_only = shippable_only
        self._lock = threading.RLock()
        self._config: dict[str, Any] = {}
        self._config_error = ""
        try:
            config_data = _json(self.config_path.read_bytes())
            if not isinstance(config_data, dict):
                raise ValueError("Visual config must be an object")
            self._config = config_data
            for key in ("python", "blender", "image_model", "tripo_model", "tripo_checkout",
                        "rig_checkout", "rig_checkpoint", "hf_home", "foreground_model",
                        "motion_checkout", "motion_models", "bvh_addon", "animation_spec"):
                value = self._config.get(key)
                if isinstance(value, str) and value:
                    path = Path(value)
                    self._config[key] = str((self.config_path.parent / path).resolve())
        except (OSError, ValueError) as exc:
            self._config_error = str(exc)

    def _path(self, key: str) -> Path:
        value = self._config.get(key)
        if not isinstance(value, str) or not value:
            raise ValueError(f"Missing configured path: {key}")
        return Path(value)

    def _clips(self) -> list[dict[str, Any]]:
        doc = _json(self._path("animation_spec").read_bytes())
        clips = doc.get("clips") if isinstance(doc, dict) else None
        if not isinstance(clips, list) or len(clips) != 5:
            raise ValueError("Expected the experiment's five motion clips")
        names = set()
        for clip in clips:
            if not isinstance(clip, dict):
                raise ValueError("Invalid motion clip")
            name = clip.get("id")
            if (not isinstance(name, str) or not re.fullmatch(r"[A-Za-z][A-Za-z0-9_]{0,63}", name)
                    or name.casefold() in names):
                raise ValueError("Invalid/duplicate motion ID")
            names.add(name.casefold())
            if (not isinstance(clip.get("prompt"), str) or not 1 <= len(clip["prompt"]) <= 1000
                    or type(clip.get("frames")) is not int or not 4 <= clip["frames"] <= 196
                    or clip["frames"] % 4 or type(clip.get("fps")) is not int
                    or clip["fps"] != 20 or type(clip.get("seed")) is not int
                    or not 0 <= clip["seed"] < 2 ** 32
                    or clip.get("root_motion") not in ("in_place", "travel")
                    or type(clip.get("loop")) is not bool):
                raise ValueError("Invalid motion specification")
        return clips

    def _deployment(self, stage: str) -> None:
        if self.shippable_only:
            raise ValueError("Experimental visual stages forbidden in shippable_only mode")
        if self._config_error:
            raise ValueError(self._config_error)
        timeout = self._config.get("timeout_seconds", 600)
        if type(timeout) is not int or not 1 <= timeout <= 1800:
            raise ValueError("timeout_seconds must be 1..1800 (whole stage budget)")
        files = [self._path("python"),
                 Path(__file__).with_name("visual_workers") / "launch.py",
                 Path(__file__).with_name("visual_workers") / "sitecustomize.py"]
        directories = [self._path("hf_home"), self._path("foreground_model")]
        checkouts = []
        if stage == "image":
            model = self._path("image_model")
            files += [self.source / "image_worker.py", model / "model_index.json",
                      model / "scheduler/scheduler_config.json", model / "tokenizer/vocab.json",
                      model / "tokenizer/merges.txt", model / "tokenizer/tokenizer_config.json",
                      model / "unet/config.json", model / "vae/config.json",
                      model / "text_encoder/config.json",
                      model / "unet/diffusion_pytorch_model.fp16.safetensors",
                      model / "vae/diffusion_pytorch_model.fp16.safetensors",
                      model / "text_encoder/model.fp16.safetensors"]
        elif stage == "image_to_3d":
            model = self._path("tripo_model")
            files += [self.source / "triposr_worker.py", model / "config.yaml",
                      model / "model.ckpt", self._path("foreground_model") / "u2netp.onnx",
                      Path(__file__).with_name("visual_workers") / "seed_worker.py"]
            checkouts = ["tripo"]
            cache = self._path("hf_home") / "hub/models--facebook--dino-vitb16/snapshots"
            # TripoSR constructs ViT from config; its own checkpoint contains DINO weights.
            if not any((p / "config.json").is_file() for p in cache.glob("*")):
                raise ValueError("Missing cached TripoSR DINO dependency")
        elif stage == "animate_3d":
            files += [self._path("blender"), self._path("rig_checkpoint"),
                      *[self.source / name for name in (
                          "rig_worker.py", "inspect_render.py", "texture_bake.py",
                          "motion_worker.py", "retarget_motion.py", "validate_playback.py",
                          "verify_result.py")],
                      self._path("rig_checkout") / "demo.py",
                      self._path("rig_checkout") / "bpy_server.py",
                      self._path("rig_checkout") / "experiments/skin_vae_2_10_32768/last.ckpt",
                      self._path("rig_checkout") / "models/Qwen3-0.6B/config.json",
                      self._path("bvh_addon") / "io_anim_bvh/__init__.py"]
            models = self._path("motion_models")
            files += [models / "ViT-B-32.pt"]
            for name, checkpoint in (
                ("rvq_nq6_dc512_nc512_noshare_qdp0.2", "net_best_fid.tar"),
                ("t2m_nlayer8_nhead6_ld384_ff1024_cdp0.1_rvq6ns", "latest.tar"),
                ("tres_nlayer8_ld384_ff1024_rvq6ns_cdp0.2_sw", "net_best_fid.tar"),
            ):
                files += [models / "t2m" / name / "opt.txt",
                          models / "t2m" / name / "model" / checkpoint]
            vq = models / "t2m/rvq_nq6_dc512_nc512_noshare_qdp0.2/meta"
            files += [vq / "mean.npy", vq / "std.npy"]
            directories += [self._path("hf_home")]
            checkouts = ["rig", "motion"]
            if self._config.get("texture_resolution", 1024) not in (512, 1024, 2048):
                raise ValueError("Unsupported texture_resolution")
            self._clips()
        else:
            raise ValueError("Unknown visual stage")
        for path in files:
            if not path.is_file() or path.stat().st_size == 0:
                raise ValueError(f"Missing provisioned file: {path}")
        for path in directories:
            if not path.is_dir():
                raise ValueError(f"Missing provisioned directory: {path}")
        for key in checkouts:
            checkout = self._path(f"{key}_checkout")
            revision = self._config.get(f"{key}_revision")
            if not isinstance(revision, str) or not re.fullmatch(r"[0-9a-f]{40}", revision):
                raise ValueError(f"Missing pinned revision: {key}")
            actual = subprocess.check_output(
                ["git", "-C", str(checkout), "rev-parse", "HEAD"], timeout=10,
                text=True, stderr=subprocess.DEVNULL,
            ).strip()
            dirty = subprocess.check_output(
                ["git", "-C", str(checkout), "status", "--porcelain", "--untracked-files=no"],
                timeout=10, text=True, stderr=subprocess.DEVNULL,
            ).strip()
            if actual != revision or dirty:
                raise ValueError(f"Pinned source mismatch or tracked modifications: {key}")

    def availability(self) -> dict[str, dict[str, Any]]:
        """Non-inferencing deployment checks. Does not certify CUDA/ABI or model licenses."""
        result = {}
        for stage in ("image", "image_to_3d", "animate_3d"):
            try:
                self._deployment(stage)
            except (OSError, ValueError, subprocess.SubprocessError) as exc:
                result[stage] = {"available": False, "reasons": [str(exc)]}
            else:
                result[stage] = {"available": True, "reasons": []}
        return result

    def _environment(self, scratch: Path, seed: int) -> dict[str, str]:
        env = dict(os.environ)
        for key in ("PYTHONPATH", "PYTHONHOME", "HF_TOKEN", "HUGGING_FACE_HUB_TOKEN",
                    "ECHOCRADLE_VISUAL_LOG"):
            env.pop(key, None)
        for key in list(env):
            if key.lower() in ("http_proxy", "https_proxy", "all_proxy"):
                env.pop(key)
        env.update(HF_HUB_OFFLINE="1", TRANSFORMERS_OFFLINE="1", DIFFUSERS_OFFLINE="1",
                   HF_HUB_DISABLE_TELEMETRY="1", HF_HUB_DISABLE_IMPLICIT_TOKEN="1",
                   DO_NOT_TRACK="1", GRADIO_ANALYTICS_ENABLED="False", WANDB_MODE="disabled",
                   NO_PROXY="localhost,127.0.0.1,::1",
                   HF_HOME=str(self._path("hf_home")),
                   U2NET_HOME=str(self._path("foreground_model")),
                   PYTHONNOUSERSITE="1", PYTHONHASHSEED=str(seed),
                   ECHOCRADLE_VISUAL_PYTHON=str(self._path("python")),
                   PYTHONPATH=str(Path(__file__).with_name("visual_workers")),
                   TMP=str(scratch), TEMP=str(scratch), TMPDIR=str(scratch))
        return env

    def _execute(self, stage: str, output: Path, seed: int, should_stop: Stop,
                 build: Callable[[Path, Callable[..., None]], Path]) -> Path:
        if type(seed) is not int or not 0 <= seed < 2 ** 32:
            raise ValueError("seed must be an unsigned 32-bit integer")
        with self._lock:
            _stop(should_stop)
            try:
                self._deployment(stage)
            except (OSError, ValueError, subprocess.SubprocessError) as exc:
                raise VisualError(f"Visual stage unavailable: {exc}") from exc
            output = Path(output).absolute()
            output.parent.mkdir(parents=True, exist_ok=True)
            with tempfile.TemporaryDirectory(prefix=".visual-", dir=output.parent) as folder:
                scratch = Path(folder).resolve()
                debug = None
                debug_root = os.environ.get("ECHOCRADLE_VISUAL_DEBUG_DIR")
                if debug_root:
                    root = Path(debug_root).resolve()
                    root.mkdir(parents=True, exist_ok=True)
                    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S.%fZ")
                    debug = Path(tempfile.mkdtemp(prefix=f"{stamp}-{stage}-", dir=root))
                env = self._environment(scratch, seed)
                deadline = time.monotonic() + self._config.get("timeout_seconds", 600)
                worker_count = 0

                def run(command: list[str], cwd: Path | None = None) -> None:
                    nonlocal worker_count
                    _stop(should_stop)
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        raise TimeoutError("Visual stage deadline exceeded")
                    worker_count += 1
                    worker_env = env
                    if debug is not None:
                        worker_env = dict(env, PYTHONUNBUFFERED="1")
                        worker_env["ECHOCRADLE_VISUAL_LOG"] = str(
                            debug / f"worker-{worker_count:03d}"
                        )
                    _run_process(command, cwd=cwd or scratch, env=worker_env, timeout=remaining,
                                 should_stop=should_stop)

                status, error = "failed", None
                try:
                    artifact = build(scratch, run)
                    media = "image/png" if stage == "image" else "model/gltf-binary"
                    _read_visual(artifact, media)
                    _stop(should_stop)
                    if time.monotonic() >= deadline:
                        raise TimeoutError("Visual stage deadline exceeded")
                    if debug is not None:
                        # Preserve the final artifact too, before atomic publication moves it.
                        shutil.copytree(scratch, debug / "scratch")
                    artifact.replace(output)
                    status = "success"
                except BaseException as exc:
                    status = "cancelled" if isinstance(exc, VisualCancelled) else "failed"
                    # Never serialize commands, environment, exception text or tracebacks:
                    # these can contain credentials. Actual worker failures are in the logs.
                    error = {"type": type(exc).__name__}
                    raise
                finally:
                    if debug is not None:
                        try:
                            if status != "success":
                                shutil.copytree(scratch, debug / "scratch", dirs_exist_ok=True)
                        finally:
                            manifest = dict(stage=stage, seed=seed, status=status, error=error,
                                            workers=worker_count)
                            (debug / "manifest.json").write_text(
                                json.dumps(manifest, indent=2) + "\n", encoding="utf-8"
                            )
            return output

    def generate_image(self, text: str, output: Path, *, seed: int,
                       negative_prompt: str = "", should_stop: Stop = None) -> Path:
        if not isinstance(text, str) or not 1 <= len(text) <= 24576:
            raise ValueError("text must contain 1..24576 characters")
        if not isinstance(negative_prompt, str) or len(negative_prompt) > 24576:
            raise ValueError("negative_prompt must contain at most 24576 characters")

        def build(scratch: Path, run: Callable[..., None]) -> Path:
            spec = scratch / "spec.json"
            spec.write_text(json.dumps(dict(prompt=text, negative_prompt=negative_prompt,
                                           seed=seed, width=512, height=512, steps=35,
                                           guidance=8.0)), encoding="utf-8")
            result = scratch / "reference.png"
            run([str(self._path("python")), str(self.source / "image_worker.py"),
                 "--spec", str(spec), "--model", str(self._path("image_model")),
                 "--output", str(result)])
            return result

        return self._execute("image", output, seed, should_stop, build)

    def generate_3d(self, image: Path, output: Path, *, seed: int,
                    should_stop: Stop = None) -> Path:
        def build(scratch: Path, run: Callable[..., None]) -> Path:
            source = scratch / "input.png"
            source.write_bytes(_read_visual(Path(image), "image/png"))
            result = scratch / "mesh.glb"
            seeded = Path(__file__).with_name("visual_workers") / "seed_worker.py"
            run([str(self._path("python")), str(seeded), str(seed),
                 str(self.source / "triposr_worker.py"),
                 "--checkout", str(self._path("tripo_checkout")), "--model",
                 str(self._path("tripo_model")), "--input", str(source), "--output", str(result)])
            return result

        return self._execute("image_to_3d", output, seed, should_stop, build)

    def animate_3d(self, mesh: Path, output: Path, *, seed: int,
                   should_stop: Stop = None) -> Path:
        def build(scratch: Path, run: Callable[..., None]) -> Path:
            source = scratch / "input.glb"
            source.write_bytes(_read_visual(Path(mesh), "model/gltf-binary"))
            py = str(self._path("python"))
            rigged = scratch / "rigged.glb"
            run([py, str(self.source / "rig_worker.py"), "--checkout",
                 str(self._path("rig_checkout")), "--checkpoint", str(self._path("rig_checkpoint")),
                 "--input", str(source), "--output", str(rigged), "--seed", str(seed)],
                self._path("rig_checkout"))
            _read_visual(rigged, "model/gltf-binary")
            prepared = scratch / "prepared"
            prepared.mkdir()
            spec = scratch / "spec.json"
            spec.write_text(json.dumps({"id": "visual", "height_m": 1.7}), encoding="utf-8")
            blender = [str(self._path("blender")), "--background", "--factory-startup",
                       "--python-exit-code", "1", "--python"]
            run([*blender, str(self.source / "inspect_render.py"), "--", "--input", str(rigged),
                 "--output", str(prepared), "--spec", str(spec), "--source-up", "triposr-z",
                 "--texture-resolution", str(self._config.get("texture_resolution", 1024))])
            character = prepared / "SK_visual.glb"
            _read_visual(character, "model/gltf-binary")
            clips = self._clips()
            motions = []
            for index, clip in enumerate(clips):
                directory = scratch / "motions" / clip["id"]
                directory.mkdir(parents=True)
                request = directory / "spec.json"
                clip = dict(clip, seed=(seed + index) % (2 ** 32))
                request.write_text(json.dumps(clip), encoding="utf-8")
                run([py, str(self.source / "motion_worker.py"), "--checkout",
                     str(self._path("motion_checkout")), "--models",
                     str(self._path("motion_models")),
                     "--spec", str(request), "--output", str(directory)])
                bvh = directory / "motion.bvh"
                if not bvh.is_file() or not bvh.stat().st_size:
                    raise VisualError("Motion worker did not produce BVH")
                motions.append(dict(clip, bvh=str(bvh)))
            motion_set = scratch / "motion_set.json"
            motion_set.write_text(json.dumps(motions), encoding="utf-8")
            run([*blender, str(self.source / "retarget_motion.py"), "--", "--character",
                 str(character), "--motion-set", str(motion_set), "--asset-id", "visual",
                 "--bvh-addon", str(self._path("bvh_addon")), "--output", str(scratch)])
            result = scratch / "SK_visual.glb"
            data = _read_visual(result, "model/gltf-binary")
            self._validate_animation(_glb(data), [clip["id"] for clip in clips])
            report = scratch / "playback.json"
            run([*blender, str(self.source / "validate_playback.py"), "--", "--input",
                 str(result), "--clips", str(motion_set), "--report", str(report)])
            if not report.is_file():
                raise VisualError("Playback validation did not produce a report")
            return result

        return self._execute("animate_3d", output, seed, should_stop, build)

    @staticmethod
    def _validate_animation(doc: dict[str, Any], names: list[str]) -> None:
        primitives = [p for mesh in doc.get("meshes", []) for p in mesh.get("primitives", [])]
        animations = doc.get("animations", [])
        if (len(doc.get("skins", [])) != 1 or not primitives
                or sorted(a.get("name", "") for a in animations) != sorted(names)
                or any(not a.get("channels") for a in animations)):
            raise VisualError("Missing learned rig or expected animation clips")
        for primitive in primitives:
            if not {"JOINTS_0", "WEIGHTS_0", "TEXCOORD_0"} <= set(primitive.get("attributes", {})):
                raise VisualError("Missing skin weights or texture coordinates")
            try:
                material = doc["materials"][primitive["material"]]
                texture = material["pbrMetallicRoughness"]["baseColorTexture"]["index"]
                image = doc["images"][doc["textures"][texture]["source"]]
                if "bufferView" not in image:
                    raise ValueError("Missing embedded texture")
            except (KeyError, IndexError, TypeError, ValueError) as exc:
                raise VisualError("Missing embedded base-colour texture") from exc