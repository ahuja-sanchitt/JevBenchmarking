"""The race end to end through the HTTP API."""
from __future__ import annotations

import json
from dataclasses import replace

import pytest
from pathlib import Path

from tests.conftest import JevMock, OpenAIMock, race

COMMENT = "Got stopped at the toll plaza, zebra-marker-7731, my fastag is blacklisted"
RECAT = {"feature": "recat", "preset_id": "r4", "comment": COMMENT}
PRIO = {"feature": "prioritiser", "message": "I will go to consumer court, this is cheating"}


def test_race_end_to_end(settings, make_client):
    client = make_client(settings)
    status, events, headers = race(client, RECAT)
    assert status == 200 and headers["content-type"].startswith("text/event-stream")
    assert [e for e, _ in events] == ["start", "result", "result", "done"]

    first, second = events[1][1], events[2][1]
    assert first["provider"] == "jev" and first["finished_position"] == 1  # 80ms mock beats 450ms mock
    assert second["provider"] == "openai" and second["finished_position"] == 2
    assert first["ms"] < second["ms"]
    assert first["answer"]["categories"][0]["query"] == "Fastag"

    done = events[3][1]
    assert done["winner"] == "jev" and done["compare"]["agrees"] is True
    assert done["point"]["id"] == done["run_id"] and done["stats"]["paired"] == 1

    hist = client.get("/api/history", params={"feature": "recat"}).json()
    assert [r["id"] for r in hist] == [done["run_id"]]
    assert hist[0]["jev_model"] == "typesafe/jev-1.13-20260917"  # the served build, not the alias
    raw = json.dumps(hist)
    assert "zebra-marker-7731" not in raw and "toll plaza" not in raw
    db_file = Path(settings.database_url.split("///", 1)[1])
    assert b"zebra-marker-7731" not in db_file.read_bytes()


def test_prioritiser_race(settings, make_client):
    client = make_client(settings)
    status, events, _ = race(client, PRIO)
    assert status == 200
    jev = next(d for e, d in events if e == "result" and d["provider"] == "jev")
    assert jev["answer"]["urgent"] is True and jev["answer"]["signal"] == "escalation"
    assert set(jev["answer"]["probabilities"]) == {"time_sensitive", "escalation", "unresolved", "safety"}
    assert events[-1][1]["compare"] == {"agrees": True, "same_signal": True}


def test_history_persists_across_app_instances(settings, make_client):
    a = make_client(settings)
    race(a, RECAT)
    race(a, RECAT)
    b = make_client(settings)
    _, events, _ = race(b, RECAT)
    assert events[-1][1]["run_id"] == 3
    assert [r["id"] for r in b.get("/api/history").json()] == [1, 2, 3]


def test_production_numbers_are_not_exposed(settings, make_client):
    client = make_client(settings)
    assert client.get("/api/projection").status_code == 404
    assert not (settings.data_dir / "workload.json").exists()

def test_provider_failure_has_no_winner(settings, make_client):
    client = make_client(settings, jev=JevMock(fail=True))
    _, events, _ = race(client, RECAT)
    jev = next(d for e, d in events if e == "result" and d["provider"] == "jev")
    assert jev["ok"] is False and "502" in jev["error"]
    done = events[-1][1]
    assert done["winner"] is None and done["compare"] is None
    stats = client.get("/api/stats").json()["recat"]
    assert stats["runs"] == 1 and stats["paired"] == 0


def test_rate_limit(settings, make_client):
    client = make_client(replace(settings, rate_limit_per_hour=2))
    h = {"X-Forwarded-For": "203.0.113.9, 10.0.0.1"}
    assert race(client, RECAT, h)[0] == 200
    assert race(client, RECAT, h)[0] == 200
    status, body, headers = race(client, RECAT, h)
    assert status == 429 and int(headers["retry-after"]) > 0
    assert race(client, RECAT, {"X-Forwarded-For": "198.51.100.4"})[0] == 200  # different visitor


def test_spend_cap(settings, make_client):
    client = make_client(replace(settings, daily_spend_cap_usd=1e-9))
    assert race(client, RECAT)[0] == 200
    status, body, _ = race(client, RECAT)
    assert status == 429 and "budget" in body["detail"]


