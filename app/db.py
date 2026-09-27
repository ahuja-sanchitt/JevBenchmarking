"""The runs table. Metrics only: the visitor's text is never stored."""
from __future__ import annotations

import hashlib
import math
from datetime import datetime, timedelta, timezone
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit
from uuid import uuid4

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine
from sqlalchemy.pool import NullPool

metadata = sa.MetaData()

runs = sa.Table(
    "runs",
    metadata,
    sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
    sa.Column("ts", sa.DateTime(timezone=True), nullable=False, index=True),
    sa.Column("feature", sa.String(16), nullable=False, index=True),
    sa.Column("preset_id", sa.String(32)),
    sa.Column("openai_ok", sa.Boolean, nullable=False),
    sa.Column("openai_model", sa.String(128)),  # the served, dated build
    sa.Column("openai_requested", sa.String(64), index=True),  # the model the visitor picked; stats are per this
    sa.Column("openai_ms", sa.Float),
    sa.Column("openai_cost", sa.Float),
    sa.Column("openai_prompt_tokens", sa.Integer),
    sa.Column("openai_cached_tokens", sa.Integer),
    sa.Column("openai_completion_tokens", sa.Integer),
    sa.Column("openai_reasoning_tokens", sa.Integer),
    sa.Column("jev_ok", sa.Boolean, nullable=False),
    sa.Column("jev_model", sa.String(128)),  # the served, dated build
    sa.Column("jev_ms", sa.Float),
    sa.Column("jev_cost", sa.Float),
    sa.Column("jev_input_tokens", sa.Integer),
    sa.Column("jev_output_tokens", sa.Integer),
    sa.Column("jev_calls", sa.Integer),
    sa.Column("jev_cost_reported", sa.Boolean),
    sa.Column("jev_strategy", sa.String(32)),
    sa.Column("agrees", sa.Boolean),
    sa.Column("winner", sa.String(8)),
)

COLUMNS = [c.name for c in runs.columns]

# One row per race attempt that passed validation, for the per-IP hourly limit. Stored in the database
# so it holds across serverless instances. The IP itself is never stored, only a salted hash.
rate_hits = sa.Table(
    "rate_hits",
    metadata,
    sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
    sa.Column("ts", sa.DateTime(timezone=True), nullable=False, index=True),
    sa.Column("ip_hash", sa.String(64), nullable=False, index=True),
)

_DROP_PARAMS = {"sslmode", "channel_binding"}  # libpq options asyncpg doesn't accept as keywords


def _statement_name() -> str:
    return f"__asyncpg_{uuid4()}__"


def normalise_url(url: str) -> tuple[str, dict]:
    """Accept the Postgres URLs hosts hand out (Neon/Vercel: postgres://...?sslmode=require) and turn them
    into what SQLAlchemy's asyncpg driver expects, returning (url, connect_args)."""
    if url.startswith(("postgres://", "postgresql://")):
        url = "postgresql+asyncpg://" + url.split("://", 1)[1]
    if not url.startswith("postgresql+asyncpg://"):
        return url, {}
    parts = urlsplit(url)
    query = parse_qsl(parts.query, keep_blank_values=True)
    ssl = next((v for k, v in query if k == "sslmode"), None)
    query = [(k, v) for k, v in query if k not in _DROP_PARAMS]
    if not any(k == "prepared_statement_cache_size" for k, _ in query):
        query.append(("prepared_statement_cache_size", "0"))  # SQLAlchemy's cache
    # Transaction-mode poolers (Supabase :6543, PgBouncer, Neon) can't hold prepared statements across
    # transactions: turn off asyncpg's cache and give each statement a unique name so none collide.
    connect_args: dict = {"statement_cache_size": 0, "prepared_statement_name_func": _statement_name}
    if ssl and ssl != "disable":
        connect_args["ssl"] = "require"
    return urlunsplit(parts._replace(query=urlencode(query))), connect_args


def make_engine(url: str) -> AsyncEngine:
    url, connect_args = normalise_url(url)
    if url.startswith("sqlite"):
        return create_async_engine(url)
    # Serverless: no connections kept between invocations; the host's pooler does the pooling.
    return create_async_engine(url, poolclass=NullPool, connect_args=connect_args)


async def init(engine: AsyncEngine) -> None:
    async with engine.begin() as conn:
        await conn.run_sync(metadata.create_all)
        await conn.run_sync(_add_missing_columns)


def _add_missing_columns(sync_conn) -> None:
    """create_all doesn't alter existing tables; add columns introduced after a database was created."""
    have = {c["name"] for c in sa.inspect(sync_conn).get_columns("runs")}
    for col in runs.columns:
        if col.name not in have:
            sync_conn.execute(sa.text(f"ALTER TABLE runs ADD COLUMN {col.name} {col.type.compile(sync_conn.dialect)}"))
    if "openai_requested" not in have and "openai_model" in have:
        # Older runs only stored the served build ("gpt-4.1-2025-04-14"); map it back to the alias raced.
        from .config import OPENAI_PRICES

        for alias in sorted(OPENAI_PRICES, key=len, reverse=True):
            sync_conn.execute(
                runs.update()
                .where(runs.c.openai_requested.is_(None), runs.c.openai_model.like(f"{alias}%"))
                .values(openai_requested=alias)
            )


