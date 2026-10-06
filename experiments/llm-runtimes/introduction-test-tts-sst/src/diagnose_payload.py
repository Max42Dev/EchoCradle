"""Capture everything sent to and returned by the model, for a failing run.

Monkeypatches the text host's HTTP call so every request payload (messages +
tool description) and every raw response is recorded. Runs trials until one
fails to complete, then dumps that trial's full transcript to a file.

    python diagnose_payload.py [trials]
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

_SRC = Path(__file__).resolve().parent
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from config_schema import CONFIG_SCHEMA  # noqa: E402
from interview import Interview  # noqa: E402
from orchestrator import JsonConfigTool, ModelOrchestrator, Modality, ToolRegistry  # noqa: E402
from orchestrator.hosts import text as text_host  # noqa: E402

PLAYER = {
    "username": ["My name is Ada.", "I insist -- call me Ada."],
    "style": ["The style should be medieval.", "I insist on medieval."],
    "ai_name": ["Your name should be Vex.", "I insist -- your name is Vex."],
    "story": ["No story, thanks.", "No story, thanks."],
}

#: Every (payload, response) pair, in order, for the current trial.
EXCHANGES: list[dict] = []

_original_post = text_host.TextHost._post


def _recording_post(self, path, payload):
    response = _original_post(self, path, payload)
    EXCHANGES.append({"path": path, "payload": payload, "response": response})
    return response


text_host.TextHost._post = _recording_post


def _run_trial(mo, trial: int) -> tuple[bool, dict]:
    EXCHANGES.clear()
    tool = JsonConfigTool(CONFIG_SCHEMA, name="config")
    registry = ToolRegistry()
    tool.register_into(registry)
    interview = Interview(mo, tool, registry, max_turns=16)
    attempts: dict[str, int] = {}
    transcript: list[str] = []

    turn = interview.opening()
    for i in range(16):
        transcript.append(f"--- turn {i} ---")
        transcript.append(f"ai>  {turn.say!r}")
        for call in turn.tool_calls:
            transcript.append(f"  [tool] {call.name}({call.arguments})")
        transcript.append(f"  config={tool.data} missing={tool.missing()}")
        if turn.done or interview.complete:
            break
        missing = tool.missing()
        field = missing[0] if missing else "story"
        n = attempts.get(field, 0)
        attempts[field] = n + 1
        lines = PLAYER[field]
        reply = lines[min(n, len(lines) - 1)]
        transcript.append(f"you> {reply!r}")
        turn = interview.respond(reply)

    complete = interview.complete
    print(f"trial {trial}: complete={complete} config={tool.data}")
    return complete, {"transcript": transcript, "exchanges": list(EXCHANGES)}


def _dump(trial: int, data: dict, out: Path) -> None:
    lines: list[str] = [f"=== FAILING TRIAL {trial} ===", "", "TRANSCRIPT", ""]
    lines += data["transcript"]
    lines += ["", "=" * 70, "MODEL EXCHANGES (exact payloads and responses)", "=" * 70]
    for n, ex in enumerate(data["exchanges"], start=1):
        payload = ex["payload"]
        lines.append(f"\n########## EXCHANGE {n} ##########")
        lines.append("--- REQUEST messages ---")
        for m in payload.get("messages", []):
            lines.append(f"[{m.get('role')}] {m.get('content')}")
        if "tools" in payload:
            lines.append("--- REQUEST tools (exact) ---")
            lines.append(json.dumps(payload["tools"], indent=2))
        lines.append(f"--- REQUEST other: temperature={payload.get('temperature')} "
                     f"max_tokens={payload.get('max_tokens')} "
                     f"tool_choice={payload.get('tool_choice')} ---")
        msg = ex["response"].get("choices", [{}])[0].get("message", {})
        lines.append("--- RESPONSE message ---")
        lines.append(f"content: {msg.get('content')!r}")
        lines.append(f"tool_calls: {json.dumps(msg.get('tool_calls'))}")
    out.write_text("\n".join(lines), encoding="utf-8")
    print(f"wrote {out}")


def main() -> None:
    trials = int(sys.argv[1]) if len(sys.argv) > 1 else 6
    mo = ModelOrchestrator()
    mo.ensure_model(Modality.TEXT)
    out = _SRC.parent / "out" / "failing_run.txt"
    out.parent.mkdir(parents=True, exist_ok=True)
    for t in range(1, trials + 1):
        complete, data = _run_trial(mo, t)
        if not complete:
            _dump(t, data, out)
            break
    else:
        print(f"all {trials} trials completed; no failing run to dump")
    mo.stop()


if __name__ == "__main__":
    main()
