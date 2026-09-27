"""Settings from the environment, the OpenAI price table, and the data-file loaders."""
from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass, field
from pathlib import Path

log = logging.getLogger("race")

ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = ROOT / "data"
STATIC_DIR = ROOT / "static"

# USD per 1M tokens. Verify at build time; overridable with OPENAI_PRICE_* env vars.
OPENAI_PRICES: dict[str, dict[str, float]] = {
    "gpt-4.1": {"in": 2.00, "cached": 0.50, "out": 8.00},  # what prod uses for this feature
    "gpt-4o-mini": {"in": 0.15, "cached": 0.075, "out": 0.60},
    "gpt-6-luna": {"in": 0.10, "cached": 0.01, "out": 0.50},
    "gpt-6-sol": {"in": 2.00, "cached": 0.20, "out": 10.00},
    "gpt-6-astra": {"in": 10.00, "cached": 1.00, "out": 50.00},
}
JEV_PRICE_IN = 0.042  # USD per 1M input tokens; output is free

# The taxonomy's catch-all option; Jev sees it reworded (see recat._option_text).
NOT_LISTED = "My issue is not listed here"


def _env(name: str, default: str | None = None) -> str | None:
    # Strip: a key pasted into a hosting dashboard often carries a trailing newline or space,
    # which makes the Authorization header illegal (httpx LocalProtocolError) before any request is sent.
    v = os.environ.get(name)
    v = v.strip().strip('"').strip("'").strip() if v is not None else None
    return v if v else default



def _float(name: str, default: float | None) -> float | None:
    v = _env(name)
    return float(v) if v is not None else default


@dataclass
class Settings:
    openai_api_key: str | None = None
    openai_model: str = "gpt-4.1"  # the default; visitors can pick any of openai_models
    openai_models: tuple[str, ...] = ("gpt-4.1", "gpt-4o-mini", "gpt-6-luna", "gpt-6-sol")
    openai_base_url: str = "https://api.openai.com/v1"
    openai_price_in: float | None = None
    openai_price_cached: float | None = None
    openai_price_out: float | None = None
    openrouter_api_key: str | None = None
    openrouter_base_url: str = "https://openrouter.ai/api"
    jev_model: str = "typesafe/jev-1.13"
    database_url: str = "sqlite+aiosqlite:///./race.db"
    rate_limit_per_hour: int = 20
    daily_spend_cap_usd: float = 2.00
    max_input_chars: int = 600
    trust_proxy_headers: bool = True
    ip_hash_salt: str = "jev-race"  # set IP_HASH_SALT in production so stored hashes can't be reversed by brute force
    openai_timeout_s: float = 90
    jev_timeout_s: float = 30
    data_dir: Path = field(default=DATA_DIR)

    @classmethod
    def from_env(cls) -> "Settings":
        return cls(
            openai_api_key=_env("OPENAI_API_KEY"),
            openai_model=_env("OPENAI_MODEL", "gpt-4.1"),
            openai_models=tuple(m.strip() for m in _env("OPENAI_MODELS", "gpt-4.1,gpt-4o-mini,gpt-6-luna,gpt-6-sol").split(",") if m.strip()),
            openai_base_url=_env("OPENAI_BASE_URL", "https://api.openai.com/v1"),
            openai_price_in=_float("OPENAI_PRICE_IN", None),
            openai_price_cached=_float("OPENAI_PRICE_CACHED", None),
            openai_price_out=_float("OPENAI_PRICE_OUT", None),
            openrouter_api_key=_env("OPENROUTER_API_KEY"),
            openrouter_base_url=_env("OPENROUTER_BASE_URL", "https://openrouter.ai/api"),
            jev_model=_env("JEV_MODEL", "typesafe/jev-1.13"),
            database_url=_env("DATABASE_URL", "sqlite+aiosqlite:///./race.db"),
            rate_limit_per_hour=int(_env("RATE_LIMIT_PER_HOUR", "20")),
            daily_spend_cap_usd=float(_env("DAILY_SPEND_CAP_USD", "2.00")),
            max_input_chars=int(_env("MAX_INPUT_CHARS", "600")),
            trust_proxy_headers=_env("TRUST_PROXY_HEADERS", "1") not in ("0", "false", "False"),
            ip_hash_salt=_env("IP_HASH_SALT", "jev-race"),
            openai_timeout_s=float(_env("OPENAI_TIMEOUT_S", "90")),
            jev_timeout_s=float(_env("JEV_TIMEOUT_S", "30")),
            data_dir=Path(_env("DATA_DIR", str(DATA_DIR))),
        )

    @property
    def allowed_openai_models(self) -> list[str]:
        """The raceable models: the configured list plus the default, limited to models with a known price."""
        models = [self.openai_model, *(m for m in self.openai_models if m != self.openai_model)]
        return [m for m in models if m in OPENAI_PRICES]

    def openai_price(self, model: str | None = None) -> dict[str, float]:
        model = model or self.openai_model
        base = OPENAI_PRICES.get(model, OPENAI_PRICES["gpt-4.1"])
        if model != self.openai_model:  # OPENAI_PRICE_* overrides apply to the default model only
            return dict(base)
        return {
            "in": self.openai_price_in if self.openai_price_in is not None else base["in"],
            "cached": self.openai_price_cached if self.openai_price_cached is not None else base["cached"],
            "out": self.openai_price_out if self.openai_price_out is not None else base["out"],
        }

    @property
    def keys_configured(self) -> dict[str, bool]:
        return {"openai": bool(self.openai_api_key), "jev": bool(self.openrouter_api_key)}


