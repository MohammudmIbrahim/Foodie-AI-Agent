"""The restaurant search workflow: locate -> plan -> gather (parallel) -> rank -> shortlist -> reply (reviewed).

Composition (custom logic pattern built from deterministic workflow agents):

    Sequential("restaurant_search")
      locate                      geocode the area or use the group default
      plan                        merge everyone's requirements into one SearchPlan
      Parallel("gather")
        cuisine_search            places matching the requested cuisines
        diet_alternative_search   (optional) any cuisine, to find a place that verifies a diet need
      rank                        hard filters, then ranking; widen the radius once if thin
      shortlist                   pick options, add the verified-diet alternative when needed
      ReviewLoop("reply")
        draft_reply               generator: render the shortlist message
        check_reply               critic: every option and field must trace back to provider data

Session versions are re-checked when results are stored and again in the outbox, so a stale run is
never sent even if it finishes.
"""

from __future__ import annotations

import asyncio
import dataclasses
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Any

from app.domain import replies
from app.domain.conflict import blocked_cuisines, build_plan
from app.domain.models import GeoPoint, Place, RankedPlace, SearchPlan
from app.domain.parsing import CUISINE_TAGS
from app.domain.ranking import rank, select_shortlist
from app.restaurants.base import ProviderError, ProviderRateLimited, ProviderTimeout
from app.workflow.base import Halt, Parallel, Review, ReviewLoop, Sequential, Step

if TYPE_CHECKING:
    from app.services.orchestration import Orchestrator

ARRIVAL_LEAD_MINUTES = 20
MAX_OPTIONS = 4


@dataclass
class SearchState:
    o: Orchestrator
    group: Any
    session: Any
    version: int
    updated: bool = False  # a shortlist was already sent in this session
    votes_cleared: bool = False
    name: str = ""
    point: GeoPoint | None = None
    plan: SearchPlan | None = None
    arrival: datetime | None = None
    tags: set[str] = field(default_factory=set)
    places: list[Place] = field(default_factory=list)  # every place any search returned
    cuisine_places: list[Place] = field(default_factory=list)
    alt_places: list[Place] = field(default_factory=list)
    pool: list[RankedPlace] = field(default_factory=list)
    excluded: dict[str, int] = field(default_factory=dict)
    shortlist: list[RankedPlace] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    text: str = ""
    review: Review = field(default_factory=Review)


def kind(e: BaseException) -> str:
    if isinstance(e, (ProviderTimeout, asyncio.TimeoutError)):
        return "timed out"
    if isinstance(e, ProviderRateLimited):
        return "rate limited"
    return "service unavailable"


async def search_places(o: Orchestrator, point: GeoPoint, radius: float, tags: set[str]) -> list[Place]:
    start = time.perf_counter()
    try:
        return await asyncio.wait_for(o.places.search(point, radius, tags), timeout=o.s.places_timeout_seconds + 5)
    finally:
        o.metrics.observe("search", time.perf_counter() - start)


def _unavailable(st: SearchState, e: BaseException) -> Halt:
    return Halt(f"Restaurant lookup is unavailable right now ({kind(e)}). Say \"{st.name} go\" to try again.", "provider")


# ---- steps ----------------------------------------------------------------------------------

async def locate(st: SearchState) -> None:
    o, session, group = st.o, st.session, st.group
    st.name = o.name(group)
    try:
        if session["lat"] is not None:
            st.point = GeoPoint(session["lat"], session["lon"], session["location_label"] or "shared location")
        elif session["location_label"]:
            with o.metrics.timed("geocode"):
                st.point = await o.geocoder.geocode(session["location_label"])
            if not st.point:
                o.repo.set_session_location(session["id"], None, None, None)
                raise Halt(f"I couldn't find \"{session['location_label'][:40]}\". Try a city name or a Maps link.", "geocode_miss")
            o.repo.set_session_coords(session["id"], st.version, st.point.lat, st.point.lon, session["location_label"])
        elif group["default_lat"] is not None:
            st.point = GeoPoint(group["default_lat"], group["default_lon"], group["default_city"])
    except ProviderError as e:
        raise Halt(f"The map lookup is unavailable right now ({kind(e)}). Say \"{st.name} go\" to retry.", "geocode") from e
    if st.point is None:
        raise Halt("What area should I search?", "no_area")


async def make_plan(st: SearchState) -> None:
    o = st.o
    cons = [s.constraint for s in o.repo.constraints(st.session["id"])]
    st.plan = build_plan(
        cons, location=st.point,
        default_radius=st.group["default_radius_miles"] or o.s.default_radius_miles,
        max_radius=o.s.max_radius_miles,
    )
    st.arrival = st.plan.arrival or (o.local_now() + timedelta(minutes=ARRIVAL_LEAD_MINUTES))
    for c in st.plan.all_cuisines:
        st.tags |= CUISINE_TAGS.get(c, frozenset({c}))


async def cuisine_search(st: SearchState) -> None:
    try:
        st.cuisine_places = await search_places(st.o, st.point, st.plan.radius_miles, st.tags)
    except (ProviderError, asyncio.TimeoutError) as e:
        raise _unavailable(st, e) from e


async def diet_alternative_search(st: SearchState) -> None:
    """Speculative: only when a diet need and a cuisine filter could hide a place that verifies the need.

    Runs alongside the cuisine search, so a slow public map server costs one wait instead of two.
    """
    if st.plan.dietary and st.plan.all_cuisines:
        st.alt_places = await search_places(st.o, st.point, st.plan.radius_miles, set())


