"""FastAPI app: static pages, read routes, and the SSE race."""
from __future__ import annotations

import asyncio
import json
import logging
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from time import perf_counter

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles

from . import db, prioritiser, recat
from .config import JEV_PRICE_IN, STATIC_DIR, Settings, load_data
from .guard import BadInput, client_ip, validate_race
from .providers import JevClient, OpenAIClient

log = logging.getLogger("race")
FEATURES = ("recat", "prioritiser")


def create_app(
    settings: Settings | None = None,
    openai_transport: httpx.AsyncBaseTransport | None = None,
    jev_transport: httpx.AsyncBaseTransport | None = None,
) -> FastAPI:
    settings = settings or Settings.from_env()
    data = load_data(settings.data_dir)

    init_lock = asyncio.Lock()

    async def ready() -> None:
        """Create the engine, tables and clients once. Called by lifespan when the host runs it, and by every
        route otherwise, so serverless hosts that skip ASGI lifespan still work."""
        if getattr(app.state, "ready", False):
            return
        async with init_lock:
            if getattr(app.state, "ready", False):
                return
            app.state.engine = db.make_engine(settings.database_url)
            try:
                await db.init(app.state.engine)
            except Exception as e:
                # Say which host/port/user we tried (never the password), so a bad DATABASE_URL is obvious in the logs.
                log.error("Database connection failed: %s: %s | %s", type(e).__name__, e, db.describe_url(settings.database_url))
                raise
            app.state.openai = OpenAIClient(settings, openai_transport)
            app.state.jev = JevClient(settings, jev_transport)
            app.state.tasks = set()
            app.state.ready = True

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        await ready()
        try:
            yield
        finally:
            # Races outlive their HTTP response; let in-flight ones finish and save.
            if app.state.tasks:
                await asyncio.gather(*app.state.tasks, return_exceptions=True)
            await app.state.openai.aclose()
            await app.state.jev.aclose()
            await app.state.engine.dispose()
            app.state.ready = False

    app = FastAPI(title="Jev for Support Systems", lifespan=lifespan, docs_url=None, redoc_url=None)
    app.state.settings = settings
    app.state.data = data

    def error(status: int, detail: str, headers: dict | None = None) -> JSONResponse:
        return JSONResponse({"detail": detail}, status_code=status, headers=headers)

    # ------------------------------------------------------------ pages
    @app.api_route("/", methods=["GET", "HEAD"], include_in_schema=False)
    async def index():
        return FileResponse(STATIC_DIR / "index.html")

    @app.api_route("/healthz", methods=["GET", "HEAD"])
    async def healthz():
        return {"ok": True}

    # ------------------------------------------------------------ read routes
    @app.get("/api/config")
    async def config():
        return {
            "presets": data.presets,
            "taxonomy": data.taxonomy,
            "openai_model": settings.openai_model,
            "jev_model": settings.jev_model,
            "openai_models": settings.allowed_openai_models,
            "prices": {"openai": {m: settings.openai_price(m) for m in settings.allowed_openai_models}, "jev": {"in": JEV_PRICE_IN, "out": 0}},
            "limits": {
                "max_input_chars": settings.max_input_chars,
                "rate_limit_per_hour": settings.rate_limit_per_hour,
                "daily_spend_cap_usd": settings.daily_spend_cap_usd,
            },
            "keys_configured": settings.keys_configured,
            "analytics": {"provider": "umami", "website_id": settings.umami_website_id, "script_url": settings.umami_script_url}
            if settings.umami_website_id else None,
        }

    @app.get("/api/history")
    async def history(feature: str | None = None, limit: int = 1000, openai_model: str | None = None):
        await ready()
        if feature is not None and feature not in FEATURES:
            return error(400, "unknown feature")
        return await db.history(app.state.engine, feature, limit, openai_model)

    @app.api_route("/api/stats", methods=["GET", "HEAD"])  # HEAD: uptime monitors
    async def stats(openai_model: str | None = None):
        await ready()
        return {f: await db.stats(app.state.engine, f, openai_model) for f in FEATURES}

    # ------------------------------------------------------------ the race
    @app.post("/api/race")
    async def race(request: Request):
        try:
            body = await request.json()
        except (json.JSONDecodeError, UnicodeDecodeError):
            return error(400, "body must be JSON")
        try:
            req = validate_race(body, settings, data)
        except BadInput as e:
            return error(400, str(e))
        if not all(settings.keys_configured.values()):
            return error(503, "Provider keys are not configured on this server.")
        await ready()
        ip_hash = db.hash_ip(client_ip(request, settings.trust_proxy_headers), settings.ip_hash_salt)
        wait = await db.rate_check(app.state.engine, ip_hash, settings.rate_limit_per_hour)
        if wait is not None:
            return error(429, f"Rate limit reached: {settings.rate_limit_per_hour} races per hour.", {"Retry-After": str(int(wait) + 1)})
        if await db.spend_today(app.state.engine) >= settings.daily_spend_cap_usd:
            now = datetime.now(timezone.utc)
            secs = 86400 - (now.hour * 3600 + now.minute * 60 + now.second)
            return error(429, "Today's demo budget is used up. It resets at 00:00 UTC.", {"Retry-After": str(secs)})

        queue: asyncio.Queue = asyncio.Queue()
        # Detached: closing the tab mid-race still saves the run and bills it against the cap.
        task = asyncio.create_task(run_race(app, req, queue))
        app.state.tasks.add(task)
        task.add_done_callback(app.state.tasks.discard)

        async def stream():
            while True:
                item = await queue.get()
                if item is None:
                    break
                event, payload = item
                yield f"event: {event}\ndata: {json.dumps(payload, ensure_ascii=False)}\n\n"

        return StreamingResponse(stream(), media_type="text/event-stream", headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})

    # CSS/JS next to the pages; mounted last so the routes above win.
    app.mount("/", StaticFiles(directory=STATIC_DIR), name="static")
    return app