def _row(m) -> dict:
    d = dict(m)
    ts = d.get("ts")
    if isinstance(ts, datetime):
        d["ts"] = (ts if ts.tzinfo else ts.replace(tzinfo=timezone.utc)).isoformat()
    return d


async def insert(engine: AsyncEngine, row: dict) -> dict:
    row = {k: v for k, v in row.items() if k in COLUMNS and k != "id"}
    async with engine.begin() as conn:
        result = await conn.execute(runs.insert().values(**row))
        rid = result.inserted_primary_key[0]
        saved = (await conn.execute(sa.select(runs).where(runs.c.id == rid))).mappings().one()
    return _row(saved)


async def history(engine: AsyncEngine, feature: str | None, limit: int = 1000, openai_model: str | None = None) -> list[dict]:
    limit = max(1, min(int(limit), 5000))
    q = sa.select(runs).order_by(runs.c.id.desc()).limit(limit)
    if feature:
        q = q.where(runs.c.feature == feature)
    if openai_model:
        q = q.where(runs.c.openai_requested == openai_model)
    async with engine.connect() as conn:
        rows = (await conn.execute(q)).mappings().all()
    return [_row(r) for r in reversed(rows)]  # oldest first


async def spend_today(engine: AsyncEngine) -> float:
    start = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
    q = sa.select(sa.func.coalesce(sa.func.sum(sa.func.coalesce(runs.c.openai_cost, 0) + sa.func.coalesce(runs.c.jev_cost, 0)), 0)).where(runs.c.ts >= start)
    async with engine.connect() as conn:
        return float((await conn.execute(q)).scalar_one())


def percentile(values: list[float], p: float) -> float | None:
    """Linear interpolation between closest ranks."""
    v = sorted(x for x in values if x is not None)
    if not v:
        return None
    i = (len(v) - 1) * p
    lo, hi = math.floor(i), math.ceil(i)
    return v[lo] + (v[hi] - v[lo]) * (i - lo)


def _mean(values: list[float]) -> float | None:
    v = [x for x in values if x is not None]
    return sum(v) / len(v) if v else None


async def stats(engine: AsyncEngine, feature: str, openai_model: str | None = None) -> dict:
    where = [runs.c.feature == feature] + ([runs.c.openai_requested == openai_model] if openai_model else [])
    async with engine.connect() as conn:
        total = (await conn.execute(sa.select(sa.func.count()).select_from(runs).where(*where))).scalar_one()
        rows = (await conn.execute(
            sa.select(runs).where(*where, runs.c.openai_ok.is_(True), runs.c.jev_ok.is_(True))
        )).mappings().all()
    col = lambda k: [r[k] for r in rows]

    def side(p: str) -> dict:
        return {
            "n": len(rows),
            "ms_p50": percentile(col(f"{p}_ms"), 0.5), "ms_p90": percentile(col(f"{p}_ms"), 0.9),
            "cost_p50": percentile(col(f"{p}_cost"), 0.5), "cost_p90": percentile(col(f"{p}_cost"), 0.9),
            "cost_mean": _mean(col(f"{p}_cost")),
        }

    n = len(rows)
    return {
        "runs": total,
        "paired": n,
        "openai": side("openai") | {
            "prompt_tokens_p50": percentile(col("openai_prompt_tokens"), 0.5),
            "completion_tokens_p50": percentile(col("openai_completion_tokens"), 0.5),
            "reasoning_tokens_p50": percentile(col("openai_reasoning_tokens"), 0.5),
        },
        "jev": side("jev") | {
            "input_tokens_p50": percentile(col("jev_input_tokens"), 0.5),
            "output_tokens_p50": percentile(col("jev_output_tokens"), 0.5),
        },
        "jev_faster_share": sum(r["winner"] == "jev" for r in rows) / n if n else None,
        "agreement": sum(bool(r["agrees"]) for r in rows) / n if n else None,
    }


def hash_ip(ip: str, salt: str) -> str:
    return hashlib.sha256(f"{salt}:{ip}".encode()).hexdigest()


async def rate_check(engine: AsyncEngine, ip_hash: str, limit: int, window_s: float = 3600.0) -> float | None:
    """Sliding window per IP. Records the hit and returns None if allowed, else seconds until a slot frees."""
    now = datetime.now(timezone.utc)
    since = now - timedelta(seconds=window_s)
    async with engine.begin() as conn:
        await conn.execute(rate_hits.delete().where(rate_hits.c.ts < now - timedelta(seconds=2 * window_s)))
        hits = (await conn.execute(
            sa.select(rate_hits.c.ts).where(rate_hits.c.ip_hash == ip_hash, rate_hits.c.ts >= since).order_by(rate_hits.c.ts)
        )).scalars().all()
        if len(hits) >= limit:
            oldest = hits[0] if hits[0].tzinfo else hits[0].replace(tzinfo=timezone.utc)
            return max(1.0, (oldest + timedelta(seconds=window_s) - now).total_seconds())
        await conn.execute(rate_hits.insert().values(ts=now, ip_hash=ip_hash))
    return None
