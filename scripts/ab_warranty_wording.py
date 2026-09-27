"""A/B the wording of Jev's 87 warranty questions: "long" (fault named three times) vs "short" (named once).
Same comment, same other questions; both versions sent at the same time for each recat preset.

    .venv/Scripts/python scripts/ab_warranty_wording.py [rounds]
"""
from __future__ import annotations

import asyncio
import statistics
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from dotenv import load_dotenv  # noqa: E402

load_dotenv(ROOT / ".env")

from app.config import Settings, load_data  # noqa: E402
from app.providers import JevClient  # noqa: E402
from app.recat import interpret_jev, jev_plan  # noqa: E402

WORDINGS = ("long", "short")


def label(c: dict) -> str:
    return " > ".join(x for x in (c["query"], c["sub_query"], c["query_heading"]) if x)


async def ask(jev: JevClient, plan, comment: str) -> dict:
    t0 = time.perf_counter()
    answers, usage = await jev.ask_many({"customer_comment": comment}, plan.questions)
    ms = (time.perf_counter() - t0) * 1000
    return {"ms": ms, "usage": usage, "answer": interpret_jev(plan, answers)}


async def main() -> None:
    rounds = int(sys.argv[1]) if len(sys.argv) > 1 else 1
    settings = Settings.from_env()
    data = load_data(settings.data_dir)
    plans = {w: jev_plan(data.taxonomy, w) for w in WORDINGS}
    jev = JevClient(settings)
    stats = {w: {"tokens": [], "cost": [], "ms": []} for w in WORDINGS}
    same = total = 0
    try:
        for pr in data.presets["recat"]:
            print(f"\n[{pr['id']}] {pr['comment']}")
            for r in range(rounds):
                res = dict(zip(WORDINGS, await asyncio.gather(*(ask(jev, plans[w], pr["comment"]) for w in WORDINGS))))
                for w in WORDINGS:
                    x = res[w]
                    stats[w]["tokens"].append(x["usage"].input_tokens)
                    stats[w]["cost"].append(x["usage"].cost)
                    stats[w]["ms"].append(x["ms"])
                    cats = "; ".join(f"{label(c)} ({c['p']:.2f})" for c in x["answer"]["categories"]) or "no match"
                    flag = "  [low confidence]" if x["answer"]["low_confidence"] else ""
                    print(f"  {w:5s} in={x['usage'].input_tokens:5d} {x['ms']:5.0f} ms  {cats}{flag}")
                a, b = ({label(c) for c in res[w]["answer"]["categories"]} for w in WORDINGS)
                same += a == b
                total += 1
                if a != b:
                    print(f"        differs: only long {sorted(a - b) or '-'} | only short {sorted(b - a) or '-'}")
    finally:
        await jev.aclose()

    print("\n--- summary ---")
    for w in WORDINGS:
        s = stats[w]
        print(f"  {w:5s} avg input {statistics.mean(s['tokens']):6.0f} tok   avg cost ${statistics.mean(s['cost']):.6f}   median {statistics.median(s['ms']):4.0f} ms")
    saved = 1 - statistics.mean(stats["short"]["tokens"]) / statistics.mean(stats["long"]["tokens"])
    print(f"  short saves {saved:.0%} of input tokens; identical category sets in {same}/{total} runs")


if __name__ == "__main__":
    asyncio.run(main())
