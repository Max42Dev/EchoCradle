"""Opt-in cached-model interview probe using the public HTTP/WebSocket client.

Loads the game's generated declaration and prompts rather than a simplified tool.
No microphone, synthesis, model provisioning, or gameplay saves are involved.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from orchestrator.client import ServiceClient


ROOT = Path(__file__).resolve().parents[1]


class InterviewTools:
    def __init__(self, config: dict[str, Any]) -> None:
        self.config = config
        self.recorded: dict[str, str] = {}
        source = (ROOT / "Unity/Assets/Scripts/Interview/InterviewToolBindings.g.cs").read_text()
        declaration = re.search(r'JArray.Parse\(@"(.*?)"\);', source)
        if declaration is None:
            raise ValueError("Generated game declaration not found")
        self.declarations = json.loads(declaration.group(1).replace('""', '"'))
        properties = self.declarations[0]["function"]["parameters"]["properties"]
        fields = config["schema"]["properties"]
        properties["field"]["enum"] = list(fields)
        properties["value"]["maxLength"] = max(v["maxLength"] for v in fields.values())

    def missing(self) -> list[str]:
        return [f for f in self.config["schema"]["required"] if not self.recorded.get(f)]

    def specs(self) -> list[dict[str, Any]]:
        return self.declarations

    def invoke(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        field, value = arguments.get("field"), arguments.get("value")
        fields = self.config["schema"]["properties"]
        if name != "config_set" or not isinstance(field, str) or not isinstance(value, str):
            return {"error": "invalid_arguments"}
        value = value.strip()
        rule = fields.get(field)
        if (rule is None or not rule.get("minLength", 0) <= len(value) <= rule["maxLength"]
                or any(ord(c) < 32 for c in value)):
            return {"error": "invalid_field_value"}
        self.recorded[field] = value
        return {"recorded": dict(self.recorded), "missing": self.missing()}

    def messages(self, history: list[dict[str, str]]) -> list[dict[str, str]]:
        prompts = self.config["prompts"]
        missing = self.missing()
        question = prompts["questions"][missing[0]] if missing else prompts["closingQuestion"]
        context = (prompts["state"].replace("{recorded}", json.dumps(self.recorded))
                   .replace("{missing}", json.dumps(missing)).replace("{question}", question))
        messages = [{"role": "system", "content": prompts["system"]}, *history]
        if not history:
            messages.append({"role": "system", "content": prompts["opening"]})
        messages.append({"role": "system", "content": context})
        return messages


def main() -> None:
    config = json.loads((ROOT / "Unity/Assets/Data/Interview.json").read_text(encoding="utf-8"))
    registry = InterviewTools(config)
    history: list[dict[str, str]] = []
    with ServiceClient.start_owned(profile="text") as client:
        for reply in [None, "My name is Alex.", "A medieval fantasy world with floating islands.",
                      "Your name is Echo.", "Yes, I insist. Your name is Echo."]:
            if reply:
                history.append({"role": "user", "content": reply})
            result = client.stream_chat_with_tools(
                registry.messages(history), registry,
                max_tokens=config["generation"]["maxTokens"],
                temperature=config["generation"]["temperature"],
            )
            print(json.dumps({
                "user": reply, "state": result.state, "text": result.text,
                "calls": [{"name": c.name, "arguments": dict(c.arguments),
                           "outcome": c.outcome, "delivered": c.result_delivered,
                           "executed": c.executed_locally} for c in result.calls],
                "recorded": registry.recorded, "missing": registry.missing(),
            }, ensure_ascii=True), flush=True)
            history.append({"role": "assistant", "content": result.text})


if __name__ == "__main__":
    main()