"""Probe the real Jev Decisions API to settle three design questions before choosing a recat strategy.

    .venv/Scripts/python scripts/jev_probe.py            # all three tests
    .venv/Scripts/python scripts/jev_probe.py tokens     # or: flat, fused

1. tokens  How Jev bills input: is the state counted once per request or once per question?
2. flat    The POC design (one choice over every category) on each recat preset,
           with the production prompt in the state (as the POC did) vs the comment only.
3. fused   Does a 100-question request go through, and what does it cost?

Needs OPENROUTER_API_KEY in .env (or the environment). Total spend is well under $0.01.
Results are also written to scripts/out/jev_probe_<timestamp>.json.
"""
from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.config import load_data  # noqa: E402
from app.recat import is_warranty, jev_plan, leaves  # noqa: E402

URL_PATH = "/alpha/decisions"
MODEL = os.environ.get("JEV_MODEL", "typesafe/jev-1.13")
EXTRA_WARRANTY_P = 0.15  # candidate cut-off for multi-fault warranty in the flat design

POC_PICK = ("Which category best fits what the customer wrote in `ticket.comment`? Always pick the closest one, "
            "even if it is the ticket's current category.")
LEAN_PICK = ("Which support category matches the problem the customer describes in customer_comment? "
             "Warranty covers mechanical, electrical and physical faults with the car itself. "
             "Pick the category for the customer's main concern.")


def load_env() -> None:
    env = ROOT / ".env"
    if not env.exists():
        return
    for line in env.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        v = v.split(" #", 1)[0].strip().strip('"').strip("'")
        if v and k.strip() not in os.environ:
            os.environ[k.strip()] = v


class Jev:
    def __init__(self, key: str, base: str):
        self.http = httpx.Client(base_url=base.rstrip("/"), timeout=60, headers={"Authorization": f"Bearer {key}"})
        self.spent = 0.0

    def ask(self, state: dict, questions: dict) -> dict:
        t0 = time.perf_counter()
        r = self.http.post(URL_PATH, json={"model": MODEL, "state": state, "questions": questions})
        ms = (time.perf_counter() - t0) * 1000
        if r.status_code != 200:
            return {"ok": False, "status": r.status_code, "ms": ms, "error": r.text[:400]}
        data = r.json()
        u = data.get("usage") or {}
        self.spent += float(u.get("cost") or 0)
        return {"ok": True, "ms": ms, "model": data.get("model"), "answers": data.get("answers") or {},
                "input_tokens": u.get("input_tokens"), "output_tokens": u.get("output_tokens"), "cost": u.get("cost")}


def est_tokens(obj) -> int:
    return len(json.dumps(obj, ensure_ascii=False)) // 4


def triple_label(l) -> str:
    return " > ".join(x for x in (l.query, l.sub_query, l.query_heading) if x)


# ---------------------------------------------------------------- 1. token accounting
def test_tokens(jev: Jev, data) -> dict:
    comment = data.presets["recat"][0]["comment"]
    topics = ["air conditioning", "brakes", "a refund", "the RC", "a fastag", "insurance", "the battery",
              "the suspension", "a challan", "the invoice"]
    noul = lambda t: {"type": "noul", "instructions": f"The customer mentions {t}",
                      "criteria": {"true": f"The comment mentions {t}.", "false": f"The comment does not mention {t}."}}
    short = {"customer_comment": comment}
    long_ = {"customer_comment": " ".join([comment] * 5)}

    runs = {
        "1 question, short state": jev.ask(short, {"t0": noul(topics[0])}),
        "10 questions, short state": jev.ask(short, {f"t{i}": noul(t) for i, t in enumerate(topics)}),
        "1 question, 5x state": jev.ask(long_, {"t0": noul(topics[0])}),
        "10 questions, 5x state": jev.ask(long_, {f"t{i}": noul(t) for i, t in enumerate(topics)}),
    }
    print("\n=== 1. How Jev counts input tokens ===")
    print(f"comment ≈ {est_tokens(comment)} tokens by chars/4; one question ≈ {est_tokens(noul(topics[0]))}")
    for name, r in runs.items():
        print(f"  {name:28s} " + (f"input={r['input_tokens']:>6}  {r['ms']:6.0f} ms" if r["ok"] else f"FAILED {r['status']}: {r['error'][:120]}"))

    a, b, c, d = (runs[k].get("input_tokens") for k in runs)
    verdict = None
    if None not in (a, b, c, d):
        extra_state_1q = c - a          # cost of 4 extra copies of the comment, 1 question
        extra_state_10q = d - b         # same, with 10 questions
        ratio = extra_state_10q / extra_state_1q if extra_state_1q else float("nan")
        verdict = "per question" if ratio > 5 else "per request"
        print(f"  extra state cost: 1 question +{extra_state_1q}, 10 questions +{extra_state_10q}  (ratio {ratio:.1f})")
        print(f"  => the state is billed {verdict.upper()}"
              + ("  (a 100-question request pays for the comment 100 times)" if verdict == "per question" else ""))
    return {"runs": runs, "state_billed": verdict}


