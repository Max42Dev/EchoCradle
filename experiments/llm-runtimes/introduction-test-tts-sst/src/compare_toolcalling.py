"""Compare single-call vs two-call tool calling, with the server configured correctly.

Run:  python compare_toolcalling.py

This exists because the original bisect that motivated the two-call split was run
*before* `--jinja` and `enable_thinking: false` were added to the text host. Those
are exactly the settings that make native tool calling work, so the earlier
conclusion needs re-testing.

The question: can ONE call hold the persona, follow the agenda, and still record
values on the side?
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

_REPO = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(_REPO / "orchestrator"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from orchestrator import JsonConfigTool, ModelOrchestrator, Modality, ToolRegistry  # noqa: E402

from config_schema import CONFIG_SCHEMA  # noqa: E402

# A single prompt that asks for everything at once: persona + agenda + tools.
COMBINED = """\
You are the AI companion in EchoCradle, interviewing the player to fill their
config file. You are witty and a little snobbish about names.

Use the config tool as you go:
- Call config_set the moment the player gives you a value.
- Its result tells you what is still missing.

Ask about one thing at a time: username, then style, then your own name, then an
optional story. Object playfully the first time the player names you. Never
invent a value the player did not give you. Keep replies to one to three
sentences of plain spoken text.
"""

# The player's turns, in order.
SCRIPT = [
    "Hi there!",
    "I'm Ada.",
    "Medieval, I think.",
    "I want to name you Bob.",
    "No, I insist on Bob.",
    "No story, thanks.",
]


def run(model_id: str, trials: int = 1) -> None:
    mo = ModelOrchestrator()
    mo.ensure_model(Modality.TEXT, model_id=model_id)
    print(f"\n=== {model_id} ({trials} trial(s)) ===")

    successes = 0
    for trial in range(1, trials + 1):
        tool = JsonConfigTool(CONFIG_SCHEMA, name="config")
        registry = ToolRegistry()
        tool.register_into(registry)

        history: list[dict[str, str]] = []
        total_calls = 0
        for turn, user in enumerate(SCRIPT, start=1):
            history.append({"role": "user", "content": user})
            messages = [{"role": "system", "content": COMBINED}, *history]
            try:
                text, calls = mo.chat_with_tools(messages, registry, max_tokens=400)
            except Exception as exc:  # noqa: BLE001
                print(f"  trial {trial} turn {turn}: ERROR {exc}")
                break
            total_calls += len(calls)
            if trials == 1:
                print(f"  turn {turn}  you: {user!r}")
                print(f"           ai : {text.strip()[:90]!r}")
                print(f"           tools: {[c.name for c in calls] or 'NONE'}")
            if text.strip():
                history.append({"role": "assistant", "content": text.strip()})

        ok = not tool.missing()
        successes += ok
        mark = "OK  " if ok else "FAIL"
        print(f"  trial {trial}: {mark} {total_calls} calls, config={tool.data}")

    print(f"  -> {successes}/{trials} complete")
    mo.stop()


if __name__ == "__main__":
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    trials = 1
    for a in sys.argv[1:]:
        if a.startswith("--trials="):
            trials = int(a.split("=", 1)[1])
    for mid in args or ["granite-4.2-8b-q4km"]:
        run(mid, trials)
