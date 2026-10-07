"""Full interview run: the real model fills the config, a scripted player answers.

This is a deterministic end-to-end run for inspection — no microphone, no
second model. It prints the transcript, the final config, and the exact message
list the model was given on its last turn (the "final history").

Usage::

    python full_run.py
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[4]
_ORCHESTRATOR_PARENT = _REPO_ROOT / "orchestrator"
if _ORCHESTRATOR_PARENT.is_dir() and str(_ORCHESTRATOR_PARENT) not in sys.path:
    sys.path.insert(0, str(_ORCHESTRATOR_PARENT))

from orchestrator import (  # noqa: E402
    JsonConfigTool,
    ToolRegistry,
)
from orchestrator.client import ServiceClient  # noqa: E402

from config_schema import CONFIG_SCHEMA, validate_config  # noqa: E402
from interview import Interview  # noqa: E402

#: The player's answers, in order. Deterministic so the run always completes.
_ANSWERS = [
    "My name is Max.",
    "A post-apocalyptic road.",
    "I'll call you Peter.",
    "No story.",
]

OUT = Path(__file__).resolve().parent.parent / "out" / "full_run_config.json"


def main() -> int:
    print("Probing this machine ...")
    mo = ServiceClient(profile="text")
    try:
        return _run(mo)
    finally:
        mo.stop()


def _run(mo: ServiceClient) -> int:
    model = mo.capabilities()["kinds"]["text"]["model_id"]
    print(f"  text -> {model}\n")

    tool = JsonConfigTool(CONFIG_SCHEMA, name="config")
    registry = ToolRegistry()
    tool.register_into(registry)
    interview = Interview(mo, tool, registry, max_turns=12)

    answers = list(_ANSWERS)
    turn = interview.opening()
    print("-" * 70)
    for i in range(12):
        print(f"ai>  {turn.say}")
        for call in turn.tool_calls:
            print(f"  [tool] {call.name}({json.dumps(call.arguments)})")
        print(f"  config={tool.data}  missing={tool.missing()}")
        if turn.done or interview.complete:
            break
        reply = answers.pop(0) if answers else "No story."
        print(f"you> {reply}\n")
        turn = interview.respond(reply)
    print("-" * 70)

    config = interview.config()
    errors = validate_config(config)
    print("\nFINAL CONFIG:")
    print(json.dumps(config, indent=2))
    print(f"\nvalidation: {'OK' if not errors else errors}")

    OUT.parent.mkdir(parents=True, exist_ok=True)
    tool.data = config
    tool.save(OUT)
    print(f"written to {OUT}")

    print("\n" + "=" * 70)
    print("FINAL HISTORY (exact messages sent to the model on its last turn)")
    print("=" * 70)
    for message in interview.history:
        print(f"[{message['role']}] {message['content']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
