"""Cached-model service smoke: real inference, synthetic speech, no microphone.

Run this file with the project's Python environment. No models are downloaded.
Sentence callbacks only collect text; TTS runs after each generated turn, not on
the socket consumer. This is an offline smoke runner, not runtime voice-latency
or barge-in validation. --play explicitly requires a working output device.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
import threading
import time
import wave
from dataclasses import asdict
from pathlib import Path
from typing import Any
from uuid import uuid4

import httpx
import numpy as np

from config_schema import CONFIG_SCHEMA, missing_fields, validate_config
from interview import Interview, TurnResult
from orchestrator.client import ServiceClient
from orchestrator.hosts.speech import AudioPlayer
from orchestrator.service_protocol import JSON_LIMIT, TERMINAL
from orchestrator.tools import JsonConfigTool, ToolRegistry


EXPERIMENT = Path(__file__).resolve().parent.parent
REPLIES = {
    "username": "My name is Max.",
    "style": "A medieval world with mountains and ancient castles.",
    "ai_name": "I will call you Peter.",
    "story": "No story.",
}
# These smoke-only inputs are not the interview's persona/turn templates; those
# are supplied unchanged by Interview from prompts.py.
LONG_TEXT = (
    "Describe a peaceful medieval world with mountains and ancient castles in "
    "great detail. Write a long description of its gardens, architecture, and scenery."
)
CANCEL_LIMIT_S = 10.0


def require(condition: bool, message: str) -> None:
    """Assertions remain active even when Python is run with -O."""
    if not condition:
        raise AssertionError(message)


def write_json(path: Path, data: Any) -> None:
    path.write_text(
        json.dumps(data, indent=2, ensure_ascii=False, allow_nan=False) + "\n",
        encoding="utf-8",
    )


def output_record(path: Path, kind: str) -> dict[str, Any]:
    raw = path.read_bytes()
    return {
        "path": str(path.resolve()), "kind": kind,
        "bytes": len(raw), "sha256": hashlib.sha256(raw).hexdigest(),
    }


def speak(
    client: ServiceClient, player: AudioPlayer, text: str, path: Path,
    report: dict[str, Any],
) -> None:
    started = time.monotonic()
    samples, rate = client.tts.synthesize_samples(text)
    tts_s = time.monotonic() - started
    require(
        samples.ndim == 1 and samples.size > 0 and np.isfinite(samples).all(),
        "TTS returned empty or invalid audio",
    )
    require(rate > 0, "TTS returned an invalid sample rate")
    pcm = np.rint(np.clip(samples, -1, 1) * 32767).astype("<i2").tobytes()
    with wave.open(str(path), "wb") as target:
        target.setnchannels(1)
        target.setsampwidth(2)
        target.setframerate(rate)
        target.writeframes(pcm)
    with wave.open(str(path), "rb") as source:
        require(
            source.getnchannels() == 1 and source.getsampwidth() == 2
            and source.getframerate() == rate and source.getnframes() == samples.size
            and source.readframes(source.getnframes()) == pcm,
            f"WAV verification failed: {path}",
        )
    record = output_record(path, "wav")
    record.update(text=text, sample_rate=rate, samples=int(samples.size), tts_s=tts_s)
    report["written_outputs"].append(record)
    if player.enabled:
        started = time.monotonic()
        player.begin_turn()
        require(player.play(samples, rate, text=text), player.error or "Playback failed")
        require(not player.wait(), "Playback was interrupted")
        delivery = player.delivery()
        require(
            bool(delivery) and all(
                item["heard_samples"] == item["total_samples"] for item in delivery
            ),
            "Output device did not deliver all queued samples",
        )
        record.update(playback_s=time.monotonic() - started, delivery=delivery)
        report["speaker_playback_completed"] = True


def scripted_reply(
    interview: Interview, last_question: str, attempts: dict[str, int],
) -> tuple[str, str]:
    missing = missing_fields(interview.slots)
    question = last_question.lower()
    patterns = {
        "username": r"call you|your name|name.*player|introduce yourself",
        "style": r"world|setting|landscape|style|genre",
        "ai_name": r"call me|name me|my name|name.*companion|aurelius|name.*myself",
        "story": r"story|backstory|anything else",
    }
    # Questions help select among missing fields; never skip an unrecorded value
    # merely because the companion has moved on to an optional question.
    field = next(
        (name for name in missing if re.search(patterns[name], question)),
        missing[0] if missing else "story",
    )
    attempts[field] = attempts.get(field, 0) + 1
    reply = REPLIES[field]
    if field == "ai_name" and attempts[field] > 1:
        reply = "I insist. I will call you Peter. Your companion name is Peter."
    return field, reply


def interview_smoke(
    client: ServiceClient, player: AudioPlayer, out: Path, max_turns: int,
    report: dict[str, Any],
) -> None:
    config_tool = JsonConfigTool(CONFIG_SCHEMA)
    registry = ToolRegistry()
    config_tool.register_into(registry)
    interview = Interview(client, config_tool, registry, max_turns=max_turns)
    attempts: dict[str, int] = {}

    def turn(transcript: str | None, player_record: dict[str, Any] | None) -> TurnResult:
        sentences: list[str] = []

        def collect(sentence: str) -> None:
            sentences.append(sentence)
            print(f"AI: {sentence}", flush=True)

        started = time.monotonic()
        if transcript is None:
            result, interrupted = interview.opening_streaming(collect)
        else:
            result, interrupted = interview.respond_streaming(transcript, collect)
        record = {
            "turn": interview.turns, "player": player_record, "text": result.say,
            "sentences": sentences, "text_s": time.monotonic() - started,
            "interrupted": interrupted, "repaired": result.repaired,
            "config": result.config, "done": result.done,
            "validation_errors": result.validation_errors,
            "tool_calls": [asdict(call) for call in result.tool_calls],
        }
        report["turns"].append(record)
        report["history"] = list(interview.history)
        require(not interrupted, "Unexpected interview interruption")
        for index, sentence in enumerate(sentences, 1):
            speak(
                client, player, sentence,
                out / f"ai_{interview.turns:02d}_{index:02d}.wav", report,
            )
        return result

    last = turn(None, None)
    story_declined = False
    while interview.turns < max_turns:
        if interview.complete and story_declined:
            break
        field, reply = scripted_reply(interview, last.say, attempts)
        print(f"Player ({field}): {reply}", flush=True)
        wav = out / f"player_{interview.turns + 1:02d}.wav"
        speak(client, player, reply, wav, report)
        started = time.monotonic()
        transcript = client.transcribe(wav)
        player_record = {
            "field": field, "scripted_text": reply, "transcript": transcript,
            "wav": str(wav), "stt_s": time.monotonic() - started,
        }
        report["player_replies"].append(player_record)
        print(f"STT: {transcript}", flush=True)
        require(bool(transcript.strip()), "STT returned an empty player transcript")
        last = turn(transcript, player_record)
        if field == "story":
            story_declined = True
    require(interview.complete, f"Interview incomplete after {interview.turns} turns")
    require(story_declined, "Turn limit reached before the optional story reply")
    require(not config_tool.errors(), f"Invalid raw config: {config_tool.errors()}")
    config = interview.config()
    require(not validate_config(config), f"Invalid normalized config: {validate_config(config)}")
    require(config.get("username", "").casefold() == "max", "Wrong player name recorded")
    require(config.get("ai_name", "").casefold() == "peter", "Wrong companion name recorded")
    require(
        all(word in config.get("style", "").casefold()
            for word in ("medieval", "mountains", "castles")),
        "World style details were lost",
    )
    calls = [call for record in report["turns"] for call in record["tool_calls"]]
    require(any(call["name"] == "config_set" for call in calls), "No real model tool calls")
    path = out / "config.json"
    write_json(path, config)
    require(json.loads(path.read_text(encoding="utf-8")) == config, "Config readback failed")
    report["config"] = config
    report["tool_calls"] = calls
    report["written_outputs"].append(output_record(path, "config"))


def correction_smoke(client: ServiceClient, report: dict[str, Any]) -> None:
    """Real native correction, independent of audio I/O."""
    tool = JsonConfigTool(CONFIG_SCHEMA, data={
        "username": "Max", "style": "medieval", "ai_name": "Peter",
    })
    registry = ToolRegistry()
    tool.register_into(registry)
    interview = Interview(client, tool, registry)
    result = interview.respond("Correction: my name is Ada, not Max. Keep the other values.")
    require(result.config.get("username") == "Ada", "Correction was not applied")
    require(result.config.get("ai_name") == "Peter", "Correction altered another field")
    report["correction"] = result.config


def control_smoke(client: ServiceClient, report: dict[str, Any]) -> None:
    """Raw HTTP deliberately bypasses client validation to exercise the boundary."""
    with httpx.Client(
        base_url=client._url, timeout=10, trust_env=False, follow_redirects=False,
    ) as http:
        auth = {"Authorization": f"Bearer {client._token}"}

        def check(
            name: str, method: str, path: str, expected: int, *,
            authenticated: bool = True, headers: dict[str, str] | None = None,
            payload: dict[str, Any] | None = None, content: bytes | None = None,
        ) -> dict[str, Any]:
            started = time.monotonic()
            response = http.request(
                method, path, headers={**(auth if authenticated else {}), **(headers or {})},
                json=payload, content=content,
            )
            data = response.json()
            report["checks"].append({
                "name": name, "expected_status": expected,
                "status": response.status_code, "response": data,
                "seconds": time.monotonic() - started,
                "passed": response.status_code == expected,
            })
            require(response.status_code == expected, f"{name}: HTTP {response.status_code}")
            return data

        check("unauthenticated_live", "GET", "/v1/health/live", 401, authenticated=False)
        check(
            "unexpected_origin", "GET", "/v1/health/live", 403,
            headers={"Origin": "http://127.0.0.1:1"},
        )
        check(
            "invalid_host", "GET", "/v1/health/live", 403,
            headers={"Host": "invalid.example"},
        )
        check("unsupported_kind", "POST", "/v1/jobs", 422, payload={"kind": "image"})
        check(
            "unknown_field", "POST", "/v1/jobs", 422,
            payload={"kind": "text", "text": REPLIES["style"], "unknown": True},
        )
        check(
            "oversized_body", "POST", "/v1/jobs", 413,
            headers={"Content-Type": "application/json"},
            content=json.dumps({"kind": "text", "text": "x" * JSON_LIMIT}).encode(),
        )
        spec = {
            "kind": "text", "text": REPLIES["style"], "max_tokens": 32,
            "temperature": 0, "deadline_ms": 120_000, "idempotency_key": str(uuid4()),
        }
        job = client._submit(spec)
        try:
            # Same parsed/canonical request, but different key ordering on the wire.
            repeated = check(
                "idempotent_repeat", "POST", "/v1/jobs", 202,
                payload=dict(reversed(list(spec.items()))),
            )
            require(job["job_id"] == repeated["job_id"], "Idempotency changed the job ID")
            check(
                "idempotency_conflict", "POST", "/v1/jobs", 409,
                payload={**spec, "text": REPLIES["username"]},
            )
            result = client._poll(job)
            require(bool(result.get("text", "").strip()), "Idempotent job returned no text")
            report["idempotency"] = {"job_id": job["job_id"], "result": result}
        finally:
            client._cancel(job["job_id"])


def trace_job(client: ServiceClient, identifier: str, trace: list[dict[str, Any]]) -> Any:
    snapshot = client._snapshot(identifier)
    trace.append({
        "at": time.monotonic(), "job_id": identifier, "state": snapshot["state"],
        "host_pending": snapshot.get("host_pending"), "revision": snapshot.get("revision"),
        "selected_model": snapshot.get("selected_model"),
    })
    return snapshot


def cancellation_smoke(client: ServiceClient, report: dict[str, Any]) -> None:
    trace: list[dict[str, Any]] = []
    record: dict[str, Any] = {"trace": trace}
    report["cancellation"] = record
    tick = threading.Event()
    job = client._submit({"kind": "text", "text": LONG_TEXT, "max_tokens": 512})
    identifier = job["job_id"]
    try:
        deadline = time.monotonic() + 120
        while True:
            snapshot = trace_job(client, identifier, trace)
            require(snapshot["state"] not in TERMINAL, "Text job finished before cancellation")
            if snapshot.get("host_pending"):
                break
            require(time.monotonic() < deadline, "Text worker never started")
            tick.wait(0.02)
        started = time.monotonic()
        cancelled = client._cancel(identifier)
        record["route_cancel_s"] = time.monotonic() - started
        require(cancelled["state"] == "cancelled", "Cancel route did not cancel the text job")
        require(record["route_cancel_s"] < CANCEL_LIMIT_S, "Cancel route was not prompt")
        while True:
            snapshot = trace_job(client, identifier, trace)
            require(snapshot["state"] == "cancelled", "Cancelled text job changed state")
            if not snapshot.get("host_pending"):
                break
            require(time.monotonic() - started < 120, "Cancelled host did not release its lane")
            tick.wait(0.02)
        record["route_host_release_s"] = time.monotonic() - started
    finally:
        client._cancel(identifier)
        with client._lock:
            client._jobs.discard(identifier)

    stopped = threading.Event()
    deltas: list[str] = []
    streamed_jobs: list[str] = []
    first_delta_at: list[float] = []

    def on_text(delta: str) -> None:
        if delta and not stopped.is_set():
            deltas.append(delta)
            with client._lock:
                streamed_jobs.extend(client._jobs)
            first_delta_at.append(time.monotonic())
            stopped.set()
        # Return immediately so the independent cancellation watcher can work.

    started = time.monotonic()
    text, calls = client.stream_chat_with_tools(
        [{"role": "user", "content": LONG_TEXT}], ToolRegistry(),
        max_tokens=512, temperature=0, on_text=on_text, should_stop=stopped.is_set,
    )
    returned = time.monotonic()
    record.update(
        stream_total_s=returned - started, first_delta=deltas, returned_text=text,
        tool_calls=[asdict(call) for call in calls],
    )
    require(bool(first_delta_at), "Cancellation stream produced no text delta")
    record["after_first_delta_s"] = returned - first_delta_at[0]
    require(record["after_first_delta_s"] < CANCEL_LIMIT_S, "Streaming cancel was not prompt")
    require(len(streamed_jobs) == 1, "Could not identify the real streamed job")
    snapshot = trace_job(client, streamed_jobs[0], trace)
    require(snapshot["state"] == "cancelled", "Streaming job was not cancelled")
    while snapshot.get("host_pending"):
        require(time.monotonic() - returned < 120, "Streaming host did not release its lane")
        tick.wait(0.02)
        snapshot = trace_job(client, streamed_jobs[0], trace)
        require(snapshot["state"] == "cancelled", "Cancelled stream changed state")
    started = time.monotonic()
    following = client.chat(
        [{"role": "user", "content": REPLIES["style"]}], max_tokens=32, temperature=0,
    )
    record.update(subsequent_text=following, subsequent_job_s=time.monotonic() - started)
    require(bool(following.strip()), "Text lane did not recover after cancellation")
    record["host_pending_observed_after_cancel"] = any(
        item["state"] == "cancelled" and item["host_pending"] for item in trace
    )
    record["passed"] = True


def main() -> int:
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", type=Path, default=EXPERIMENT / "out" / "service_smoke")
    parser.add_argument("--play", action="store_true", help="Require real speaker playback")
    parser.add_argument("--max-turns", type=int, default=12, help="Includes the opening turn")
    parser.add_argument("--text-model", help="Cached catalog model ID; default uses catalog policy")
    args = parser.parse_args()
    if args.max_turns < 1:
        parser.error("--max-turns must be positive")
    out = args.out.resolve()
    out.mkdir(parents=True, exist_ok=True)
    # Refuse stale/overwritten evidence, especially a previous successful config.
    if any(out.iterdir()):
        parser.error("--out must be empty; choose a new directory for each run")
    report: dict[str, Any] = {
        "status": "running", "play_requested": args.play,
        "speaker_playback_completed": False, "microphone_tested": False,
        "audio_source": "real service TTS, offline WAV replay through real service STT",
        "runtime_voice_validation": False, "max_turns": args.max_turns,
        "checks": [], "turns": [], "history": [], "player_replies": [],
        "written_outputs": [],
    }
    player = AudioPlayer(enabled=args.play)
    started = time.monotonic()
    try:
        before = time.monotonic()
        with ServiceClient(profile="voice", text_model=args.text_model) as client:
            report["service_start_s"] = time.monotonic() - before
            capabilities = client.capabilities()
            report["capabilities"] = capabilities
            report["actual_models"] = capabilities.get("actual", {})
            require(capabilities.get("cached_only") is True, "Service is not cached-only")
            for kind in ("text", "tts", "stt"):
                actual = report["actual_models"].get(kind, {})
                require(bool(actual.get("model_id")), f"Missing actual {kind} model")
                require(capabilities["kinds"][kind]["state"] == "ready", f"{kind} not ready")
            control_smoke(client, report)
            cancellation_smoke(client, report)
            correction_smoke(client, report)
            interview_smoke(client, player, out, args.max_turns, report)
            report["final_capabilities"] = client.capabilities()
        report["status"] = "passed"
    except Exception as exc:  # Keep real failure evidence; never substitute fake inference.
        report["status"] = "failed"
        report["error"] = {"type": type(exc).__name__, "message": str(exc)}
        raise
    finally:
        player.close()
        report["total_s"] = time.monotonic() - started
        report["report_path"] = str(out / "report.json")
        write_json(out / "report.json", report)
        print(f"Report: {out / 'report.json'} ({report['status']})", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())