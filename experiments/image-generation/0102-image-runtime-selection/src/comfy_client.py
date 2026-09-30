#!/usr/bin/env python3
"""Submit a minimal text-to-image workflow to a running ComfyUI server.

Stdlib only (urllib) -- no pip install, no torch. This is the reference
implementation for option B in experiment 0102: drive an out-of-process
ComfyUI over its local HTTP API, matching the project's MCP config at
http://127.0.0.1:8188 (see .vscode/mcp.json -> servers.comfyui).

Routes used (verified against ComfyUI server.py):
  POST /prompt          {"prompt": <api-graph>, "client_id": <str>}
                        -> {"prompt_id": ..., "number": ..., "node_errors": {}}
  GET  /history/{id}    -> {prompt_id: {"outputs": {...}, "status": {...}}}
  GET  /view            -> the rendered PNG bytes
  GET  /system_stats    -> VRAM/RAM probe (vram_total / vram_free)
  GET  /models/checkpoints
  POST /free            {"unload_models": true, "free_memory": true}

Note on ComfyUI's origin middleware: the server rejects cross-site POSTs when
the Host and Origin headers disagree. urllib does not send an Origin header at
all, so this client is unaffected -- do not add one.

Usage (PowerShell):
  python comfy_client.py --prompt "gilded iron axe icon, game item" --seed 1234
  python comfy_client.py --stats
  python comfy_client.py --release-only
"""

from __future__ import annotations

import argparse
import json
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from pathlib import Path
from typing import Any

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8188
DEFAULT_CHECKPOINT = "v1-5-pruned-emaonly.safetensors"


def base_url(host: str, port: int) -> str:
    """Return the root URL, tolerating a host that already carries a scheme."""
    if host.startswith(("http://", "https://")):
        return f"{host.rstrip('/')}:{port}" if ":" not in host.split("//", 1)[1] else host
    return f"http://{host}:{port}"


def request_json(
    url: str,
    payload: dict[str, Any] | None = None,
    method: str = "GET",
    timeout: float = 30.0,
) -> Any:
    """GET or POST a JSON endpoint and decode the JSON response."""
    data = None
    headers = {"Accept": "application/json"}
    if payload is not None:
        data = json.dumps(payload).encode("utf-8")
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = resp.read()
    except urllib.error.HTTPError as exc:  # surface ComfyUI's JSON error body
        detail = exc.read().decode("utf-8", "replace")
        raise RuntimeError(f"{method} {url} -> HTTP {exc.code}: {detail}") from exc
    except urllib.error.URLError as exc:
        raise RuntimeError(
            f"Cannot reach ComfyUI at {url} ({exc.reason}). Is it running? "
            "Start it with: python main.py --port 8188"
        ) from exc
    return json.loads(body) if body else None


def request_bytes(url: str, timeout: float = 60.0) -> bytes:
    """GET a binary resource (an image from /view)."""
    with urllib.request.urlopen(urllib.request.Request(url), timeout=timeout) as resp:
        return resp.read()


def build_workflow(
    prompt: str,
    negative: str,
    checkpoint: str,
    width: int,
    height: int,
    steps: int,
    cfg: float,
    seed: int,
    sampler: str,
    scheduler: str,
    batch_size: int,
    filename_prefix: str,
) -> dict[str, Any]:
    """Return a minimal SD1.5-class txt2img graph in ComfyUI's API format."""
    return {
        "4": {
            "class_type": "CheckpointLoaderSimple",
            "inputs": {"ckpt_name": checkpoint},
        },
        "5": {
            "class_type": "EmptyLatentImage",
            "inputs": {"width": width, "height": height, "batch_size": batch_size},
        },
        "6": {
            "class_type": "CLIPTextEncode",
            "inputs": {"text": prompt, "clip": ["4", 1]},
        },
        "7": {
            "class_type": "CLIPTextEncode",
            "inputs": {"text": negative, "clip": ["4", 1]},
        },
        "3": {
            "class_type": "KSampler",
            "inputs": {
                "seed": seed,
                "steps": steps,
                "cfg": cfg,
                "sampler_name": sampler,
                "scheduler": scheduler,
                "denoise": 1.0,
                "model": ["4", 0],
                "positive": ["6", 0],
                "negative": ["7", 0],
                "latent_image": ["5", 0],
            },
        },
        "8": {
            "class_type": "VAEDecode",
            "inputs": {"samples": ["3", 0], "vae": ["4", 2]},
        },
        "9": {
            "class_type": "SaveImage",
            "inputs": {"filename_prefix": filename_prefix, "images": ["8", 0]},
        },
    }


def queue_prompt(root: str, workflow: dict[str, Any], client_id: str) -> str:
    """POST the graph to /prompt and return the assigned prompt_id."""
    result = request_json(
        f"{root}/prompt",
        {"prompt": workflow, "client_id": client_id},
        method="POST",
    )
    if not result or "prompt_id" not in result:
        raise RuntimeError(f"ComfyUI rejected the prompt: {result}")
    return result["prompt_id"]