async def run_race(app: FastAPI, req: dict, queue: asyncio.Queue) -> None:
    settings: Settings = app.state.settings
    data = app.state.data
    feature = req["feature"]
    try:
        model = req["openai_model"]
        await queue.put(("start", {"feature": feature, "openai_model": model, "jev_model": settings.jev_model}))

        if feature == "recat":
            calls = {
                "openai": lambda: recat.run_openai(app.state.openai, data, req["comment"], model),
                "jev": lambda: recat.run_jev(app.state.jev, data, req["comment"]),
            }
        else:
            calls = {
                "openai": lambda: prioritiser.run_openai(app.state.openai, data, req),
                "jev": lambda: prioritiser.run_jev(app.state.jev, data, req),
            }

        t0 = perf_counter()

        async def timed(provider: str):
            try:
                answer, usage = await calls[provider]()
                return {"provider": provider, "ok": True, "ms": round((perf_counter() - t0) * 1000, 1), "answer": answer, "error": None, "usage": usage}
            except Exception as e:  # a provider failure never crashes the race
                log.warning("%s failed: %s", provider, e)
                return {"provider": provider, "ok": False, "ms": round((perf_counter() - t0) * 1000, 1), "answer": None, "error": str(e) or type(e).__name__, "usage": None}

        # Both start in the same event-loop tick; results are emitted in the order they finish.
        tasks = [asyncio.create_task(timed(p)) for p in ("openai", "jev")]
        results: dict[str, dict] = {}
        for position, fut in enumerate(asyncio.as_completed(tasks), start=1):
            res = await fut
            res["finished_position"] = position
            results[res["provider"]] = res
            await queue.put(("result", res))

        o, j = results["openai"], results["jev"]
        both = o["ok"] and j["ok"]
        mod = recat if feature == "recat" else prioritiser
        compare = mod.compare(j["answer"], o["answer"]) if both else None
        winner = ("jev" if j["ms"] <= o["ms"] else "openai") if both else None

        ou, ju = o["usage"] or {}, j["usage"] or {}
        row = {
            "ts": datetime.now(timezone.utc),
            "feature": feature,
            "preset_id": req.get("preset_id"),
            "openai_ok": o["ok"], "openai_model": ou.get("served_model") or model, "openai_requested": model,
            "openai_ms": o["ms"] if o["ok"] else None, "openai_cost": ou.get("cost"),
            "openai_prompt_tokens": ou.get("prompt_tokens"), "openai_cached_tokens": ou.get("cached_tokens"),
            "openai_completion_tokens": ou.get("completion_tokens"), "openai_reasoning_tokens": ou.get("reasoning_tokens"),
            "jev_ok": j["ok"], "jev_model": ju.get("served_model") or settings.jev_model,
            "jev_ms": j["ms"] if j["ok"] else None, "jev_cost": ju.get("cost"),
            "jev_input_tokens": ju.get("input_tokens"), "jev_output_tokens": ju.get("output_tokens"),
            "jev_calls": ju.get("calls"), "jev_cost_reported": ju.get("cost_reported"), "jev_strategy": ju.get("strategy"),
            "agrees": compare["agrees"] if compare else None,
            "winner": winner,
        }
        try:
            point = await db.insert(app.state.engine, row)
            st = await db.stats(app.state.engine, feature, model)
        except Exception as e:
            log.exception("saving the run failed")
            await queue.put(("fatal", {"error": f"could not save the run: {type(e).__name__}"}))
            return
        await queue.put(("done", {"run_id": point["id"], "compare": compare, "winner": winner, "point": point, "stats": st}))
    finally:
        await queue.put(None)


app = create_app()