def test_missing_key_503_and_config_has_no_keys(settings, make_client):
    client = make_client(replace(settings, openrouter_api_key=None))
    status, body, _ = race(client, RECAT)
    assert status == 503
    cfg = client.get("/api/config")
    text = cfg.text
    assert "sk-test-secret-openai" not in text and "sk-or" not in text
    assert cfg.json()["keys_configured"] == {"openai": True, "jev": False}


def test_validation_errors(settings, make_client):
    client = make_client(settings)
    too_long = RECAT | {"comment": "x" * (settings.max_input_chars + 1)}
    no_message = {"feature": "prioritiser", "message": "   "}
    bad_call = {"feature": "prioritiser"}
    for body in (too_long, no_message, bad_call, {"feature": "nope"}):
        status, _, _ = race(client, body)
        assert status == 400, body


def test_static_pages_served(settings, make_client):
    client = make_client(settings)
    assert client.get("/").status_code == 200
    assert client.get("/app.css").status_code == 200
    assert client.get("/healthz").json() == {"ok": True}


def test_visitor_can_pick_the_openai_model(settings, make_client):
    openai = OpenAIMock()
    client = make_client(settings, openai=openai)
    cfg = client.get("/api/config").json()
    assert cfg["openai_models"][0] == "gpt-4.1" and "gpt-4o-mini" in cfg["openai_models"]
    assert cfg["prices"]["openai"]["gpt-4o-mini"] == {"in": 0.15, "cached": 0.075, "out": 0.60}

    race(client, RECAT)  # default model
    _, events, _ = race(client, RECAT | {"openai_model": "gpt-4o-mini"})
    assert events[0][1]["openai_model"] == "gpt-4o-mini"
    assert [c["model"] for c in openai.calls] == ["gpt-4.1", "gpt-4o-mini"]
    oai = next(d for e, d in events if e == "result" and d["provider"] == "openai")
    assert oai["usage"]["cost"] == pytest.approx((2100 * 0.15 + 620 * 0.60) / 1e6)

    # History and stats are kept per model, so different baselines never mix.
    mini = client.get("/api/history", params={"feature": "recat", "openai_model": "gpt-4o-mini"}).json()
    assert [r["openai_requested"] for r in mini] == ["gpt-4o-mini"]
    assert client.get("/api/stats", params={"openai_model": "gpt-4.1"}).json()["recat"]["runs"] == 1
    assert client.get("/api/stats").json()["recat"]["runs"] == 2
    assert events[-1][1]["stats"]["runs"] == 1  # the done event's stats are for the chosen model

    status, body, _ = race(client, RECAT | {"openai_model": "gpt-9-imaginary"})
    assert status == 400 and "openai_model" in body["detail"]


def test_old_database_gets_new_columns(settings):
    import asyncio
    import sqlalchemy as sa
    from app import db

    async def go():
        engine = db.make_engine(settings.database_url)
        async with engine.begin() as conn:  # a runs table from before openai_requested existed
            await conn.execute(sa.text("CREATE TABLE runs (id INTEGER PRIMARY KEY, ts DATETIME, feature VARCHAR(16), openai_ok BOOLEAN, openai_model VARCHAR(128), jev_ok BOOLEAN)"))
            await conn.execute(sa.text("INSERT INTO runs (id, feature, openai_ok, openai_model, jev_ok) VALUES (1, 'recat', 1, 'gpt-4.1-2025-04-14', 1), (2, 'recat', 1, 'gpt-4o-mini-2024-07-18', 1)"))
        await db.init(engine)
        async with engine.connect() as conn:
            cols = await conn.run_sync(lambda c: {x["name"] for x in sa.inspect(c).get_columns("runs")})
            requested = (await conn.execute(sa.text("SELECT openai_requested FROM runs ORDER BY id"))).scalars().all()
        await engine.dispose()
        return cols, requested

    cols, requested = asyncio.run(go())
    assert {"openai_requested", "jev_output_tokens"} <= cols
    assert requested == ["gpt-4.1", "gpt-4o-mini"]  # backfilled from the served build name


