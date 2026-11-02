"""Jev adapter contract tests with mocked HTTP. No real network or spend.

Fixtures follow the documented shapes for both providers (see app/decisions/jev.py). They are
sanitized examples written for this repo, not captured responses: verify against your account's
docs before enabling Jev.
"""

import json

import httpx
import pytest
import respx
from conftest import ALEX, CHAT, SAM, Harness, make_settings

from app.config import ConfigError
from app.decisions.jev import JevAuthError, JevDecisionProvider

SYSTEMONE = "https://api.typesafe.test/v1/systemone"
DECIDE = "https://jev.test/api/v1/decide"


def answer(intent="modify", conflict="none", action="search", conf=0.9):
    return {
        "model": "jev-1.14.0",
        "answers": {
            "intent": {"type": "choice", "choice": intent, "confidence": conf, "probabilities": {intent: conf}},
            "conflict": {"type": "choice", "choice": conflict, "confidence": conf, "probabilities": {conflict: conf}},
            "next_action": {"type": "choice", "choice": action, "confidence": conf, "probabilities": {action: conf}},
        },
        "usage": {"input_tokens": 180, "output_tokens": 3},
    }


def provider(client, schema="typesafe_systemone", **kw):
    url = SYSTEMONE if schema == "typesafe_systemone" else DECIDE
    base, endpoint = url.rsplit("/v1/", 1)
    return JevDecisionProvider(client, schema=schema, base_url=base, endpoint="/v1/" + endpoint,
                               api_key="test-key", model="jev-latest", **kw)


SNAP = {"session_id": "s1", "status": "collecting", "has_location": True, "cuisines": ["burger"], "has_shortlist": False}


@respx.mock
async def test_systemone_schema_request_and_normal_response():
    route = respx.post(SYSTEMONE).mock(return_value=httpx.Response(200, json=answer()))
    async with httpx.AsyncClient() as c:
        d = await provider(c).decide(SNAP, "maybe something lighter, call me at 480-555-1234")
    assert (d.intent, d.next_action, d.source) == ("modify", "search", "jev")
    req = route.calls[0].request
    body = json.loads(req.content)
    assert set(body) == {"model", "state", "questions"} and body["model"] == "jev-latest"
    assert set(body["questions"]) == {"intent", "conflict", "next_action"}
    assert all(q["type"] == "choice" and q["criteria"] for q in body["questions"].values())
    assert req.headers["authorization"] == "Bearer test-key"
    assert "480-555-1234" not in body["state"] and "[phone]" in body["state"]


@respx.mock
async def test_decide_schema_has_no_model_field():
    route = respx.post(DECIDE).mock(return_value=httpx.Response(200, json=answer()))
    async with httpx.AsyncClient() as c:
        await provider(c, schema="jevtypesafe_decide").decide(SNAP, "hmm")
    assert set(json.loads(route.calls[0].request.content)) == {"state", "questions"}


@respx.mock
async def test_low_confidence_asks_one_question():
    respx.post(SYSTEMONE).mock(return_value=httpx.Response(200, json=answer(conf=0.3)))
    async with httpx.AsyncClient() as c:
        d = await provider(c).decide(SNAP, "hmm")
    assert d.next_action == "ask_one_question" and d.error == "low_confidence"


@respx.mock
async def test_unknown_label_falls_back():
    respx.post(SYSTEMONE).mock(return_value=httpx.Response(200, json=answer(intent="order_pizza")))
    async with httpx.AsyncClient() as c:
        d = await provider(c).decide(SNAP, "hmm")
    assert d.source == "fallback" and d.error == "unknown_label_intent"


@pytest.mark.parametrize("response,error", [
    (httpx.Response(429), "rate_limited"),
    (httpx.Response(500), "upstream_500"),
    (httpx.Response(529), "upstream_529"),
    (httpx.Response(422, json={"detail": "bad"}), "bad_request_422"),
    (httpx.Response(200, text="not json"), "invalid_json"),
    (httpx.Response(200, json={"answers": "nope"}), "invalid_shape"),
])
@respx.mock
async def test_error_responses_fall_back_without_retry(response, error):
    route = respx.post(SYSTEMONE).mock(return_value=response)
    async with httpx.AsyncClient() as c:
        d = await provider(c).decide(SNAP, "hmm")
    assert d.source == "fallback" and d.error == error
    assert route.call_count == 1  # no paid retries