# ---------------------------------------------------------------- 2. flat choice, POC style
def test_flat(jev: Jev, data) -> dict:
    all_leaves = leaves(data.taxonomy)
    # BUYER/SELLER per query, from the spreadsheet's customer_type column (scripts/build_taxonomy.py).
    types = json.loads((ROOT / "data" / "customer_types.json").read_text(encoding="utf-8"))
    policy = data.recat_prompt.replace("{TAXONOMY}", "(the valid categories are the options of `category`)")
    rows = []
    print("\n=== 2. Flat choice over every category (POC design): with prompt vs comment only ===")
    for pr in data.presets["recat"]:
        ctype = types[pr["original"]["query"]]
        seller = ctype == "SELLER"
        combos = [l for l in all_leaves if types[l.query] == ctype]
        labels = {f"c{i}": triple_label(l) for i, l in enumerate(combos)}
        ticket = {"ticket_id": "demo-1", **pr["original"], "comment": pr["comment"]}
        original_label = triple_label(type(combos[0])(**pr["original"]))

        variants = {
            "with prompt": ({"policy": policy, "ticket": ticket}, POC_PICK),
            "comment only": ({"customer_comment": pr["comment"]}, LEAN_PICK),
        }
        print(f"\n[{pr['id']}] {pr['label']}  ({'seller' if seller else 'buyer'}, {len(labels)} options)")
        print(f"    comment: {pr['comment']}")
        print(f"    filed under: {original_label}")
        for vname, (state, instructions) in variants.items():
            r = jev.ask(state, {"category": {"type": "choice", "instructions": instructions, "criteria": labels}})
            row = {"preset": pr["id"], "variant": vname, "options": len(labels), **{k: r.get(k) for k in ("ok", "ms", "input_tokens", "cost", "model", "status", "error")}}
            if r["ok"]:
                a = r["answers"]["category"]
                probs = sorted(((labels[k], float(v)) for k, v in (a.get("probabilities") or {}).items()), key=lambda x: -x[1])
                pick = labels.get(a.get("choice"))
                extra = [(l, p) for l, p in probs[1:] if p >= EXTRA_WARRANTY_P and l.startswith("Warranty") and pick.startswith("Warranty")]
                row |= {"pick": pick, "confidence": a.get("confidence"), "top3": probs[:3], "extra_warranty": extra,
                        "moved": pick != original_label}
                print(f"    {vname:13s} in={r['input_tokens']:>6}  {r['ms']:6.0f} ms  ${r['cost'] or 0:.6f}  conf={a.get('confidence')}")
                print(f"                  pick: {pick}  ({'moved' if row['moved'] else 'kept'})")
                for l, p in probs[1:3]:
                    print(f"                        {p:.2f}  {l}")
                if extra:
                    print(f"                  + extra warranty faults ≥{EXTRA_WARRANTY_P}: " + "; ".join(f"{l} ({p:.2f})" for l, p in extra))
            else:
                print(f"    {vname:13s} FAILED {r['status']}: {r['error'][:200]}")
            rows.append(row)

    ok = [r for r in rows if r["ok"]]
    for v in ("with prompt", "comment only"):
        vs = [r for r in ok if r["variant"] == v]
        if vs:
            print(f"\n  {v:13s} avg input={sum(r['input_tokens'] for r in vs) / len(vs):7.0f}  "
                  f"avg latency={sum(r['ms'] for r in vs) / len(vs):6.0f} ms  avg cost=${sum(r['cost'] or 0 for r in vs) / len(vs):.6f}")
    pairs = {}
    for r in ok:
        pairs.setdefault(r["preset"], {})[r["variant"]] = r.get("pick")
    same = sum(1 for p in pairs.values() if len(p) == 2 and p["with prompt"] == p["comment only"])
    print(f"  same pick with and without the prompt: {same}/{len(pairs)}")
    return {"rows": rows}


# ---------------------------------------------------------------- 3. fused (100 questions)
def test_fused(jev: Jev, data) -> dict:
    plan = jev_plan(data.taxonomy)
    pr = data.presets["recat"][0]
    r = jev.ask({"customer_comment": pr["comment"]}, plan.questions)
    print(f"\n=== 3. Fused request: {len(plan.questions)} questions in one call ({pr['id']}) ===")
    if r["ok"]:
        hits = sorted(((float(r["answers"][f"w{i}"]["noul"]), triple_label(l)) for i, l in enumerate(plan.warranty)), reverse=True)[:4]
        print(f"  accepted. input={r['input_tokens']}  {r['ms']:.0f} ms  ${r['cost'] or 0:.6f}  served={r['model']}")
        print(f"  area: {r['answers']['query'].get('choice')} conf={r['answers']['query'].get('confidence')}")
        for p, l in hits:
            print(f"    {p:.2f}  {l}")
    else:
        print(f"  REFUSED {r['status']}: {r['error'][:300]}")
    return {"questions": len(plan.questions), "result": {k: v for k, v in r.items() if k != "answers"}}


def main() -> None:
    load_env()
    key = os.environ.get("OPENROUTER_API_KEY")
    if not key:
        sys.exit("OPENROUTER_API_KEY is not set. Copy .env.example to .env and fill it in.")
    base = os.environ.get("OPENROUTER_BASE_URL", "https://openrouter.ai/api")
    wanted = set(sys.argv[1:]) or {"tokens", "flat", "fused"}
    data = load_data()
    jev = Jev(key, base)
    print(f"model={MODEL}  base={base}  tests={sorted(wanted)}")

    out = {}
    if "tokens" in wanted:
        out["tokens"] = test_tokens(jev, data)
    if "flat" in wanted:
        out["flat"] = test_flat(jev, data)
    if "fused" in wanted:
        out["fused"] = test_fused(jev, data)

    print(f"\nreported spend this run: ${jev.spent:.6f}")
    dest = ROOT / "scripts" / "out" / f"jev_probe_{time.strftime('%Y%m%d_%H%M%S')}.json"
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text(json.dumps(out, indent=2, ensure_ascii=False, default=str), encoding="utf-8")
    print(f"details -> {dest.relative_to(ROOT)}")


if __name__ == "__main__":
    main()