@pytest.mark.parametrize("given,url,ssl", [
    ("sqlite+aiosqlite:///./race.db", "sqlite+aiosqlite:///./race.db", None),
    # Neon
    ("postgres://u:p@ep-x-pooler.neon.tech/db?sslmode=require&channel_binding=require",
     "postgresql+asyncpg://u:p@ep-x-pooler.neon.tech/db?prepared_statement_cache_size=0", "require"),
    # Supabase pooler, transaction mode (port 6543) and session mode (5432)
    ("postgresql://postgres.ref:pw@aws-0-ap-south-1.pooler.supabase.com:6543/postgres?sslmode=require",
     "postgresql+asyncpg://postgres.ref:pw@aws-0-ap-south-1.pooler.supabase.com:6543/postgres?prepared_statement_cache_size=0", "require"),
    ("postgresql://postgres.ref:pw@aws-0-ap-south-1.pooler.supabase.com:5432/postgres",
     "postgresql+asyncpg://postgres.ref:pw@aws-0-ap-south-1.pooler.supabase.com:5432/postgres?prepared_statement_cache_size=0", None),
    ("postgresql+asyncpg://u:p@host/db?sslmode=disable", "postgresql+asyncpg://u:p@host/db?prepared_statement_cache_size=0", None),
])
def test_database_url_normalisation(given, url, ssl):
    from app import db
    out_url, args = db.normalise_url(given)
    assert out_url == url
    if url.startswith("sqlite"):
        assert args == {}
        return
    assert args["statement_cache_size"] == 0 and args.get("ssl") == ssl
    names = {args["prepared_statement_name_func"]() for _ in range(3)}
    assert len(names) == 3  # unique statement names, so transaction-mode poolers never see a collision


def test_works_without_lifespan_and_stores_only_ip_hashes(settings):
    # Serverless hosts may skip ASGI lifespan: everything must initialise on first request.
    import asyncio
    import sqlalchemy as sa
    from fastapi.testclient import TestClient
    from app import db
    from app.main import create_app

    app = create_app(replace(settings, rate_limit_per_hour=1),
                     openai_transport=OpenAIMock().transport(), jev_transport=JevMock().transport())
    client = TestClient(app)  # not used as a context manager, so lifespan never runs
    assert client.get("/api/stats").status_code == 200
    ip = {"X-Forwarded-For": "203.0.113.77"}
    assert race(client, RECAT, ip)[0] == 200
    assert race(client, RECAT, ip)[0] == 429

    async def stored():
        engine = db.make_engine(settings.database_url)
        async with engine.connect() as conn:
            rows = (await conn.execute(sa.select(db.rate_hits.c.ip_hash))).scalars().all()
        await engine.dispose()
        return rows

    hashes = asyncio.run(stored())
    assert hashes == [db.hash_ip("203.0.113.77", settings.ip_hash_salt)]
    assert "203.0.113.77" not in hashes[0]


def test_head_requests_for_uptime_monitors(settings, make_client):
    client = make_client(settings)
    for path in ("/", "/healthz", "/api/stats"):
        assert client.head(path).status_code == 200, path


def test_settings_strip_pasted_whitespace_and_quotes(monkeypatch):
    from app.config import Settings
    monkeypatch.setenv("OPENAI_API_KEY", "  sk-test-abc\n")
    monkeypatch.setenv("OPENROUTER_API_KEY", '"sk-or-​test "\r\n')  # zero-width space, non-breaking space
    monkeypatch.setenv("OPENAI_MODEL", " gpt-4.1 ")
    s = Settings.from_env()
    assert (s.openai_api_key, s.openrouter_api_key, s.openai_model) == ("sk-test-abc", "sk-or-test", "gpt-4.1")


def test_analytics_config_only_when_set(settings, make_client):
    assert make_client(settings).get("/api/config").json()["analytics"] is None
    cfg = make_client(replace(settings, umami_website_id="abc-123")).get("/api/config").json()["analytics"]
    assert cfg == {"provider": "umami", "website_id": "abc-123", "script_url": "https://cloud.umami.is/script.js"}
