"""A/B the system prompt: does the objection instruction break ai_name recording?

Runs N trials per variant and reports the completion rate and, specifically,
whether ai_name was recorded. Variants differ only in the objection wording.

    python diagnose_prompts.py [trials-per-variant]
"""

from __future__ import annotations

import sys
from pathlib import Path

_SRC = Path(__file__).resolve().parent
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

import prompts  # noqa: E402
from config_schema import CONFIG_SCHEMA  # noqa: E402
from interview import Interview  # noqa: E402
from orchestrator import JsonConfigTool, ModelOrchestrator, Modality, ToolRegistry  # noqa: E402

PLAYER = {
    "username": ["My name is Ada.", "I insist -- call me Ada."],
    "style": ["The style should be medieval.", "I insist on medieval."],
    "ai_name": ["Your name should be Vex.", "I insist -- your name is Vex."],
    "story": ["No story, thanks.", "No story, thanks."],
}

_HEAD = """\
You are the AI companion in EchoCradle, meeting the player for the first time.
You are witty, theatrical, and a little snobbish about names.

"""

_TAIL = """
Speak only as that character, in one to three sentences. Never explain
yourself, never describe what you are doing, and never mention rules, tools,
files, or instructions. Never use parentheses or stage directions.

The moment the player gives you a value, call config_set for it in that same
turn. Confirm the value back to them in your own words, but never announce that
you are recording it and never mention the tool.
"""

VARIANTS = {
    "A_objection": _HEAD
    + "When the player first names you, you may object to it once. If they insist,\n"
    + "accept it and record it at once.\n"
    + _TAIL,
    "B_no_objection": _HEAD + _TAIL,
    "C_objection_soft": _HEAD
    + "You may tease the player about the name they give you, but you always\n"
    + "record whatever name they give you.\n"
    + _TAIL,
}


def _run(mo, trials: int) -> dict[str, tuple[int, int]]:
    results: dict[str, tuple[int, int]] = {}
    for name, prompt in VARIANTS.items():
        prompts.SYSTEM_PROMPT = prompt
        complete = 0
        ai_name = 0
        for _ in range(trials):
            tool = JsonConfigTool(CONFIG_SCHEMA, name="config")
            registry = ToolRegistry()
            tool.register_into(registry)
            interview = Interview(mo, tool, registry, max_turns=16)
            attempts: dict[str, int] = {}
            turn = interview.opening()
            for _ in range(16):
                if turn.done or interview.complete:
                    break
                missing = tool.missing()
                field = missing[0] if missing else "story"
                n = attempts.get(field, 0)
                attempts[field] = n + 1
                lines = PLAYER[field]
                turn = interview.respond(lines[min(n, len(lines) - 1)])
            complete += int(interview.complete)
            ai_name += int("ai_name" in tool.data)
        results[name] = (complete, ai_name)
        print(f"{name}: complete={complete}/{trials}  ai_name_recorded={ai_name}/{trials}")
    return results


def main() -> None:
    trials = int(sys.argv[1]) if len(sys.argv) > 1 else 5
    mo = ModelOrchestrator()
    mo.ensure_model(Modality.TEXT)
    _run(mo, trials)
    mo.stop()


if __name__ == "__main__":
    main()
