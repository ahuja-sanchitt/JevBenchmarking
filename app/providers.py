"""HTTP clients for the two sides of the race: OpenAI Chat Completions and the OpenRouter Decisions API (Jev)."""
from __future__ import annotations

import asyncio
import json
from dataclasses import asdict, dataclass

import httpx

from .config import JEV_PRICE_IN, Settings

REFUSAL_STATUSES = (400, 413, 422)
MAX_SPLIT_DEPTH = 5


class ProviderError(Exception):
    """A provider call failed. The message is shown in the lane, so it never contains key material."""


def _error_text(r: httpx.Response) -> str:
    try:
        body = r.json()
        err = body.get("error", body)
        msg = err.get("message") if isinstance(err, dict) else str(err)
    except Exception:
        msg = r.text
    return (msg or r.reason_phrase or "")[:300]


@dataclass
class OpenAIUsage:
    prompt_tokens: int
    cached_tokens: int
    completion_tokens: int  # includes reasoning tokens
    reasoning_tokens: int
    cost: float  # computed from list price, never reported
    served_model: str
    model: str = ""  # the model we asked for (served_model is the dated build that answered)
    cost_reported: bool = False
    fallback_json_object: bool = False
    temperature_dropped: bool = False  # the model rejected prod's temperature, so it ran at its default

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class JevUsage:
    input_tokens: int
    output_tokens: int
    cost: float
    cost_reported: bool  # False when any response lacked usage.cost and we fell back to list price
    calls: int  # billed requests
    strategy: str  # "fused" for one request, "split-N" otherwise
    served_model: str

    def to_dict(self) -> dict:
        return asdict(self)


class OpenAIClient:
    def __init__(self, settings: Settings, transport: httpx.AsyncBaseTransport | None = None):
        self.settings = settings
        self.model = settings.openai_model
        self.http = httpx.AsyncClient(
            base_url=settings.openai_base_url.rstrip("/") + "/",
            timeout=settings.openai_timeout_s,
            transport=transport,
            headers={"Authorization": f"Bearer {settings.openai_api_key or ''}"},
        )

    async def aclose(self) -> None:
        await self.http.aclose()

    def cost(self, prompt: int, cached: int, completion: int, model: str | None = None) -> float:
        p = self.settings.openai_price(model or self.model)
        return ((prompt - cached) * p["in"] + cached * p["cached"] + completion * p["out"]) / 1e6

    async def complete_json(
        self, system: str, user: str, schema_name: str, schema: dict, temperature: float | None = None, model: str | None = None
    ) -> tuple[object, OpenAIUsage]:
        """System + user messages and temperature as prod sends them. Reasoning effort is left at the
        model default ("prod config", like Gemini's thinking_budget=None)."""
        model = model or self.model
        body = {
            "model": model,
            "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
            "response_format": {"type": "json_schema", "json_schema": {"name": schema_name, "schema": schema, "strict": True}},
        }
        if temperature is not None:
            body["temperature"] = temperature
        fallback = dropped = False
        r = await self._post(body)
        for _ in range(2):
            if r.status_code != 400:
                break
            # A model swap should never break the demo: retry once per rejected parameter, and record it.
            if "temperature" in r.text and "temperature" in body:
                body.pop("temperature")
                dropped = True
            elif "response_format" in r.text and body["response_format"]["type"] == "json_schema":
                body["response_format"] = {"type": "json_object"}
                fallback = True
            else:
                break
            r = await self._post(body)
        if r.status_code != 200:
            raise ProviderError(f"OpenAI {r.status_code}: {_error_text(r)}")

        data = r.json()
        try:
            content = data["choices"][0]["message"]["content"]
            parsed = json.loads(content)
        except (KeyError, IndexError, TypeError, json.JSONDecodeError) as e:
            raise ProviderError(f"OpenAI returned unparseable output: {e}") from e

        u = data.get("usage") or {}
        prompt_tokens = int(u.get("prompt_tokens") or 0)
        cached = int((u.get("prompt_tokens_details") or {}).get("cached_tokens") or 0)
        completion = int(u.get("completion_tokens") or 0)
        reasoning = int((u.get("completion_tokens_details") or {}).get("reasoning_tokens") or 0)
        usage = OpenAIUsage(
            prompt_tokens=prompt_tokens, cached_tokens=cached, completion_tokens=completion, reasoning_tokens=reasoning,
            cost=self.cost(prompt_tokens, cached, completion, model), served_model=data.get("model") or model, model=model,
            fallback_json_object=fallback, temperature_dropped=dropped,
        )
        return parsed, usage

    async def _post(self, body: dict) -> httpx.Response:
        try:
            return await self.http.post("chat/completions", json=body)
        except httpx.TimeoutException as e:
            raise ProviderError(f"OpenAI timed out after {self.settings.openai_timeout_s:.0f}s") from e
        except httpx.HTTPError as e:
            raise ProviderError(f"OpenAI request failed: {type(e).__name__}") from e