@dataclass
class Data:
    taxonomy: dict[str, dict[str, list[str]]]
    presets: dict[str, list[dict]]
    workload: dict
    recat_prompt: str  # system message
    prioritiser_prompt: str  # system message (the response schema is appended, as prod does)
    recat_user_template: str  # user message, {ticket_json}
    prioritiser_user_template: str  # user message, {customer_message}, {message_length}, ...
    customer_types: dict[str, str]  # query -> BUYER | SELLER, from the spreadsheet
    stand_in: bool
    user_templates_stand_in: bool  # True until the prod templates from ai/prompt_templates.py are pasted in

    def customer_type(self, query: str) -> str:
        return self.customer_types.get(query, "BUYER")

    def taxonomy_for(self, customer_type: str) -> dict[str, dict[str, list[str]]]:
        """Only that customer type's categories, like prod's build_queries_list_json(ticket)."""
        return {q: subs for q, subs in self.taxonomy.items() if self.customer_type(q) == customer_type}


def render(template: str, values: dict) -> str:
    """Fill {name} slots; other braces (e.g. JSON in the template) are left alone."""
    for k, v in values.items():
        template = template.replace("{" + k + "}", str(v))
    return template


def load_data(data_dir: Path = DATA_DIR) -> Data:
    read = lambda name: (data_dir / name).read_text(encoding="utf-8")
    data = Data(
        taxonomy=json.loads(read("taxonomy.json")),
        presets=json.loads(read("presets.json")),
        workload=json.loads(read("workload.json")),
        recat_prompt=read("recat_prompt.txt"),
        prioritiser_prompt=read("prioritiser_prompt.txt"),
        recat_user_template=read("recat_user_template.txt"),
        prioritiser_user_template=read("prioritiser_user_template.txt"),
        customer_types=json.loads(read("customer_types.json")),
        stand_in=(data_dir / "STAND_IN").exists(),
        user_templates_stand_in=(data_dir / "USER_TEMPLATES_STAND_IN").exists(),
    )
    if data.user_templates_stand_in:
        log.warning("data/*_user_template.txt are stand-ins; paste the prod templates and delete data/USER_TEMPLATES_STAND_IN")
    for name, text in (("recat_prompt.txt", data.recat_prompt), ("prioritiser_prompt.txt", data.prioritiser_prompt)):
        if text.startswith("PLACEHOLDER"):
            log.warning("data/%s is a placeholder; the OpenAI side is not running the production prompt", name)
    if "{TAXONOMY}" not in data.recat_prompt:
        raise ValueError("data/recat_prompt.txt must contain the {TAXONOMY} placeholder")
    return data
