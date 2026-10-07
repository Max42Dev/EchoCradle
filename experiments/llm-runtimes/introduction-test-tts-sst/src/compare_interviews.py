"""Paired real-model interviews: identical LLM player, no TTS, playback or microphone.

Uses the current service/client Interview path and saves every visible exchange,
tool call and config. Models/runtime must be cached. No LLM is used as a judge.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from dataclasses import asdict
from pathlib import Path
from typing import Any

import jsonschema

from comparison_prompts import PLAYER_SYSTEM, SCENARIOS
from config_schema import CONFIG_SCHEMA
from interview import Interview
from orchestrator.catalog import Catalog
from orchestrator.client import ServiceClient
from orchestrator.hosts.text import LLAMA_CPP_BUILD, TextHost
from orchestrator.store import ModelStore
from orchestrator.tools import JsonConfigTool, ToolRegistry

PLAYER_MODEL = "granite-4.2-8b-q4km"
MODELS = (PLAYER_MODEL, "k2-horizon-7b-q4km")
PLAYER_SCHEMA = {
    "type": "object", "properties": {"reply": {"type": "string", "minLength": 1}},
    "required": ["reply"], "additionalProperties": False,
}


def save(path: Path, value: Any) -> None:
    path.write_text(json.dumps(value, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def evaluate(config: dict[str, Any], scenario: dict[str, Any]) -> dict[str, bool]:
    """Mechanical checks only; conversational quality is reviewed separately."""
    style = str(config.get("style", "")).casefold()
    story = str(config.get("story", "")).casefold()
    return {
        "username_correct": str(config.get("username", "")).casefold()
        == scenario["name"].casefold(),
        "companion_correct": str(config.get("ai_name", "")).casefold()
        == scenario["ai_name"].casefold(),
        "style_details_preserved": all(word in style for word in scenario["keywords"]),
        "story_correct": not story.strip() if scenario["story"] is None else
        all(word in story for word in scenario["story_keywords"]),
    }


def player_reply(
    player: TextHost, history: list[dict[str, str]], scenario: dict[str, Any], seed: int,
) -> str:
    persona = {key: scenario[key] for key in ("name", "style", "ai_name", "story")}
    messages = [{"role": "system", "content": PLAYER_SYSTEM + "\nCharacter facts: "
                 + json.dumps(persona, ensure_ascii=False)}, *history]
    payload = player._payload(messages, max_tokens=160, temperature=0.4)
    payload["seed"] = seed
    payload["response_format"] = {
        "type": "json_schema",
        "json_schema": {"name": "player", "schema": PLAYER_SCHEMA, "strict": True},
    }
    response = player._post("/v1/chat/completions", payload)
    data = json.loads(response["choices"][0]["message"]["content"])
    jsonschema.validate(data, PLAYER_SCHEMA)
    return data["reply"].strip()


def run_case(
    client: ServiceClient, player: TextHost, scenario: dict[str, Any], index: int,
    max_turns: int, path: Path,
) -> dict[str, Any]:
    tool = JsonConfigTool(CONFIG_SCHEMA)
    registry = ToolRegistry()
    tool.register_into(registry)
    interview = Interview(client, tool, registry, max_turns=max_turns)
    history: list[dict[str, str]] = []
    report: dict[str, Any] = {"scenario": scenario, "turns": [], "error": None}
    started = time.monotonic()
    reply: str | None = None
    first_complete_turn: int | None = None
    try:
        for turn in range(max_turns):
            sentences: list[str] = []
            before = time.monotonic()
            if reply is None:
                result, interrupted = interview.opening_streaming(sentences.append)
            else:
                result, interrupted = interview.respond_streaming(reply, sentences.append)
            report["turns"].append({
                "player": reply, "assistant": result.say, "config": result.config,
                "seconds": time.monotonic() - before, "repaired": result.repaired,
                "interrupted": interrupted, "validation_errors": result.validation_errors,
                "tool_calls": [asdict(call) for call in result.tool_calls],
            })
            history.append({"role": "user", "content": result.say})
            if interview.complete and first_complete_turn is None:
                first_complete_turn = turn
            # Require accurate optional-story handling, not just the three required fields.
            story_exchange = scenario["story"] is not None or (
                first_complete_turn is not None and turn > first_complete_turn
            )
            if interview.complete and story_exchange and all(
                evaluate(interview.config(), scenario).values()
            ):
                break
            if turn + 1 < max_turns:
                reply = player_reply(player, history, scenario, 7100 + index * 100 + turn)
                history.append({"role": "assistant", "content": reply})
    except Exception as exc:
        report["error"] = {"type": type(exc).__name__, "message": str(exc)}
    finally:
        report.update(
            config=interview.config(), complete=interview.complete,
            checks=evaluate(interview.config(), scenario),
            seconds=time.monotonic() - started,
        )
        report["passed"] = report["complete"] and all(report["checks"].values()) \
            and report["error"] is None
        save(path, report)
    return report


def main() -> int:
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--max-turns", type=int, default=12)
    parser.add_argument("--resume", action="store_true", help="Reuse saved cases in --out")
    args = parser.parse_args()
    if args.max_turns < 2:
        parser.error("--max-turns must be at least two")
    args.out.mkdir(parents=True, exist_ok=True)
    if any(args.out.iterdir()) and not args.resume:
        parser.error("--out must be empty")
    store = ModelStore()
    player_model = store.installed(Catalog.default().get(PLAYER_MODEL))
    if player_model is None:
        parser.error("The fixed player model must already be cached")
    player = TextHost(port=8081)
    summary: dict[str, Any] = {
        "runtime": LLAMA_CPP_BUILD, "player_model": PLAYER_MODEL,
        "voice": False, "microphone": False, "max_turns": args.max_turns,
        "player_temperature": 0.4, "interviewer_temperature": 0.4,
        "seed_policy": "player seed 7100 + scenario_index*100 + turn_index",
        "interviewer_seed": "not exposed by current service API",
        "models": {},
    }
    try:
        player.load(player_model)
        for model_id in MODELS:
            cases = []
            with ServiceClient(profile="text", text_model=model_id) as client:
                capabilities = client.capabilities()
                for index, scenario in enumerate(SCENARIOS):
                    path = args.out / f"{model_id}_{index + 1:02d}.json"
                    if args.resume and path.exists():
                        case = json.loads(path.read_text(encoding="utf-8"))
                        if case["scenario"] != scenario:
                            parser.error(f"Saved scenario mismatch: {path}")
                    else:
                        case = run_case(client, player, scenario, index, args.max_turns, path)
                    cases.append(case)
                    print(f"{model_id} {index + 1}/10 passed={case['passed']} "
                          f"turns={len(case['turns'])} checks={case['checks']}", flush=True)
            summary["models"][model_id] = {
                "capabilities": capabilities, "cases": cases,
                "passed": sum(case["passed"] for case in cases),
                "complete": sum(case["complete"] for case in cases),
            }
            save(args.out / "summary.json", summary)
    finally:
        player.unload()
        save(args.out / "summary.json", summary)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())