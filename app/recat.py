"""Ticket recategorisation: read a customer's comment and place it in the right category.

Both sides get only the comment and the full category list (all 170), no other ticket data.
OpenAI: the production prompt and response schema, called the way prod calls it.
Jev: one fused request that asks every branch of the taxonomy at once; Python picks the path.
"""
from __future__ import annotations

import json
from dataclasses import dataclass

from .config import NOT_LISTED, Data, render
from .providers import JevClient, OpenAIClient

WARRANTY_THRESHOLD = 0.55
LOW_CONFIDENCE = 0.45
NONE_KEY = "qn"
TEMPERATURE = 0.3  # prod's recategorisation temperature


@dataclass(frozen=True)
class Leaf:
    query: str
    sub_query: str
    query_heading: str

    def as_dict(self) -> dict:
        return {"query": self.query, "sub_query": self.sub_query, "query_heading": self.query_heading}

    @property
    def label(self) -> str:
        """Sub > Heading, the part below the query."""
        return f"{self.sub_query} > {self.query_heading}" if self.query_heading else self.sub_query

    @property
    def is_catch_all(self) -> bool:
        return NOT_LISTED.lower() in (self.sub_query.lower(), self.query_heading.lower())


def is_warranty(query: str) -> bool:
    return query.strip().lower() == "warranty"


def leaves(taxonomy: dict) -> list[Leaf]:
    out = []
    for q, subs in taxonomy.items():
        for s, heads in subs.items():
            out.extend(Leaf(q, s, h) for h in heads) if heads else out.append(Leaf(q, s, ""))
    return out


def triple(d: dict) -> tuple[str, str, str]:
    return (d.get("query") or "", d.get("sub_query") or "", d.get("query_heading") or "")


# ---------------------------------------------------------------- OpenAI

# The production response schema minus extra_details, closed for strict mode.
_TRIPLE = {
    "type": "object",
    "properties": {"query": {"type": "string"}, "sub_query": {"type": "string"}, "query_heading": {"type": "string"}},
    "required": ["query", "sub_query", "query_heading"],
    "additionalProperties": False,
}
SCHEMA = {
    "type": "object",
    "properties": {
        "ticket_id": {"type": "string"},
        "is_warranty": {"type": "boolean"},
        "recategorised": {"type": "boolean"},
        "new_query": {"type": "string"},
        "new_subquery": {"type": "string"},
        "new_query_heading": {"type": "string"},
        "matched_issues": {"type": "array", "items": _TRIPLE},
    },
    "required": ["ticket_id", "is_warranty", "recategorised", "new_query", "new_subquery", "new_query_heading", "matched_issues"],
    "additionalProperties": False,
}


def openai_messages(data: Data, comment: str) -> tuple[str, str]:
    """System: the prod prompt with the queries list. User: the prompt's Ticket Data block, holding only the comment."""
    taxonomy = json.dumps(data.taxonomy, separators=(",", ":"), ensure_ascii=False)
    system = data.recat_prompt.replace("{TAXONOMY}", taxonomy)
    user = render(data.recat_user_template, {"ticket_json": json.dumps([{"comment": comment}], ensure_ascii=False, indent=2)})
    return system, user


def normalise_openai(raw: object, valid: set[tuple[str, str, str]]) -> dict:
    if isinstance(raw, list):
        raw = raw[0] if raw else {}
    if not isinstance(raw, dict):
        raw = {}
    warranty = bool(raw.get("is_warranty"))
    if warranty:
        cats = [triple(m) for m in raw.get("matched_issues") or [] if isinstance(m, dict)]
    else:
        cats = [(raw.get("new_query") or "", raw.get("new_subquery") or "", raw.get("new_query_heading") or "")]
    cats = [c for c in cats if any(c)]  # empty = the model found no match
    categories = [{"query": q, "sub_query": s, "query_heading": h, "valid": (q, s, h) in valid} for q, s, h in dict.fromkeys(cats)]
    return {"is_warranty": warranty and bool(categories), "placed": bool(categories), "categories": categories}


async def run_openai(client: OpenAIClient, data: Data, comment: str, model: str | None = None) -> tuple[dict, dict]:
    system, user = openai_messages(data, comment)
    raw, usage = await client.complete_json(system, user, "ticket_recategorisation", SCHEMA, TEMPERATURE, model)
    valid = {(l.query, l.sub_query, l.query_heading) for l in leaves(data.taxonomy)}
    return normalise_openai(raw, valid), usage.to_dict()


# ---------------------------------------------------------------- Jev

@dataclass
class JevPlan:
    questions: dict
    queries: list[str]
    warranty: list[Leaf]  # index i -> question w{i}
    leaf_options: dict[int, list[Leaf]]  # query index -> options o0.. of question leaf{i}


def _option_text(l: Leaf) -> str:
    """Jev reads option descriptions, so the catch-alls are reworded; keys still map back to the exact leaf."""
    if not l.is_catch_all:
        return l.label
    if l.sub_query.lower() == NOT_LISTED.lower():
        return f"Something else about {l.query}, not covered by any other option"
    return f"{l.sub_query} > something else, not covered by the other {l.sub_query} options"


