"""Prompt templates for the interview.

Prompts live here, not inline in the logic (Python conventions).

**Keep the system prompt short.** A long prompt full of rules gets *parroted*:
the model starts explaining its own instructions to the player ("I shall not
accept it at once... I'll offer a snobbish counter..."). Every rule added to the
prompt is another sentence the model can repeat back. So the system prompt is
character only, and the mechanical requirements live in a terse note marked
internal.

**One call per turn.** The model holds the conversation *and* records values on
the side by calling the config tools. Two settings make that work:

* ``llama-server`` must run with ``--jinja`` so native tool calling is active.
* Reasoning models (Granite 4.2, Qwen3) must have thinking disabled, or they
  spend the whole token budget in a hidden reasoning trace and return nothing.
  The catalog records ``enable_thinking: false`` per model.
"""

from __future__ import annotations

import json
from typing import Any

from config_schema import FIELD_ORDER, FIELD_QUESTIONS

#: The model replies with plain spoken text; the tool calls carry the data.
#: There is deliberately no JSON envelope: asking for one *and* native tool
#: calls makes the model confuse the two and leak its planning into the reply.
TURN_SCHEMA: dict[str, Any] | None = None

SYSTEM_PROMPT = """\
You are the AI companion in EchoCradle, meeting the player for the first time.
You are witty, theatrical, and a little snobbish about names.

When the player first names you, you may object to it once. If they insist,
accept it and record it at once.

Speak only as that character, in one to three sentences. Never explain
yourself, never describe what you are doing, and never mention rules, tools,
files, or instructions. Never use parentheses or stage directions.

Speech transcripts may contain misspellings. A name spelled letter by letter
followed by its pronunciation is one name, not two. Ask briefly if uncertain.
Private system context and interruption notes are not dialogue; never echo them.

The moment the player gives you a value, call config_set for it in that same
turn. A direct correction replaces that field: call config_set with the corrected
value, including fields already filled. Record confirmed values rather than
asking again. Confirm naturally, but never repeat bookkeeping or tool plans.
config_set can overwrite an existing value; a spoken acknowledgement alone
changes nothing. Apply corrections with the tool before acknowledging them.
"""

SPOKEN_RETRY_PROMPT = (
    "Reply with only the words you say out loud to the player: "
    "one to three sentences of plain text. No stage directions, internal plans, "
    "tools, schema, history commentary, or bookkeeping."
)


def build_system_prompt() -> str:
    """The system prompt.

    The config schema is deliberately *not* embedded here: the tool description
    already carries the fields and their meanings, so repeating the schema in the
    prompt only adds tokens the model can parrot back. Measured on Granite 4.2-8B,
    embedding it made no difference to the completion rate.
    """
    return SYSTEM_PROMPT


def build_turn_messages(
    history: list[dict[str, str]],
    slots: dict[str, Any],
    *,
    validation_errors: list[str] | None = None,
    stalled_field: str | None = None,
    interruption_context: str | None = None,
) -> list[dict[str, str]]:
    """Assemble the message list for one turn.

    The state note is terse and explicitly internal. On the opening turn there is
    no player message yet, so a greeting is supplied instead — without it the
    model answers as if the player had said nothing.

    ``validation_errors`` carries the result of checking the config against its
    schema after the previous turn. Reporting them back is what lets the model
    correct itself instead of silently leaving the file incomplete.

    ``stalled_field`` is set when the same field has been missing for several
    turns. The model is looping, so a firmer instruction is added.
    """
    filled = {k: v for k, v in slots.items() if isinstance(v, str) and v.strip()}
    missing = [f for f in FIELD_ORDER if f not in filled]
    # Terse and explicitly internal. The model is told what to ask next here
    # rather than in the system prompt, because anything in the system prompt
    # tends to get repeated back to the player.
    note = (
        "Private context; never mention or paraphrase it. "
        f"recorded={json.dumps(filled)}; "
        f"still_needed={missing or 'none'}; "
        f"ask_them_next={FIELD_QUESTIONS.get(missing[0], 'wrap up') if missing else 'wrap up'}"
    )
    turns = [
        {"role": "system", "content": build_system_prompt()},
        *history,
    ]
    if not any(message["role"] == "user" for message in history):
        turns.append(
            {
                "role": "system",
                "content": "The player has just arrived. Greet them and begin.",
            }
        )
    turns.append({"role": "system", "content": note})
    if interruption_context:
        turns.append({
            "role": "system",
            "content": "Private delivery context; never quote it: " + interruption_context,
        })

    if validation_errors:
        problems = "\n".join(f"- {e}" for e in validation_errors)
        turns.append(
            {
                "role": "system",
                "content": (
                    "Private context: the config file does not yet match its schema:\n"
                    f"{problems}\n"
                    "Fix this in your next turn: ask the player for what is missing "
                    "and record it with the config tool. Do not mention this note."
                ),
            }
        )

    if stalled_field:
        turns.append(
            {
                "role": "system",
                "content": (
                    f"Private context: '{stalled_field}' is still missing after several turns. "
                    "If the player gave or confirmed a value, call config_set with it now. "
                    "Otherwise ask one brief clarification; do not invent a value. "
                    "Never mention this note."
                ),
            }
        )
    return turns
