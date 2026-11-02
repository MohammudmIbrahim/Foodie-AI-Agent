"""Workflow primitives and the restaurant search workflow (custom logic agent pattern)."""

from __future__ import annotations

import asyncio
import dataclasses
from types import SimpleNamespace

import pytest
from conftest import ALEX, CHAT, FIXTURES, MIKE, SAM, Harness

import app.workflow.search as search
from app.domain.models import GeoPoint, Place, RankedPlace
from app.domain.replies import ALLERGY_CAVEAT
from app.restaurants.mock import FixturePlacesProvider
from app.workflow.base import Halt, Parallel, Review, ReviewLoop, Sequential, Step


def record(log: list[str], name: str, delay: float = 0.0, fail: bool = False):
    async def fn(state) -> None:
        log.append(f"start {name}")
        await asyncio.sleep(delay)
        if fail:
            raise RuntimeError(name)
        log.append(f"end {name}")
    return fn


async def test_sequential_runs_in_order():
    log: list[str] = []
    await Sequential("s", [Step("a", record(log, "a")), Step("b", record(log, "b"))]).run(None)
    assert log == ["start a", "end a", "start b", "end b"]


async def test_parallel_runs_together_and_tolerates_optional_failure():
    log: list[str] = []
    await Parallel("p", [Step("a", record(log, "a", 0.01)), Step("b", record(log, "b", fail=True), optional=True)]).run(None)
    assert log[:2] == ["start a", "start b"] and "end a" in log


async def test_parallel_required_failure_propagates():
    with pytest.raises(RuntimeError):
        await Parallel("p", [Step("a", record([], "a", fail=True)), Step("b", record([], "b"))]).run(None)


async def test_review_loop_revises_then_halts_at_round_limit():
    state = SimpleNamespace(rounds=0, review=Review())

    async def gen(st) -> None:
        st.rounds += 1

    async def approve_second(st) -> None:
        st.review = Review() if st.rounds == 2 else Review(drop={"x"})

    await ReviewLoop("r", Step("g", gen), Step("c", approve_second), max_rounds=2).run(state)
    assert state.rounds == 2

    async def never(st) -> None:
        st.review = Review(drop={"x"})

    state.rounds = 0
    with pytest.raises(Halt) as e:
        await ReviewLoop("r", Step("g", gen), Step("c", never), max_rounds=3).run(state)
    assert state.rounds == 3 and e.value.reason == "review_rejected"


def _place(pid: str, **kw) -> Place:
    return Place(provider="osm", provider_place_id=pid, name=f"Place {pid}", latitude=33.42, longitude=-111.94,
                 maps_url="https://maps.example/x", last_fetched_at="2026-10-26", **kw)


def _state(shortlist: list[RankedPlace], sources: list[Place], text: str, allergies: set[str] | None = None):
    plan = SimpleNamespace(allergies=allergies or set())
    return SimpleNamespace(places=sources, shortlist=shortlist, text=text, plan=plan, review=Review())


async def test_critic_drops_places_or_fields_not_from_the_provider():
    real = _place("a")
    ok = RankedPlace(place=real, score=1, distance_miles=1, confidence=1)
    invented = RankedPlace(place=_place("ghost"), score=1, distance_miles=1, confidence=1)
    embellished = RankedPlace(place=dataclasses.replace(_place("b"), rating=4.9, review_count=900), score=1, distance_miles=1, confidence=1)
    st = _state([ok, invented, embellished], [real, _place("b")], "list")
    await search.check_reply(st)
    assert st.review.drop == {"ghost", "b"} and not st.review.blocking


async def test_critic_blocks_missing_disclosures():
    r = RankedPlace(place=_place("a"), score=1, distance_miles=1, confidence=1, unverified=["halal"])
    st = _state([r], [r.place], "1. Place a", allergies={"peanut"})
    await search.check_reply(st)
    assert set(st.review.blocking) == {"unverified requirement not disclosed", "allergy caveat missing"}
    st = _state([r], [r.place], "halal not verified\n" + ALLERGY_CAVEAT, allergies={"peanut"})
    await search.check_reply(st)
    assert st.review.ok


async def test_embellished_option_is_removed_before_sending(h, monkeypatch):
    real_select = search.select_shortlist

    def tampered(pool, plan, **kw):
        out = real_select(pool, plan, **kw)
        first = out[0]
        out[0] = dataclasses.replace(first, place=dataclasses.replace(first.place, rating=5.0, review_count=1000))
        return out

    monkeypatch.setattr(search, "select_shortlist", tampered)
    await h.onboard()
    await h.send(ALEX, "Chef find mexican food near Tempe")
    assert "stars" not in h.last
    session = h.repo.active_session(h.repo.group_by_chat(CHAT)["id"])
    rows = h.repo.shortlist(session["id"])
    assert rows and all("5.0" not in r["place_json"] for r in rows)


class ConcurrencyProbe(FixturePlacesProvider):
    def __init__(self) -> None:
        super().__init__(FIXTURES / "osm_snapshot.json", latency_seconds=0.05)
        self.in_flight = self.peak = 0

    async def search(self, center: GeoPoint, radius_miles: float, cuisine_tags: set[str]) -> list[Place]:
        self.in_flight += 1
        self.peak = max(self.peak, self.in_flight)
        try:
            return await super().search(center, radius_miles, cuisine_tags)
        finally:
            self.in_flight -= 1


async def test_diet_alternative_search_runs_in_parallel_with_cuisine_search(tmp_path):
    probe = ConcurrencyProbe()
    h = Harness(tmp_path, places=probe)
    await h.onboard()
    await h.send(ALEX, "Chef find us dinner near Tempe")
    await h.send(SAM, "Halal for me.")
    probe.peak = 0
    await h.send(MIKE, "Burgers for me")
    assert probe.peak == 2
    assert "lists halal" in h.last