async def rank_step(st: SearchState) -> None:
    o, plan = st.o, st.plan
    st.places = st.cuisine_places + st.alt_places
    with o.metrics.timed("ranking"):
        st.pool, st.excluded = rank(st.cuisine_places, plan, st.arrival)
    # Broaden once, only when results are insufficient and the radius isn't strict.
    if len(st.pool) < 3 and not plan.radius_strict and plan.radius_miles < o.s.max_radius_miles:
        wider = min(o.s.max_radius_miles, plan.radius_miles * 2)
        try:
            more = await search_places(o, st.point, wider, st.tags)
        except (ProviderError, asyncio.TimeoutError) as e:
            raise _unavailable(st, e) from e
        st.places += more
        wplan = dataclasses.replace(plan, radius_miles=wider)
        wpool, wexcluded = rank(st.cuisine_places + more, wplan, st.arrival)
        if len(wpool) > len(st.pool):
            st.notes.append(f"I widened the search to {wider:g} miles to find more options.")
            st.plan, st.pool, st.excluded = wplan, wpool, wexcluded


async def shortlist_step(st: SearchState) -> None:
    plan = st.plan
    for cuisine, avoid in blocked_cuisines(plan):
        st.notes.append(f"{cuisine.title()} is out because someone can't have {avoid}. Whoever wanted {cuisine}, name another cuisine and I'll redo the list.")
    st.shortlist = select_shortlist(st.pool, plan)
    if not st.shortlist:
        raise Halt(replies.no_results(plan, st.excluded, st.name), "no_results")
    if plan.budget_hard is None and plan.budget_soft is not None and all(r.place.price_level is None for r in st.shortlist):
        st.notes.append(f"Prices aren't listed, so I can't confirm the ${plan.budget_soft:g} budget.")
    alt = _diet_alternative(st)
    if alt:
        st.shortlist.append(alt)
        diets = ", ".join(sorted(d.replace("_", "-") for d in plan.dietary))
        st.notes.append(f"Option {len(st.shortlist)} lists {diets} but isn't the cuisine you asked for.")


def _diet_alternative(st: SearchState) -> RankedPlace | None:
    """When every cuisine match leaves a diet need unverified, offer the best nearby place that verifies it."""
    plan = st.plan
    diets = {d.replace("_", "-") for d in plan.dietary}
    if not st.alt_places or not all(diets & set(r.unverified) for r in st.shortlist):
        return None
    anything = dataclasses.replace(plan, cuisines_by_member={m: set() for m in plan.cuisines_by_member}, conflict="none")
    pool, _ = rank(st.alt_places, anything, st.arrival)
    shown = {r.place.provider_place_id for r in st.shortlist}
    return next((r for r in pool if not diets & set(r.unverified) and r.place.provider_place_id not in shown), None)


async def draft_reply(st: SearchState) -> None:
    """Generator. On a revision round it drops the options the critic rejected."""
    if st.review.drop:
        st.shortlist = [r for r in st.shortlist if r.place.provider_place_id not in st.review.drop]
        if not st.shortlist:
            raise Halt(replies.no_results(st.plan, st.excluded, st.name), "no_results")
    notes = list(st.notes)
    if st.votes_cleared:
        notes.insert(0, "Requirements changed, so earlier votes were cleared. Please vote again.")
    st.text = summary(st.plan, st.point, st.updated) + "\n" + replies.shortlist(st.name, st.shortlist, st.plan, st.o.places.attribution, notes)


async def check_reply(st: SearchState) -> None:
    """Critic. Deterministic checks of the non-negotiables before anything reaches the group."""
    review = Review()
    sources = {p.provider_place_id: p for p in st.places}
    for r in st.shortlist:
        src = sources.get(r.place.provider_place_id)
        if src is None or src.to_dict() != r.place.to_dict():
            review.drop.add(r.place.provider_place_id)  # not from the provider, or a field was changed
    if len(st.shortlist) > MAX_OPTIONS:
        review.blocking.append("too many options")
    if any(r.unverified for r in st.shortlist) and "not verified" not in st.text.lower():
        review.blocking.append("unverified requirement not disclosed")
    if st.plan.allergies and replies.ALLERGY_CAVEAT not in st.text:
        review.blocking.append("allergy caveat missing")
    st.review = review


def summary(plan: SearchPlan, point: GeoPoint, updated: bool) -> str:
    bits = []
    if plan.all_cuisines:
        bits.append(" / ".join(sorted(plan.all_cuisines)))
    for d in sorted(plan.dietary):
        bits.append(f"{d.replace('_', '-')} required")
    if plan.allergies:
        bits.append("allergy: " + ", ".join(sorted(a.replace("_", " ") for a in plan.allergies)))
    if plan.avoid:
        bits.append("avoiding " + ", ".join(sorted(plan.avoid)))
    if plan.budget_hard is not None:
        bits.append(f"${plan.budget_hard:g} max")
    elif plan.budget_soft is not None:
        bits.append(f"under ${plan.budget_soft:g}")
    where = point.label.split(",")[0]
    lead = "Updated" if updated else "Got it"
    return f"{lead}: {', '.join(bits) + ', ' if bits else ''}within {plan.radius_miles:g} mi of {where}."


SEARCH_WORKFLOW = Sequential("restaurant_search", [
    Step("locate", locate),
    Step("plan", make_plan),
    Parallel("gather", [
        Step("cuisine_search", cuisine_search),
        Step("diet_alternative_search", diet_alternative_search, optional=True),
    ]),
    Step("rank", rank_step),
    Step("shortlist", shortlist_step),
    ReviewLoop("reply", generator=Step("draft_reply", draft_reply), critic=Step("check_reply", check_reply)),
])