def wait_for_result(root: str, prompt_id: str, timeout: float, poll: float) -> dict[str, Any]:
    """Poll /history/{id} until the job finishes, errors, or times out."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        history = request_json(f"{root}/history/{prompt_id}")
        entry = history.get(prompt_id) if history else None
        if entry:
            status = entry.get("status", {})
            if status.get("status_str") == "error":
                raise RuntimeError(f"ComfyUI execution error: {json.dumps(status)}")
            if status.get("completed") or entry.get("outputs"):
                return entry
        time.sleep(poll)
    raise TimeoutError(f"prompt {prompt_id} did not finish within {timeout:.0f}s")


def extract_images(entry: dict[str, Any]) -> list[dict[str, str]]:
    """Flatten every image descriptor saved by SaveImage nodes."""
    images: list[dict[str, str]] = []
    for node_output in entry.get("outputs", {}).values():
        for image in node_output.get("images", []):
            images.append(image)
    return images


def download_image(root: str, image: dict[str, str], out_path: Path) -> Path:
    """Download one /view image to out_path."""
    query = urllib.parse.urlencode(
        {
            "filename": image.get("filename", ""),
            "subfolder": image.get("subfolder", ""),
            "type": image.get("type", "output"),
        }
    )
    data = request_bytes(f"{root}/view?{query}")
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_bytes(data)
    return out_path


def system_stats(root: str) -> dict[str, Any]:
    """Return ComfyUI's /system_stats probe (VRAM/RAM/versions)."""
    return request_json(f"{root}/system_stats") or {}


def free_vram(root: str) -> None:
    """Ask ComfyUI to unload models and return cached memory to the driver."""
    request_json(
        f"{root}/free",
        {"unload_models": True, "free_memory": True},
        method="POST",
    )


def print_stats(root: str) -> None:
    """Print a compact VRAM/RAM summary from /system_stats."""
    stats = system_stats(root)
    system = stats.get("system", {})
    print(f"ComfyUI {system.get('comfyui_version', '?')} | torch {system.get('pytorch_version', '?')}")
    for device in stats.get("devices", []):
        total = device.get("vram_total", 0) / 1024**3
        free = device.get("vram_free", 0) / 1024**3
        print(f"  {device.get('name', '?')}: {free:.1f} / {total:.1f} GiB VRAM free")


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Minimal ComfyUI HTTP txt2img client.")
    parser.add_argument("--host", default=DEFAULT_HOST, help="ComfyUI host (default 127.0.0.1)")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT, help="ComfyUI port (default 8188)")
    parser.add_argument("--prompt", default="a gilded iron axe icon, game item, plain background")
    parser.add_argument("--negative", default="blurry, lowres, watermark, text, extra fingers")
    parser.add_argument("--checkpoint", default=DEFAULT_CHECKPOINT)
    parser.add_argument("--steps", type=int, default=20)
    parser.add_argument("--cfg", type=float, default=7.5)
    parser.add_argument("--seed", type=int, default=1234)
    parser.add_argument("--width", type=int, default=512)
    parser.add_argument("--height", type=int, default=512)
    parser.add_argument("--batch-size", type=int, default=1)
    parser.add_argument("--sampler", default="euler")
    parser.add_argument("--scheduler", default="normal")
    parser.add_argument("--prefix", default="echocradle")
    parser.add_argument("--out", type=Path, default=Path("out/comfy_image.png"))
    parser.add_argument("--timeout", type=float, default=300.0, help="Seconds to wait for the job")
    parser.add_argument("--poll", type=float, default=1.0, help="History poll interval (seconds)")
    parser.add_argument("--release", action="store_true", help="POST /free after the run")
    parser.add_argument("--release-only", action="store_true", help="Only POST /free, then exit")
    parser.add_argument("--stats", action="store_true", help="Only print /system_stats, then exit")
    parser.add_argument(
        "--list-checkpoints",
        action="store_true",
        help="Only list installed checkpoints, then exit",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    root = base_url(args.host, args.port)

    try:
        if args.stats:
            print_stats(root)
            return 0
        if args.list_checkpoints:
            for name in request_json(f"{root}/models/checkpoints") or []:
                print(name)
            return 0
        if args.release_only:
            free_vram(root)
            print("unload_models + free_memory requested")
            return 0

        workflow = build_workflow(
            prompt=args.prompt,
            negative=args.negative,
            checkpoint=args.checkpoint,
            width=args.width,
            height=args.height,
            steps=args.steps,
            cfg=args.cfg,
            seed=args.seed,
            sampler=args.sampler,
            scheduler=args.scheduler,
            batch_size=args.batch_size,
            filename_prefix=args.prefix,
        )

        client_id = str(uuid.uuid4())
        started = time.monotonic()
        prompt_id = queue_prompt(root, workflow, client_id)
        print(f"queued prompt_id={prompt_id} seed={args.seed} {args.width}x{args.height}")

        entry = wait_for_result(root, prompt_id, args.timeout, args.poll)
        elapsed = time.monotonic() - started

        images = extract_images(entry)
        if not images:
            raise RuntimeError(f"job finished but produced no images: {json.dumps(entry)}")

        written: list[Path] = []
        for index, image in enumerate(images):
            target = args.out
            if len(images) > 1:
                target = args.out.with_name(f"{args.out.stem}_{index}{args.out.suffix}")
            written.append(download_image(root, image, target))

        for path in written:
            print(f"saved {path} ({path.stat().st_size / 1024:.0f} KiB)")
        print(f"latency {elapsed:.2f}s (server-side queue + sampling + decode)")
    except (RuntimeError, TimeoutError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    finally:
        if args.release:
            try:
                free_vram(root)
                print("requested model unload + VRAM free")
            except RuntimeError as exc:
                print(f"warning: could not free VRAM: {exc}", file=sys.stderr)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())