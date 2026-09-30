#!/usr/bin/env python3
"""Benchmark an OpenAI-compatible local text runtime.

Works unchanged against:
  * llama.cpp  ``llama-server``      (default http://127.0.0.1:8080/v1)
  * Ollama     ``/v1`` shim          (default http://127.0.0.1:11434/v1)
  * vLLM       ``vllm serve``        (default http://127.0.0.1:8000/v1)
  * LM Studio  local server          (default http://127.0.0.1:1234/v1)

It measures, for a short *structured-JSON* prompt:
  * first-token latency (TTFT, ms)
  * decode throughput (tokens/sec)
  * total wall time (ms)
  * whether the returned text parses as JSON

Dependency-light: stdlib only (``urllib``). ``httpx`` is used automatically if
it happens to be installed, but is never required. No API key is needed; a
dummy ``Bearer`` header is sent because some servers reject a missing one.

Usage
-----
    python bench_text_runtime.py
    python bench_text_runtime.py --base-url http://127.0.0.1:8080/v1 --model local
    python bench_text_runtime.py --runs 5 --max-tokens 128
"""

from __future__ import annotations

import argparse
import json
import statistics
import sys
import time
import urllib.error
import urllib.request

# ---------------------------------------------------------------------------
# Optional httpx (used only if present; urllib is the fallback).
# ---------------------------------------------------------------------------
try:  # pragma: no cover - environment dependent
    import httpx  # type: ignore
except Exception:  # noqa: BLE001
    httpx = None  # type: ignore

DEFAULT_BASE_URL = "http://127.0.0.1:11434/v1"
DEFAULT_MODEL = "llama3.1:8b"

# A short prompt that forces a small, schema-shaped JSON answer. Kept tiny so
# the measurement is dominated by decode speed, not prompt processing.
DEFAULT_PROMPT = (
    "You are a game content generator. Reply with ONLY a JSON object, no prose, "
    "matching this schema: "
    '{"name": string, "level": integer, "hostile": boolean}. '
    "Invent one blacksmith NPC for a fantasy RPG."
)

# JSON schema handed to servers that support schema-constrained decoding.
# llama.cpp and Ollama accept this; vLLM/LM Studio ignore unknown fields.
RESPONSE_SCHEMA = {
    "type": "object",
    "properties": {
        "name": {"type": "string"},
        "level": {"type": "integer"},
        "hostile": {"type": "boolean"},
    },
    "required": ["name", "level", "hostile"],
}


def _build_payload(model: str, prompt: str, max_tokens: int, use_schema: bool) -> dict:
    payload: dict = {
        "model": model,
        "messages": [{"role": "user", "content": prompt}],
        "max_tokens": max_tokens,
        "temperature": 0.0,
        "stream": True,
        # Ask for JSON. Servers that understand json_schema constrain decoding;
        # the rest fall back to plain json_object mode.
        "response_format": (
            {"type": "json_schema", "json_schema": {"name": "npc", "schema": RESPONSE_SCHEMA}}
            if use_schema
            else {"type": "json_object"}
        ),
    }
    return payload


def _stream_once_urllib(url: str, payload: dict, timeout: float) -> dict:
    """Stream a chat completion with urllib and time the first token."""
    body = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=body,
        headers={
            "Content-Type": "application/json",
            "Authorization": "Bearer no-key-required",
            "Accept": "text/event-stream",
        },
        method="POST",
    )

    start = time.perf_counter()
    first_token_at: float | None = None
    chunks: list[str] = []
    usage: dict = {}

    with urllib.request.urlopen(req, timeout=timeout) as resp:
        for raw_line in resp:
            line = raw_line.decode("utf-8", errors="replace").strip()
            if not line or not line.startswith("data:"):
                continue
            data = line[len("data:") :].strip()
            if data == "[DONE]":
                break
            try:
                event = json.loads(data)
            except json.JSONDecodeError:
                continue

            if event.get("usage"):
                usage = event["usage"]

            for choice in event.get("choices", []):
                delta = choice.get("delta") or {}
                piece = delta.get("content")
                if piece:
                    if first_token_at is None:
                        first_token_at = time.perf_counter()
                    chunks.append(piece)

    end = time.perf_counter()
    return _summarise(start, first_token_at, end, "".join(chunks), usage)


def _stream_once_httpx(url: str, payload: dict, timeout: float) -> dict:
    """Same measurement using httpx when it is available."""
    start = time.perf_counter()
    first_token_at: float | None = None
    chunks: list[str] = []
    usage: dict = {}

    with httpx.stream(  # type: ignore[union-attr]
        "POST",
        url,
        json=payload,
        headers={"Authorization": "Bearer no-key-required"},
        timeout=timeout,
    ) as resp:
        resp.raise_for_status()
        for line in resp.iter_lines():
            line = line.strip()
            if not line or not line.startswith("data:"):
                continue
            data = line[len("data:") :].strip()
            if data == "[DONE]":
                break
            try:
                event = json.loads(data)
            except json.JSONDecodeError:
                continue
            if event.get("usage"):
                usage = event["usage"]
            for choice in event.get("choices", []):
                piece = (choice.get("delta") or {}).get("content")
                if piece:
                    if first_token_at is None:
                        first_token_at = time.perf_counter()
                    chunks.append(piece)

    end = time.perf_counter()
    return _summarise(start, first_token_at, end, "".join(chunks), usage)