def warranty_question(l: Leaf, wording: str) -> dict:
    if wording == "short":  # the fault named once; ~49 tokens instead of ~74
        return {
            "type": "noul",
            "instructions": f"Fault: {l.label}",
            "criteria": {"true": "The customer reports this fault.", "false": "The customer does not report this fault."},
        }
    return {
        "type": "noul",
        "instructions": f"The customer is reporting this specific fault: {l.label}",
        "criteria": {"true": f"The comment describes {l.label}.", "false": f"The comment does not describe {l.label}."},
    }


def jev_plan(taxonomy: dict, wording: str = "short") -> JevPlan:
    """Every question in one request: which area; then, for every area at once, which leaf;
    plus a yes/no per warranty fault (warranty is multi-label, and a choice returns only one option)."""
    all_leaves = leaves(taxonomy)
    queries = list(taxonomy)
    subs = lambda q: [s for s in taxonomy[q] if s.lower() != NOT_LISTED.lower()][:10]
    criteria = {f"q{i}": f"{q}: " + "; ".join(subs(q)) for i, q in enumerate(queries)}
    criteria[NONE_KEY] = "None of these: the comment is too vague or unrelated to place in any support area."
    questions: dict = {
        "query": {
            "type": "choice",
            "instructions": "Which support area does this customer comment belong to. Warranty covers mechanical, electrical and physical faults with the car itself.",
            "criteria": criteria,
        }
    }
    warranty = [l for l in all_leaves if is_warranty(l.query)]
    for i, l in enumerate(warranty):
        questions[f"w{i}"] = warranty_question(l, wording)
    leaf_options: dict[int, list[Leaf]] = {}
    for i, q in enumerate(queries):
        if is_warranty(q):
            continue
        opts = [l for l in all_leaves if l.query == q]
        leaf_options[i] = opts
        questions[f"leaf{i}"] = {
            "type": "choice",
            "instructions": f"Assuming this comment is about '{q}', which sub-query and heading fits best.",
            "criteria": {f"o{j}": _option_text(l) for j, l in enumerate(opts)},
        }
    return JevPlan(questions, queries, warranty, leaf_options)


def _conf(answer: dict) -> float:
    return float(answer.get("confidence", (answer.get("probabilities") or {}).get(answer.get("choice"), 0.0)))


def interpret_jev(plan: JevPlan, answers: dict) -> dict:
    qa = answers["query"]
    key = qa["choice"]
    name = lambda k: "None of these" if k == NONE_KEY else plan.queries[int(k[1:])]
    query_probabilities = {name(k): float(v) for k, v in (qa.get("probabilities") or {}).items()}
    query_conf = _conf(qa)
    low = query_conf < LOW_CONFIDENCE

    categories: list[dict] = []
    if key == NONE_KEY:
        query, warranty = None, False  # nothing fits
    else:
        qi = int(key[1:])
        query = plan.queries[qi]
        warranty = is_warranty(query)
        if warranty:
            scored = sorted(((float(answers[f"w{i}"]["noul"]), l) for i, l in enumerate(plan.warranty)), key=lambda x: -x[0])
            hits = [(p, l) for p, l in scored if p >= WARRANTY_THRESHOLD]
            if not hits and scored:
                hits, low = [scored[0]], True
            categories = [{**l.as_dict(), "p": round(p, 4)} for p, l in hits]
        else:
            la = answers[f"leaf{qi}"]
            leaf_conf = _conf(la)
            low = low or leaf_conf < LOW_CONFIDENCE
            categories = [{**plan.leaf_options[qi][int(la["choice"][1:])].as_dict(), "p": round(leaf_conf, 4)}]

    return {
        "is_warranty": warranty,
        "placed": bool(categories),
        "categories": categories,
        "low_confidence": low,
        "query": query,
        "query_confidence": round(query_conf, 4),
        "query_probabilities": query_probabilities,
        "questions": len(plan.questions),
    }


async def run_jev(client: JevClient, data: Data, comment: str) -> tuple[dict, dict]:
    plan = jev_plan(data.taxonomy)
    answers, usage = await client.ask_many({"customer_comment": comment}, plan.questions)
    answer = interpret_jev(plan, answers)
    answer["strategy"] = usage.strategy
    return answer, usage.to_dict()


# ---------------------------------------------------------------- compare

def compare(jev: dict, openai: dict) -> dict:
    a = {triple(c) for c in jev["categories"]}
    b = {triple(c) for c in openai["categories"]}
    union = a | b
    warranty_agrees = jev["is_warranty"] == openai["is_warranty"]
    return {
        "warranty_agrees": warranty_agrees,
        "exact": a == b,
        "overlap": round(len(a & b) / len(union), 4) if union else 1.0,
        "agrees": warranty_agrees and (bool(a & b) or not union),  # both finding no match counts as agreeing
    }
