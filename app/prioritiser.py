"""Message prioritiser: is this customer message urgent?

Both sides get only the customer's message and the same four urgency definitions.
OpenAI: our prompt (data/prioritiser_prompt.txt) with the schema appended, called the way prod calls it.
Jev: four yes/no questions, one per urgency signal; Python applies thresholds and precedence.
"""
from __future__ import annotations

import json

from .config import Data, render
from .providers import JevClient, OpenAIClient

# key -> (instructions, true, false); the same definitions as the OpenAI prompt, in the same order.
SIGNALS: dict[str, tuple[str, str, str]] = {
    "time_sensitive": (
        "The customer needs something done today or is about to miss a deadline",
        "Mentions cancelling, an OTP, a delivery or pickup happening now, an expiring payment link, or a deadline today.",
        "No immediate time-bound action is at stake.",
    ),
    "escalation": (
        "The customer is threatening escalation or accusing the company of wrongdoing",
        "Threatens legal action, consumer court or police, alleges fraud or cheating, demands a manager or senior, or expresses contempt for the service.",
        "Frustrated at most, with no threat or accusation.",
    ),
    "unresolved": (
        "The customer says something has been pending for a long time",
        "Says money or an item is still not received, or references weeks or months of waiting.",
        "No reference to a long unresolved wait.",
    ),
    "safety": (
        "The customer describes a breakdown, accident or safety problem",
        "The car will not start, broke down, was in an accident, the wrong car was delivered, or there is a safety concern.",
        "No breakdown, accident or safety concern.",
    ),
}
# Lower bars where a miss is costliest: an ignored threat or breakdown is worse than a false alarm.
THRESHOLDS = {"time_sensitive": 0.55, "escalation": 0.45, "unresolved": 0.55, "safety": 0.45}
LABEL = {
    "time_sensitive": "time-critical request",
    "escalation": "escalation or accusation",
    "unresolved": "long unresolved issue",
    "safety": "breakdown or safety issue",
}
SIGNAL_VALUES = [*SIGNALS, "none"]
TEMPERATURE = 0.2  # prod's prioritiser temperature

SCHEMA = {
    "type": "object",
    "required": ["urgent", "signal", "reason"],
    "properties": {
        "urgent": {"type": "boolean", "description": "Whether the ticket needs an agent immediately"},
        "signal": {"enum": SIGNAL_VALUES, "type": "string", "description": "The first urgency signal that applies, or none"},
        "reason": {"type": "string", "description": "One sentence explanation"},
    },
    "additionalProperties": False,
}


def compose(p: dict[str, float]) -> dict:
    """Urgent if any signal clears its threshold; the first in SIGNALS order is reported."""
    for k in SIGNALS:
        if p[k] >= THRESHOLDS[k]:
            return {"urgent": True, "signal": k, "reason": f"Matched {LABEL[k]} (p={p[k]:.2f})."}
    top = max(SIGNALS, key=lambda k: p[k])
    return {"urgent": False, "signal": "none", "reason": f"No urgency signal; highest was {LABEL[top]} (p={p[top]:.2f})."}


def openai_messages(data: Data, message: str) -> tuple[str, str]:
    """Prod's shape: the prompt plus the response schema as the system message, the rendered template as the user message."""
    system = f"{data.prioritiser_prompt}\n\n{json.dumps(SCHEMA)}"
    return system, render(data.prioritiser_user_template, {"customer_message": message})


async def run_openai(client: OpenAIClient, data: Data, req: dict) -> tuple[dict, dict]:
    system, user = openai_messages(data, req["message"])
    raw, usage = await client.complete_json(system, user, "message_prioritiser", SCHEMA, TEMPERATURE, req.get("openai_model"))
    if isinstance(raw, list):
        raw = raw[0] if raw else {}
    signal = raw.get("signal") if raw.get("signal") in SIGNAL_VALUES else "none"
    answer = {"urgent": bool(raw.get("urgent")), "signal": signal, "reason": raw.get("reason") or ""}
    return answer, usage.to_dict()


def jev_questions() -> dict:
    return {k: {"type": "noul", "instructions": ins, "criteria": {"true": t, "false": f}} for k, (ins, t, f) in SIGNALS.items()}


async def run_jev(client: JevClient, data: Data, req: dict) -> tuple[dict, dict]:
    answers, usage = await client.ask_many({"customer_message": req["message"]}, jev_questions())
    p = {k: round(float(answers[k]["noul"]), 4) for k in SIGNALS}
    answer = compose(p) | {"probabilities": p, "thresholds": THRESHOLDS, "strategy": usage.strategy, "questions": len(SIGNALS)}
    return answer, usage.to_dict()


def compare(jev: dict, openai: dict) -> dict:
    return {"agrees": jev["urgent"] == openai["urgent"], "same_signal": jev["signal"] == openai["signal"]}
