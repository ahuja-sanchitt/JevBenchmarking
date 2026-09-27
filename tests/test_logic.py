"""Feature logic: compose precedence, data integrity, Jev fused/split, no-match handling, OpenAI cost."""
from __future__ import annotations

import asyncio
import json

import pytest

from app import prioritiser, recat
from app.config import DATA_DIR, load_data
from app.providers import JevClient, OpenAIClient
from tests.conftest import JevMock, OpenAIMock

DATA = load_data(DATA_DIR)
LOW = {k: 0.02 for k in prioritiser.SIGNALS}
AC_COMMENT = "The air conditioning is not cooling and there is a foul smell every time I switch it on."


def p(**over):
    return LOW | over


# ---------------------------------------------------------------- 1. urgency thresholds and precedence
@pytest.mark.parametrize("probs,expected", [
    (p(), (False, "none")),                                                   # nothing fires
    (p(time_sensitive=0.9), (True, "time_sensitive")),
    (p(time_sensitive=0.54), (False, "none")),                                # just under 0.55
    (p(escalation=0.46), (True, "escalation")),                               # lower bar: 0.45
    (p(safety=0.46), (True, "safety")),                                       # lower bar: 0.45
    (p(unresolved=0.50), (False, "none")),                                    # 0.50 is under unresolved's 0.55
    (p(escalation=0.9, safety=0.95), (True, "escalation")),                   # first in order wins, not the highest
])
def test_compose_urgency(probs, expected):
    out = prioritiser.compose(probs)
    assert (out["urgent"], out["signal"]) == expected
    assert out["reason"]


# ---------------------------------------------------------------- 2. data integrity
def test_taxonomy_counts_and_sanitised():
    blob = json.dumps(DATA.taxonomy).lower() + DATA.recat_prompt.lower() + DATA.prioritiser_prompt.lower()
    # The forbidden brand strings live in a gitignored file so the repo never names them.
    local = DATA_DIR.parent / "scripts" / "renames.local.json"
    for brand in json.loads(local.read_text(encoding="utf-8"))["forbidden"] if local.exists() else []:
        assert brand not in blob
    all_leaves = recat.leaves(DATA.taxonomy)
    assert len(DATA.taxonomy) == 13
    assert len(all_leaves) == 170
    assert sum(recat.is_warranty(l.query) for l in all_leaves) == 87
    for q in DATA.taxonomy:
        assert sum(l.query == q for l in all_leaves) >= 2, q
    assert "extra_details" not in DATA.recat_prompt


def test_presets_are_well_formed():
    for pr in DATA.presets["recat"]:
        assert pr["comment"].strip() and set(pr) == {"id", "label", "comment"}, pr["id"]
    for pr in DATA.presets["prioritiser"]:
        assert pr["message"].strip() and set(pr) == {"id", "label", "message"}, pr["id"]


def _closed(schema):
    if schema.get("type") == "object":
        assert schema.get("additionalProperties") is False
        assert set(schema["required"]) == set(schema["properties"])
    for sub in schema.get("properties", {}).values():
        _closed(sub)
    if "items" in schema:
        _closed(schema["items"])


def test_jev_and_openai_share_the_urgency_definitions():
    # Fairness: every Jev question's wording appears in the OpenAI prompt, in the same order.
    positions = []
    for key, (ins, true, _false) in prioritiser.SIGNALS.items():
        assert key in DATA.prioritiser_prompt and true in DATA.prioritiser_prompt, key
        positions.append(DATA.prioritiser_prompt.index(true))
    assert positions == sorted(positions)
    assert set(prioritiser.jev_questions()) == set(prioritiser.SIGNALS)


def test_strict_schemas_close_every_object():
    _closed(recat.SCHEMA)
    _closed(prioritiser.SCHEMA)
    assert "extra_details" not in recat.SCHEMA["properties"]


def test_jev_plan_rewords_catch_alls_and_maps_back():
    plan = recat.jev_plan(DATA.taxonomy)
    assert len(plan.questions) == 1 + 87 + 12
    text = json.dumps(plan.questions)
    assert "My issue is not listed here" not in text
    qi = plan.queries.index("Insurance")
    opts = plan.leaf_options[qi]
    j = next(i for i, l in enumerate(opts) if l.sub_query == "Claim" and l.query_heading == "My issue is not listed here")
    assert "something else" in plan.questions[f"leaf{qi}"]["criteria"][f"o{j}"]


# ---------------------------------------------------------------- 3–5. Jev recat
def run(coro):
    return asyncio.run(coro)


async def _jev_recat(settings, mock, comment):
    client = JevClient(settings, mock.transport())
    try:
        return await recat.run_jev(client, DATA, comment)
    finally:
        await client.aclose()


def test_jev_recat_warranty_fused_multilabel(settings):
    mock = JevMock(delay=0)
    answer, usage = run(_jev_recat(settings, mock, AC_COMMENT))
    assert len(mock.calls) == 1 and usage["strategy"] == "fused" and usage["calls"] == 1
    assert mock.calls[0]["state"] == {"customer_comment": AC_COMMENT}
    assert answer["is_warranty"]
    assert len(answer["categories"]) >= 2 and all(c["query"] == "Warranty" for c in answer["categories"])
    assert answer["placed"]
    assert usage["cost"] > 0 and usage["cost_reported"] is True
    assert usage["served_model"] == "typesafe/jev-1.13-20260917"


def test_jev_recat_refusal_splits_in_parallel(settings):
    mock = JevMock(delay=0.05, max_questions=40)
    answer, usage = run(_jev_recat(settings, mock, AC_COMMENT))
    assert usage["strategy"].startswith("split")
    assert usage["calls"] == 4  # 100 -> 50+50 (refused) -> 4 x 25
    assert answer["is_warranty"] and len(answer["categories"]) >= 2