def _summarise(
    start: float,
    first_token_at: float | None,
    end: float,
    text: str,
    usage: dict,
) -> dict:
    total_ms = (end - start) * 1000.0
    ttft_ms = (first_token_at - start) * 1000.0 if first_token_at else None

    # Prefer server-reported token counts; fall back to a whitespace estimate.
    completion_tokens = usage.get("completion_tokens")
    if not completion_tokens:
        completion_tokens = len(text.split()) if text else 0

    decode_s = (end - first_token_at) if first_token_at else (end - start)
    tps = (completion_tokens / decode_s) if decode_s > 0 and completion_tokens else 0.0

    json_ok = False
    if text.strip():
        try:
            json.loads(text)
            json_ok = True
        except json.JSONDecodeError:
            json_ok = False

    return {
        "ttft_ms": ttft_ms,
        "total_ms": total_ms,
        "tokens": completion_tokens,
        "tokens_per_sec": tps,
        "json_ok": json_ok,
        "text": text,
    }


def _probe_models(base_url: str, timeout: float) -> list[str]:
    """Best-effort GET /models so the user can see what the server exposes."""
    url = base_url.rstrip("/") + "/models"
    req = urllib.request.Request(
        url, headers={"Authorization": "Bearer no-key-required"}, method="GET"
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        return [m.get("id", "?") for m in data.get("data", [])]
    except Exception as exc:  # noqa: BLE001
        print(f"  (could not list models: {exc})", file=sys.stderr)
        return []


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Benchmark an OpenAI-compatible local text runtime."
    )
    parser.add_argument("--base-url", default=DEFAULT_BASE_URL, help="OpenAI-compatible base URL")
    parser.add_argument("--model", default=DEFAULT_MODEL, help="model id to request")
    parser.add_argument("--prompt", default=DEFAULT_PROMPT, help="user prompt")
    parser.add_argument("--max-tokens", type=int, default=96, help="max tokens to generate")
    parser.add_argument("--runs", type=int, default=3, help="number of timed runs")
    parser.add_argument("--timeout", type=float, default=120.0, help="per-request timeout (s)")
    parser.add_argument(
        "--no-schema",
        action="store_true",
        help="send response_format=json_object instead of a JSON schema",
    )
    args = parser.parse_args(argv)

    url = args.base_url.rstrip("/") + "/chat/completions"
    payload = _build_payload(args.model, args.prompt, args.max_tokens, not args.no_schema)
    stream_fn = _stream_once_httpx if httpx is not None else _stream_once_urllib

    print(f"Runtime backend : {'httpx' if httpx is not None else 'urllib (stdlib)'}")
    print(f"Endpoint        : {url}")
    print(f"Model           : {args.model}")
    print(f"Runs            : {args.runs}  (max_tokens={args.max_tokens})")
    print("Available models:", ", ".join(_probe_models(args.base_url, args.timeout)) or "(none)")
    print("-" * 68)

    results: list[dict] = []
    for i in range(1, args.runs + 1):
        try:
            r = stream_fn(url, payload, args.timeout)
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")[:300]
            print(f"run {i}: HTTP {exc.code} — {detail}", file=sys.stderr)
            return 2
        except Exception as exc:  # noqa: BLE001
            print(f"run {i}: request failed — {exc}", file=sys.stderr)
            return 2

        results.append(r)
        ttft = f"{r['ttft_ms']:.0f} ms" if r["ttft_ms"] is not None else "n/a"
        print(
            f"run {i}: TTFT={ttft:>9}  "
            f"tok/s={r['tokens_per_sec']:6.1f}  "
            f"total={r['total_ms']:7.0f} ms  "
            f"tokens={r['tokens']:3d}  json={'ok' if r['json_ok'] else 'FAIL'}"
        )

    def _median(key: str) -> float:
        vals = [r[key] for r in results if r[key] is not None]
        return statistics.median(vals) if vals else float("nan")

    print("-" * 68)
    print("MEDIAN")
    print(f"  first-token latency : {_median('ttft_ms'):.0f} ms")
    print(f"  tokens/sec          : {_median('tokens_per_sec'):.1f}")
    print(f"  total time          : {_median('total_ms'):.0f} ms")
    print(f"  JSON valid          : {sum(r['json_ok'] for r in results)}/{len(results)} runs")
    print("\nSample output:")
    print("  " + (results[-1]["text"][:400] if results else "(none)"))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())