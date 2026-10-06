"""Diagnostic: run the live interview and print every turn's tool calls.

Not a test. Run directly to see what the model actually emits::

    python diagnose_live.py
"""

from __future__ import annotations

import sys
from pathlib import Path

_SRC = Path(__file__).resolve().parent
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from config_schema import CONFIG_SCHEMA  # noqa: E402
from interview import Interview  # noqa: E402
from orchestrator import JsonConfigTool, ModelOrchestrator, Modality, ToolRegistry  # noqa: E402

PLAYER = {
    "username": ["My name is Ada.", "I insist -- call me Ada."],
    "style": ["The style should be medieval.", "I insist on medieval."],
    "ai_name": ["Your name should be Vex.", "I insist -- your name is Vex."],
    "story": ["No story, thanks.", "No story, thanks."],
}


def _run_once(mo, trial: int) -> bool:
    tool = JsonConfigTool(CONFIG_SCHEMA, name="config")
    registry = ToolRegistry()
    tool.register_into(registry)
    interview = Interview(mo, tool, registry, max_turns=16)
    attempts: dict[str, int] = {}

    turn = interview.opening()
    for i in range(16):
        print(f"\n--- trial {trial} turn {i} ---")
        print(f"ai>  {turn.say!r}")
        for call in turn.tool_calls:
            print(f"  [tool] {call.name}({call.arguments})")
        print(f"  config={tool.data} missing={tool.missing()}")
        if turn.done or interview.complete:
            break
        missing = tool.missing()
        field = missing[0] if missing else "story"
        n = attempts.get(field, 0)
        attempts[field] = n + 1
        lines = PLAYER[field]
        reply = lines[min(n, len(lines) - 1)]
        print(f"you> {reply!r}")
        turn = interview.respond(reply)

    print(f"\nTRIAL {trial} FINAL config={tool.data} complete={interview.complete}")
    return interview.complete


def main() -> None:
    mo = ModelOrchestrator()
    mo.ensure_model(Modality.TEXT)
    trials = int(sys.argv[1]) if len(sys.argv) > 1 else 3
    wins = sum(_run_once(mo, t) for t in range(1, trials + 1))
    print(f"\n=== {wins}/{trials} complete ===")
    mo.stop()


if __name__ == "__main__":
    main()
