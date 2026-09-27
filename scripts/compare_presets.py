"""Run every preset through OpenAI (prod-style call) and Jev at the same time, using the app's own code,
and print answers, latency, tokens and cost side by side. Nothing is written to the race database.

    .venv/Scripts/python scripts/compare_presets.py              # both features
    .venv/Scripts/python scripts/compare_presets.py recat        # or: prioritiser

Keys come from .env. Details are written to scripts/out/compare_<timestamp>.json.
"""
from __future__ import annotations

import asyncio
import json
import statistics
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from dotenv import load_dotenv  # noqa: E402

load_dotenv(ROOT / ".env")

from app import prioritiser, recat  # noqa: E402
from app.config import Settings, load_data  # noqa: E402
from app.providers import JevClient, OpenAIClient  # noqa: E402

PROD_TIMEOUT_MS = 8000  # prod's LLM timeout


def label(c: dict) -> str:
    return " > ".join(x for x in (c.get("query"), c.get("sub_query"), c.get("query_heading")) if x)


async def timed(coro):
    t0 = time.perf_counter()
    try:
        answer, usage = await coro
        return {"ok": True, "ms": (time.perf_counter() - t0) * 1000, "answer": answer, "usage": usage}
    except Exception as e:
        return {"ok": False, "ms": (time.perf_counter() - t0) * 1000, "error": str(e)}


def describe(feature: str, r: dict) -> str:
    if not r["ok"]:
        return f"FAILED: {r['error'][:160]}"
    a = r["answer"]
    if feature == "prioritiser":
        return ("URGENT" if a["urgent"] else "not urgent") + f" ({a['signal']})"
    cats = "; ".join(label(c) + ("" if c.get("valid", True) else " [NOT IN TAXONOMY]") for c in a["categories"])
    return cats if a["placed"] else "no match"


def usage_line(provider: str, r: dict) -> str:
    if not r["ok"]:
        return ""
    u = r["usage"]
    slow = "  ⚠ >8s, would time out in prod" if provider == "openai" and r["ms"] > PROD_TIMEOUT_MS else ""
    if provider == "openai":
        flags = (" [temperature dropped]" if u.get("temperature_dropped") else "") + (" [json_object fallback]" if u.get("fallback_json_object") else "")
        return (f"{r['ms']:7.0f} ms  in={u['prompt_tokens']} (cached {u['cached_tokens']})  out={u['completion_tokens']} "
                f"(reasoning {u['reasoning_tokens']})  ${u['cost']:.6f} computed{flags}{slow}")
    return f"{r['ms']:7.0f} ms  in={u['input_tokens']}  out={u['output_tokens']} (free)  ${u['cost']:.6f} reported  {u['strategy']}"


async def run_feature(feature: str, data, oa: OpenAIClient, jev: JevClient) -> list[dict]:
    rows = []
    print(f"\n{'=' * 100}\n{feature.upper()}\n{'=' * 100}")
    for pr in data.presets[feature]:
        if feature == "recat":
            o_coro = recat.run_openai(oa, data, pr["comment"])
            j_coro = recat.run_jev(jev, data, pr["comment"])
            text = pr["comment"]
        else:
            req = {"message": pr["message"]}
            o_coro, j_coro = prioritiser.run_openai(oa, data, req), prioritiser.run_jev(jev, data, req)
            text = pr["message"]
        # Both start in the same tick, like the race.
        o, j = await asyncio.gather(timed(o_coro), timed(j_coro))
        if o["ok"] and j["ok"]:
            cmp = (recat if feature == "recat" else prioritiser).compare(j["answer"], o["answer"])
        else:
            cmp = None
        print(f"\n[{pr['id']}] {pr.get('label', '')}\n    {text}")
        print(f"    OpenAI  {describe(feature, o)}\n            {usage_line('openai', o)}")
        print(f"    Jev     {describe(feature, j)}\n            {usage_line('jev', j)}")
        if cmp is not None:
            print(f"    agree: {'YES' if cmp['agrees'] else 'NO'}" + (f"  (exact: {cmp['exact']})" if 'exact' in cmp else f"  (same signal: {cmp['same_signal']})"))
        rows.append({"preset": pr["id"], "openai": o, "jev": j, "compare": cmp})
    summarise(feature, rows)
    return rows


def summarise(feature: str, rows: list[dict]) -> None:
    paired = [r for r in rows if r["openai"]["ok"] and r["jev"]["ok"]]
    print(f"\n--- {feature} summary: {len(paired)}/{len(rows)} presets where both answered ---")
    if not paired:
        return
    med = lambda xs: statistics.median(xs)
    o_ms, j_ms = [r["openai"]["ms"] for r in paired], [r["jev"]["ms"] for r in paired]
    o_cost, j_cost = [r["openai"]["usage"]["cost"] for r in paired], [r["jev"]["usage"]["cost"] for r in paired]
    o_reason = [r["openai"]["usage"]["reasoning_tokens"] for r in paired]
    o_out = [r["openai"]["usage"]["completion_tokens"] for r in paired]
    print(f"  latency median   OpenAI {med(o_ms):7.0f} ms   Jev {med(j_ms):6.0f} ms   → Jev {med(o_ms) / med(j_ms):.1f}× faster")
    print(f"  cost mean        OpenAI ${statistics.mean(o_cost):.6f}   Jev ${statistics.mean(j_cost):.6f}   → Jev {statistics.mean(o_cost) / statistics.mean(j_cost):.1f}× cheaper")
    print(f"  OpenAI output    median {med(o_out):.0f} tokens, of which reasoning {med(o_reason):.0f}")
    print(f"  Jev answered first in {sum(r['jev']['ms'] < r['openai']['ms'] for r in paired)}/{len(paired)}   "
          f"agree {sum(r['compare']['agrees'] for r in paired)}/{len(paired)}   "
          f"OpenAI over prod's 8s timeout: {sum(r['openai']['ms'] > PROD_TIMEOUT_MS for r in paired)}")


async def main() -> None:
    settings = Settings.from_env()
    missing = [k for k, v in settings.keys_configured.items() if not v]
    if missing:
        sys.exit(f"missing key(s) in .env: {missing}")
    data = load_data(settings.data_dir)
    features = [f for f in sys.argv[1:] if f in ("recat", "prioritiser")] or ["recat", "prioritiser"]
    oa, jev = OpenAIClient(settings), JevClient(settings)
    print(f"OpenAI model={settings.openai_model} (default reasoning effort)   Jev model={settings.jev_model}")
    if data.user_templates_stand_in:
        print("NOTE: OpenAI user messages use stand-in templates until prod's templates are pasted into data/*_user_template.txt")
    out = {}
    try:
        for f in features:
            out[f] = await run_feature(f, data, oa, jev)
    finally:
        await oa.aclose()
        await jev.aclose()
    dest = ROOT / "scripts" / "out" / f"compare_{time.strftime('%Y%m%d_%H%M%S')}.json"
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text(json.dumps(out, indent=2, ensure_ascii=False, default=str), encoding="utf-8")
    print(f"\ndetails -> {dest.relative_to(ROOT)}")


if __name__ == "__main__":
    asyncio.run(main())
