"""Mock provider transports and settings, so the whole suite runs without API keys."""
from __future__ import annotations

import asyncio
import json
import re

import httpx
import pytest
from fastapi.testclient import TestClient

from app.config import DATA_DIR, Settings
from app.main import create_app

STOP = {"issues", "working", "properly", "while", "coming", "other", "about", "their", "there", "which", "assistance", "related", "information", "something", "covered", "option", "options", "listed"}
SIGNAL_WORDS = {
    "time_sensitive": ["today", "otp", "deadline", "right now"],
    "escalation": ["court", "police", "cheat", "fraud", "manager"],
    "unresolved": ["still not", "months", "weeks"],
    "safety": ["won't start", "not starting", "accident", "broke down"],
}


def words(s: str, min_len: int) -> set[str]:
    return {w for w in re.findall(r"[a-z]+", s.lower()) if len(w) >= min_len and w not in STOP}


class JevMock:
    """Answers nouls by keyword in the state and choices deterministically, in the §3.1 response shape."""

    def __init__(self, delay: float = 0.08, max_questions: int | None = None, fail: bool = False, omit_cost: bool = False):
        self.delay, self.max_questions, self.fail, self.omit_cost = delay, max_questions, fail, omit_cost
        self.calls: list[dict] = []

    def _noul(self, key: str, q: dict, text: str) -> float:
        if key in SIGNAL_WORDS:
            return 0.93 if any(k in text for k in SIGNAL_WORDS[key]) else 0.04
        heading = q["instructions"].split(":", 1)[-1].split(">")[-1]
        return 0.92 if words(heading, 5) & words(text, 5) else 0.05

    def _choice(self, q: dict, text: str) -> dict:
        tw = words(text, 4)
        scores = {k: len(words(desc.split(":", 1)[-1] if k.startswith("q") else desc, 4) & tw) for k, desc in q["criteria"].items()}
        best = max(scores, key=lambda k: (scores[k], -list(scores).index(k)))
        if scores[best] == 0 and "qn" in scores:
            best = "qn"
        rest = (1 - 0.8) / max(len(scores) - 1, 1)
        probs = {k: (0.8 if k == best else rest) for k in scores}
        return {"type": "choice", "choice": best, "confidence": 0.8, "probabilities": probs}

    async def handler(self, request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        self.calls.append(body)
        await asyncio.sleep(self.delay)
        if self.fail:
            return httpx.Response(502, json={"error": {"message": "upstream provider error"}})
        qs = body["questions"]
        if self.max_questions and len(qs) > self.max_questions:
            return httpx.Response(400, json={"error": {"message": f"too many questions ({len(qs)})"}})
        text = " ".join(str(v) for v in body["state"].values()).lower()
        answers = {}
        for k, q in qs.items():
            if q["type"] == "noul":
                answers[k] = {"type": "noul", "noul": self._noul(k, q, text)}
            else:
                answers[k] = self._choice(q, text)
        input_tokens = len(json.dumps(body)) // 4
        usage = {"input_tokens": input_tokens, "output_tokens": 2 * len(qs)}
        if not self.omit_cost:
            usage["cost"] = input_tokens * 0.042e-6
        return httpx.Response(200, json={"id": "gen-dec-test", "model": "typesafe/jev-1.13-20260917", "provider": "TypeSafe", "answers": answers, "usage": usage})

    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self.handler)


RECAT_OPENAI_ANSWER = {
    "ticket_id": "demo-1", "is_warranty": False, "recategorised": True,
    "new_query": "Fastag", "new_subquery": "Blacklisted fastag", "new_query_heading": "", "matched_issues": [],
}
PRIO_OPENAI_ANSWER = {"urgent": True, "signal": "escalation", "reason": "The customer threatens legal action."}


class OpenAIMock:
    def __init__(self, delay: float = 0.45, reject_schema: bool = False, reject_temperature: bool = False, fail: bool = False, answers: dict | None = None):
        self.delay, self.reject_schema, self.reject_temperature, self.fail = delay, reject_schema, reject_temperature, fail
        self.answers = answers or {"ticket_recategorisation": RECAT_OPENAI_ANSWER, "message_prioritiser": PRIO_OPENAI_ANSWER}
        self.calls: list[dict] = []

    async def handler(self, request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        self.calls.append(body)
        await asyncio.sleep(self.delay)
        if self.fail:
            return httpx.Response(503, json={"error": {"message": "The server is overloaded."}})
        rf = body["response_format"]
        if self.reject_temperature and "temperature" in body:
            return httpx.Response(400, json={"error": {"message": "Unsupported parameter: 'temperature' is not supported with this model."}})
        if self.reject_schema and rf["type"] == "json_schema":
            return httpx.Response(400, json={"error": {"message": "Invalid parameter: 'response_format' of type 'json_schema' is not supported with this model."}})
        text = " ".join(m["content"] for m in body["messages"])
        name = rf["json_schema"]["name"] if rf["type"] == "json_schema" else ("ticket_recategorisation" if "Ticket Data" in text else "message_prioritiser")
        return httpx.Response(200, json={
            "model": body["model"],
            "choices": [{"message": {"role": "assistant", "content": json.dumps(self.answers[name])}}],
            "usage": {
                "prompt_tokens": 2100, "prompt_tokens_details": {"cached_tokens": 0},
                "completion_tokens": 620, "completion_tokens_details": {"reasoning_tokens": 560},
            },
        })

    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self.handler)


@pytest.fixture
def settings(tmp_path) -> Settings:
    return Settings(
        openai_api_key="sk-test-secret-openai",
        openrouter_api_key="sk-or-test-secret-jev",
        database_url=f"sqlite+aiosqlite:///{(tmp_path / 'race.db').as_posix()}",
        rate_limit_per_hour=50,
        daily_spend_cap_usd=5.0,
        trust_proxy_headers=True,
        data_dir=DATA_DIR,
    )


@pytest.fixture
def make_client():
    """make_client(settings, jev=JevMock(), openai=OpenAIMock()) -> started TestClient."""
    opened: list[TestClient] = []

    def _make(settings: Settings, jev: JevMock | None = None, openai: OpenAIMock | None = None) -> TestClient:
        app = create_app(settings, openai_transport=(openai or OpenAIMock()).transport(), jev_transport=(jev or JevMock()).transport())
        c = TestClient(app)
        c.__enter__()
        opened.append(c)
        return c

    yield _make
    for c in opened:
        c.__exit__(None, None, None)


def parse_sse(text: str) -> list[tuple[str, dict]]:
    events = []
    for chunk in text.replace("\r\n", "\n").split("\n\n"):
        ev, data = None, None
        for line in chunk.split("\n"):
            if line.startswith("event:"):
                ev = line[6:].strip()
            elif line.startswith("data:"):
                data = json.loads(line[5:].strip())
        if ev:
            events.append((ev, data))
    return events


def race(client: TestClient, body: dict, headers: dict | None = None):
    with client.stream("POST", "/api/race", json=body, headers=headers or {}) as r:
        text = r.read().decode("utf-8")
        if r.status_code != 200:
            return r.status_code, json.loads(text), r.headers
        return 200, parse_sse(text), r.headers
