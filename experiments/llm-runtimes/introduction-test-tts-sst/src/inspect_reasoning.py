"""Show the raw reasoning trace of a reasoning model, and detect repetition loops.

Run:  python inspect_reasoning.py [model_id]

Granite 4.2 and Qwen3 emit a hidden `reasoning_content` field. When the model is
confused it can degenerate into repeating a phrase until the token budget runs
out. This script surfaces that trace so the failure is visible rather than
silent, and flags it automatically.

This is a *model* failure mode, not an orchestrator bug. The mitigation is
`enable_thinking: false` (recorded per model in the catalog).
"""

from __future__ import annotations

import json
import re
import sys
import urllib.request
from collections import Counter
from pathlib import Path

_REPO = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(_REPO / "orchestrator"))

from orchestrator import ModelOrchestrator, Modality  # noqa: E402


def repetition_score(text: str, n: int = 4) -> float:
    """Fraction of n-grams that are duplicates. 0 = no repetition, 1 = all same."""
    words = re.findall(r"\S+", text.lower())
    if len(words) < n * 2:
        return 0.0
    grams = [tuple(words[i : i + n]) for i in range(len(words) - n + 1)]
    counts = Counter(grams)
    repeated = sum(c - 1 for c in counts.values() if c > 1)
    return repeated / len(grams)


def probe(mo: ModelOrchestrator, model_id: str, prompt: str, thinking: bool) -> None:
    payload = {
        "model": model_id,
        "messages": [{"role": "user", "content": prompt}],
        "max_tokens": 400,
        "temperature": 0.8,
    }
    if not thinking:
        payload["chat_template_kwargs"] = {"enable_thinking": False}

    request = urllib.request.Request(
        "http://127.0.0.1:8080/v1/chat/completions",
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    data = json.loads(urllib.request.urlopen(request, timeout=180).read())
    message = data["choices"][0]["message"]
    reasoning = message.get("reasoning_content") or ""
    content = message.get("content") or ""

    label = "thinking ON " if thinking else "thinking OFF"
    print(f"\n--- {label} ---")
    print(f"  finish_reason : {data['choices'][0].get('finish_reason')}")
    print(f"  reasoning len : {len(reasoning)} chars")
    print(f"  content       : {content.strip()[:100]!r}")
    if reasoning:
        score = repetition_score(reasoning)
        flag = "  <-- REPETITION LOOP" if score > 0.25 else ""
        print(f"  repetition    : {score:.2f}{flag}")
        print(f"  reasoning head: {reasoning.strip()[:160]!r}")


def main() -> None:
    model_id = sys.argv[1] if len(sys.argv) > 1 else "granite-4.2-8b-q4km"
    mo = ModelOrchestrator()
    mo.ensure_model(Modality.TEXT, model_id=model_id)
    print(f"=== {model_id} ===")

    # A prompt that invites the model to ramble, which is where loops show up.
    prompt = (
        "You are a snobbish AI companion. The player just said 'I want to name you Bob'. "
        "Object playfully and suggest a better name."
    )
    probe(mo, model_id, prompt, thinking=True)
    probe(mo, model_id, prompt, thinking=False)
    mo.stop()


if __name__ == "__main__":
    main()
