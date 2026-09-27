"""Build data/taxonomy.json and data/customer_types.json from the queries spreadsheet.

    .venv/Scripts/python scripts/build_taxonomy.py path/to/queries.xlsx

Columns: customer_type, query, sub_query, query_heading (empty = no heading).
Sanitises brand and internal jargon, and merges casing duplicates such as
"RC Status" / "RC status" into one sub-query. customer_types.json maps each
query to BUYER or SELLER, so a ticket is only offered its own type's categories.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import openpyxl

DATA = Path(__file__).resolve().parent.parent / "data"
OUT = DATA / "taxonomy.json"
TYPES_OUT = DATA / "customer_types.json"

# Public demo: no brand names or internal jargon. The mapping itself names the brand, so it lives in a
# gitignored local file (scripts/renames.local.json: {"rename": {...}, "forbidden": [...]}).
LOCAL = Path(__file__).resolve().parent / "renames.local.json"
RENAME: dict[str, str] = json.loads(LOCAL.read_text(encoding="utf-8"))["rename"] if LOCAL.exists() else {}


def clean(s: object) -> str:
    s = " ".join(str(s or "").split())
    return RENAME.get(s, s)


def build(path: str) -> tuple[dict[str, dict[str, list[str]]], dict[str, str]]:
    ws = openpyxl.load_workbook(path, read_only=True, data_only=True).worksheets[0]
    rows = list(ws.iter_rows(values_only=True))
    header = [str(h).strip().lower() for h in rows[0]]
    ti, qi, si, hi = header.index("customer_type"), header.index("query"), header.index("sub_query"), header.index("query_heading")

    tax: dict[str, dict[str, list[str]]] = {}
    types: dict[str, str] = {}
    canon: dict[tuple[str, str], str] = {}  # (query, lower(sub)) -> first spelling seen
    for r in rows[1:]:
        q, s, h = clean(r[qi]), clean(r[si]), clean(r[hi])
        if not q or not s:
            continue
        t = clean(r[ti]).upper()
        if types.setdefault(q, t) != t:
            raise ValueError(f"query {q!r} appears under both {types[q]} and {t}")
        s = canon.setdefault((q, s.lower()), s)
        heads = tax.setdefault(q, {}).setdefault(s, [])
        if h and h.lower() not in (x.lower() for x in heads):
            heads.append(h)
    return tax, types


if __name__ == "__main__":
    tax, types = build(sys.argv[1])
    OUT.write_text(json.dumps(tax, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    TYPES_OUT.write_text(json.dumps(types, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    n = sum(max(len(h), 1) for subs in tax.values() for h in subs.values())
    w = sum(max(len(h), 1) for h in tax.get("Warranty", {}).values())
    subs = sum(len(s) for s in tax.values())
    per_type = {t: sum(max(len(h), 1) for q, s in tax.items() if types[q] == t for h in s.values()) for t in sorted(set(types.values()))}
    print(f"{len(tax)} queries, {subs} sub-queries, {n} leaves, {w} warranty leaves, per type {per_type} -> {OUT.parent}")