class _Refused(Exception):
    def __init__(self, status: int, text: str):
        super().__init__(text)
        self.status = status


class JevClient:
    def __init__(self, settings: Settings, transport: httpx.AsyncBaseTransport | None = None):
        self.settings = settings
        self.model = settings.jev_model
        self.http = httpx.AsyncClient(
            base_url=settings.openrouter_base_url.rstrip("/") + "/",
            timeout=settings.jev_timeout_s,
            transport=transport,
            headers={"Authorization": f"Bearer {settings.openrouter_api_key or ''}"},
        )

    async def aclose(self) -> None:
        await self.http.aclose()

    async def _ask(self, state: dict, questions: dict) -> dict:
        try:
            r = await self.http.post("alpha/decisions", json={"model": self.model, "state": state, "questions": questions})
        except httpx.TimeoutException as e:
            raise ProviderError(f"Jev timed out after {self.settings.jev_timeout_s:.0f}s") from e
        except httpx.HTTPError as e:
            raise ProviderError(f"Jev request failed: {type(e).__name__}") from e
        if r.status_code in REFUSAL_STATUSES:
            raise _Refused(r.status_code, _error_text(r))
        if r.status_code != 200:
            raise ProviderError(f"OpenRouter {r.status_code}: {_error_text(r)}")
        return r.json()

    async def ask_many(self, state: dict, questions: dict) -> tuple[dict, JevUsage]:
        """Send every question in one request. If it's refused, split in half and send both halves
        in parallel, recursively. Questions are independent, so splitting only adds requests, not wait."""
        responses = await self._ask_split(state, questions, 0)
        answers: dict = {}
        input_tokens = output_tokens = 0
        cost = 0.0
        reported = True
        served = self.model
        for resp in responses:
            answers.update(resp.get("answers") or {})
            u = resp.get("usage") or {}
            it = int(u.get("input_tokens") or 0)
            input_tokens += it
            output_tokens += int(u.get("output_tokens") or 0)
            if u.get("cost") is None:
                reported = False
                cost += it * JEV_PRICE_IN / 1e6
            else:
                cost += float(u["cost"])
            served = resp.get("model") or served  # log the dated build actually served
        missing = set(questions) - set(answers)
        if missing:
            raise ProviderError(f"Jev returned no answer for {len(missing)} question(s), e.g. {sorted(missing)[0]}")
        calls = len(responses)
        usage = JevUsage(
            input_tokens=input_tokens, output_tokens=output_tokens, cost=cost, cost_reported=reported,
            calls=calls, strategy="fused" if calls == 1 else f"split-{calls}", served_model=served,
        )
        return answers, usage

    async def _ask_split(self, state: dict, questions: dict, depth: int) -> list[dict]:
        try:
            return [await self._ask(state, questions)]
        except _Refused as e:
            if len(questions) < 2 or depth >= MAX_SPLIT_DEPTH:
                raise ProviderError(f"OpenRouter {e.status}: {e}") from e
        items = list(questions.items())
        mid = len(items) // 2
        halves = await asyncio.gather(
            self._ask_split(state, dict(items[:mid]), depth + 1),
            self._ask_split(state, dict(items[mid:]), depth + 1),
        )
        return halves[0] + halves[1]