@respx.mock
async def test_timeout_falls_back():
    respx.post(SYSTEMONE).mock(side_effect=httpx.ReadTimeout("slow"))
    async with httpx.AsyncClient() as c:
        d = await provider(c).decide(SNAP, "hmm")
    assert d.error == "timeout"


@respx.mock
async def test_401_fails_closed():
    route = respx.post(SYSTEMONE).mock(return_value=httpx.Response(401))
    async with httpx.AsyncClient() as c:
        p = provider(c)
        with pytest.raises(JevAuthError):
            await p.decide(SNAP, "hmm")
        d = await p.decide(SNAP, "hmm again")
    assert d.error == "auth_error" and route.call_count == 1


@respx.mock
async def test_spend_guardrails_per_session_and_day():
    route = respx.post(SYSTEMONE).mock(return_value=httpx.Response(200, json=answer()))
    async with httpx.AsyncClient() as c:
        p = provider(c, max_calls_per_session=2, max_calls_per_day=3)
        for _ in range(3):
            await p.decide(SNAP, "x")
        d = await p.decide({**SNAP, "session_id": "s2"}, "x")
        d2 = await p.decide({**SNAP, "session_id": "s3"}, "x")
    assert route.call_count == 3
    assert d.error is None  # s2's first call is within the daily cap
    assert d2.error == "spend_guardrail"


def test_config_requires_key_and_schema(tmp_path):
    with pytest.raises((ConfigError, ValueError)):
        make_settings(tmp_path, decision_provider="jev", jev_enabled=True)
    with pytest.raises((ConfigError, ValueError)):
        make_settings(tmp_path, decision_provider="jev", jev_enabled=True, jev_api_key="k",
                      jev_base_url="https://x", jev_endpoint="/v1/decide")
    s = make_settings(tmp_path, decision_provider="jev", jev_enabled=True, jev_api_key="k", jev_schema="jevtypesafe_decide",
                      jev_base_url="https://x", jev_endpoint="/v1/decide")
    assert s.jev_active


def test_disabled_by_default(tmp_path):
    s = make_settings(tmp_path)
    assert not s.jev_active and s.decision_provider == "rules"


@respx.mock
async def test_orchestrator_only_calls_jev_for_ambiguous_active_food_messages(tmp_path):
    route = respx.post(DECIDE).mock(return_value=httpx.Response(200, json=answer(intent="clarify", action="ask_one_question")))
    async with httpx.AsyncClient() as c:
        jev = provider(c, schema="jevtypesafe_decide")
        h = Harness(tmp_path, decisions=jev)
        await h.onboard()
        await h.send(SAM, "anyone want food later?")  # dormant: never sent
        await h.send(ALEX, "Chef find dinner near Tempe")  # parsed locally
        await h.send(SAM, "halal for me")  # parsed locally
        await h.send(SAM, "lol the game last night")  # unrelated: never sent
        assert route.call_count == 0
        await h.send(SAM, "eh not feeling that, something different")  # ambiguous food-related
        assert route.call_count == 1
        assert h.last.startswith("What should I change")
        state = json.loads(route.calls[0].request.content)["state"]
        assert "Tempe" not in state and "halal" not in state and "+1555" not in state
        await h.send(SAM, "eh something different again")
        assert route.call_count == 2
        assert h.last.startswith("What should I change") and h.texts().count(h.last) == 1  # one question per version


@respx.mock
async def test_orchestrator_survives_jev_auth_failure(tmp_path):
    respx.post(DECIDE).mock(return_value=httpx.Response(401))
    async with httpx.AsyncClient() as c:
        h = Harness(tmp_path, decisions=provider(c, schema="jevtypesafe_decide"))
        await h.onboard()
        await h.send(ALEX, "Chef find dinner near Tempe")
        assert await h.send(SAM, "something different maybe") in ("clarify", "ignored_ambiguous")
        assert h.repo.db.one("SELECT COUNT(*) FROM audit_events WHERE action = 'jev_auth_failed'")[0] == 1
        assert h.repo.group_by_chat(CHAT)