def test_jev_recat_non_warranty_single_valid_leaf(settings):
    answer, _ = run(_jev_recat(settings, JevMock(delay=0), "My fastag got blacklisted at the toll plaza"))
    assert not answer["is_warranty"]
    assert [recat.triple(c) for c in answer["categories"]] == [("Fastag", "Blacklisted fastag", "")]
    valid = {(l.query, l.sub_query, l.query_heading) for l in recat.leaves(DATA.taxonomy)}
    assert recat.triple(answer["categories"][0]) in valid


def test_jev_missing_cost_falls_back_and_is_flagged(settings):
    _, usage = run(_jev_recat(settings, JevMock(delay=0, omit_cost=True), AC_COMMENT))
    assert usage["cost_reported"] is False
    assert usage["cost"] == pytest.approx(usage["input_tokens"] * 0.042e-6)


# ---------------------------------------------------------------- 6. placement and "no match"
def test_placement_and_no_match_both_sides():
    plan = recat.jev_plan(DATA.taxonomy)
    qi = plan.queries.index("Insurance")
    opts = plan.leaf_options[qi]
    j = next(j for j, l in enumerate(opts) if l.query_heading == "Need to register new claim")
    answers = {f"w{i}": {"noul": 0.01} for i in range(len(plan.warranty))}
    answers |= {"query": {"choice": f"q{qi}", "confidence": 0.9}, f"leaf{qi}": {"choice": f"o{j}", "confidence": 0.9}}
    placed = recat.interpret_jev(plan, answers)
    assert placed["placed"] and [recat.triple(c) for c in placed["categories"]] == [("Insurance", "Claim", "Need to register new claim")]

    none = recat.interpret_jev(plan, {"query": {"choice": "qn", "confidence": 0.7}})
    assert none["placed"] is False and none["categories"] == []

    valid = {(l.query, l.sub_query, l.query_heading) for l in recat.leaves(DATA.taxonomy)}
    empty = recat.normalise_openai({"is_warranty": False, "new_query": "", "new_subquery": "", "new_query_heading": ""}, valid)
    assert empty["placed"] is False and empty["categories"] == []
    assert recat.compare(none, empty)["agrees"] is True  # both finding no match is agreement
    moved = recat.normalise_openai([{"is_warranty": False, "new_query": "Fastag", "new_subquery": "Blacklisted fastag", "new_query_heading": ""}], valid)
    assert moved["placed"] is True and moved["categories"][0]["valid"] is True
    invented = recat.normalise_openai({"is_warranty": True, "matched_issues": [{"query": "Warranty", "sub_query": "Engine", "query_heading": "Made up"}]}, valid)
    assert invented["categories"][0]["valid"] is False


# ---------------------------------------------------------------- 7. OpenAI cost + fallback
async def _openai(settings, mock):
    client = OpenAIClient(settings, mock.transport())
    try:
        return await recat.run_openai(client, DATA, "My fastag is blacklisted")
    finally:
        await client.aclose()


def test_openai_cost_includes_reasoning_at_output_rate(settings):
    answer, usage = run(_openai(settings, OpenAIMock(delay=0)))
    assert usage["reasoning_tokens"] == 560 and usage["completion_tokens"] == 620
    assert usage["cost"] == pytest.approx((2100 * 2.00 + 620 * 8.00) / 1e6)
    assert usage["cost_reported"] is False
    assert answer["categories"][0]["valid"] is True


def test_openai_recat_matches_prod_message_split(settings):
    mock = OpenAIMock(delay=0)
    run(_openai(settings, mock))
    body = mock.calls[0]
    system, user = body["messages"]
    assert (system["role"], user["role"]) == ("system", "user")
    # The full queries list lives in the system prompt; the user message carries only the comment.
    assert "{TAXONOMY}" not in system["content"] and '"Blacklisted fastag"' in system["content"]
    assert "Seller-Miscellaneous" in system["content"]
    assert user["content"].startswith("Ticket Data – JSON:") and '"comment": "My fastag is blacklisted"' in user["content"]
    assert "ticket_id" not in user["content"] and "sub_query" not in user["content"]
    assert body["temperature"] == 0.3
    assert "reasoning_effort" not in body


def test_openai_prioritiser_matches_prod_message_split(settings):
    async def go():
        client = OpenAIClient(settings, mock.transport())
        try:
            return await prioritiser.run_openai(client, DATA, {"message": "theek hai"})
        finally:
            await client.aclose()

    mock = OpenAIMock(delay=0)
    run(go())
    system, user = mock.calls[0]["messages"]
    assert system["content"].endswith(json.dumps(prioritiser.SCHEMA))  # prod appends the schema
    assert user["content"].strip() == "Customer message:\ntheek hai"  # only the message, nothing else
    assert mock.calls[0]["temperature"] == 0.2


def test_openai_retries_without_temperature_when_rejected(settings):
    mock = OpenAIMock(delay=0, reject_temperature=True)
    _, usage = run(_openai(settings, mock))
    assert ["temperature" in c for c in mock.calls] == [True, False]
    assert usage["temperature_dropped"] is True


def test_openai_json_object_fallback_after_schema_400(settings):
    mock = OpenAIMock(delay=0, reject_schema=True)
    answer, usage = run(_openai(settings, mock))
    assert [c["response_format"]["type"] for c in mock.calls] == ["json_schema", "json_object"]
    assert usage["fallback_json_object"] is True
    assert answer["categories"][0]["query"] == "Fastag"
