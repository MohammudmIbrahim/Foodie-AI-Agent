"""Deterministic hard filtering and fairness-aware ranking.

Order: mandatory restrictions -> hours at arrival -> strict radius -> confirmed budget ceiling
-> rank. Missing data is never a pass: unverifiable hard requirements are flagged and every
flagged candidate ranks below every candidate with verified evidence, so a high rating can never
outweigh a hard requirement.
"""

from __future__ import annotations

from datetime import datetime

from app.domain.models import Evidence, Place, RankedPlace, SearchPlan
from app.domain.parsing import AVOID_TAGS, CUISINE_TAGS
from app.restaurants.hours import open_at
from app.restaurants.maps import haversine_miles

WEIGHTS = {"group": 0.30, "reviews": 0.20, "distance": 0.20, "price": 0.15, "hours": 0.15}
REVIEW_PRIOR_MEAN = 3.8
REVIEW_PRIOR_COUNT = 50
HOURS_UNKNOWN = 0.5  # neutral, disclosed as "hours not confirmed"; never treated as open


def budget_to_level(amount: float) -> int:
    """Per-person dollars -> 1..4 price level ($ <=15, $$ <=30, $$$ <=60, $$$$ above)."""
    if amount <= 15:
        return 1
    if amount <= 30:
        return 2
    if amount <= 60:
        return 3
    return 4


def bayesian_rating(rating: float, count: int) -> float:
    return (count * rating + REVIEW_PRIOR_COUNT * REVIEW_PRIOR_MEAN) / (count + REVIEW_PRIOR_COUNT)


def _norm(s: str) -> str:
    return "".join(ch for ch in s.lower() if ch.isalnum())


def dedupe(places: list[Place]) -> list[Place]:
    """Drop repeated provider IDs and same-name places within ~80 m (node + building duplicates)."""
    seen_ids: set[str] = set()
    by_name: dict[str, list[Place]] = {}
    out: list[Place] = []
    for p in places:
        if p.provider_place_id in seen_ids:
            continue
        same_name = by_name.setdefault(_norm(p.name), [])
        if any(haversine_miles(p.latitude, p.longitude, q.latitude, q.longitude) < 0.05 for q in same_name):
            continue
        seen_ids.add(p.provider_place_id)
        same_name.append(p)
        out.append(p)
    return out


def matched_cuisines(place: Place, cuisines: set[str]) -> set[str]:
    tags = set(place.cuisine_tags)
    return {c for c in cuisines if tags & CUISINE_TAGS.get(c, frozenset({c}))}


def _diet_evidence(place: Place, diet: str) -> Evidence:
    ev = place.dietary_evidence.get(diet, Evidence.UNKNOWN)
    if diet == "vegetarian" and ev == Evidence.UNKNOWN and place.dietary_evidence.get("vegan") == Evidence.YES:
        # diet:vegan=yes means vegan options exist, which are vegetarian options.
        return Evidence.YES
    return ev


def rank(places: list[Place], plan: SearchPlan, arrival: datetime) -> tuple[list[RankedPlace], dict[str, int]]:
    """Return the eligible, ranked pool and exclusion counts by reason (for honest replies)."""
    assert plan.location is not None
    excluded: dict[str, int] = {}
    pool: list[RankedPlace] = []
    requested = plan.all_cuisines
    hard_level = budget_to_level(plan.budget_hard) if plan.budget_hard is not None else None
    budgets = [b for b in (plan.budget_hard, plan.budget_soft) if b is not None]
    comfort_level = budget_to_level(min(budgets)) if budgets else None

    def drop(reason: str) -> None:
        excluded[reason] = excluded.get(reason, 0) + 1

    for p in dedupe(places):
        tags = set(p.cuisine_tags)
        if any(tags & AVOID_TAGS.get(a, frozenset({a})) for a in plan.avoid):
            drop("avoid")
            continue
        unverified: list[str] = []
        reasons: list[str] = []
        diet_fail = False
        for diet in sorted(plan.dietary):
            ev = _diet_evidence(p, diet)
            if ev == Evidence.NO:
                diet_fail = True
            elif ev == Evidence.UNKNOWN:
                unverified.append(diet.replace("_", "-"))
            else:
                reasons.append(f"{diet.replace('_', '-')} options listed in OSM")
        if diet_fail:
            drop("dietary")
            continue

        is_open = open_at(p.opening_hours, arrival)
        if is_open is False:
            drop("closed")
            continue

        dist = haversine_miles(plan.location.lat, plan.location.lon, p.latitude, p.longitude)
        if dist > plan.radius_miles:
            drop("radius")
            continue

        if hard_level is not None:
            if p.price_level is None:
                unverified.append("budget")
            elif p.price_level > hard_level:
                drop("budget")
                continue

        matched = matched_cuisines(p, requested)
        food_court = p.amenity == "food_court"
        if requested and not matched and not food_court:
            drop("cuisine")
            continue

        components: dict[str, float] = {}
        if plan.cuisines_by_member:
            sats = []
            for prefs in plan.cuisines_by_member.values():
                if not prefs or matched & prefs:
                    sats.append(1.0)
                elif food_court:
                    sats.append(0.6)
                else:
                    sats.append(0.15)
            components["group"] = 0.65 * (sum(sats) / len(sats)) + 0.35 * min(sats)
        if p.rating is not None and p.review_count:
            components["reviews"] = max(0.0, (bayesian_rating(p.rating, p.review_count) - 1) / 4)
        components["distance"] = max(0.0, 1 - dist / plan.radius_miles) if plan.radius_miles else 0.0
        if p.price_level is not None and comfort_level is not None:
            components["price"] = 1.0 if p.price_level <= comfort_level else max(0.0, 1 - 0.5 * (p.price_level - comfort_level))
        components["hours"] = 1.0 if is_open else HOURS_UNKNOWN

        known = sum(WEIGHTS[k] for k in components)
        score = sum(WEIGHTS[k] * v for k, v in components.items()) / known

        if matched:
            reasons.insert(0, "matches " + ", ".join(sorted(matched)))
        elif food_court:
            reasons.insert(0, "food court, may suit mixed cravings (menu not verified)")
        hours_note = (
            f"open at {arrival.strftime('%-I:%M %p')} per OSM hours" if is_open else "hours not confirmed"
        )
        pool.append(
            RankedPlace(
                place=p, score=round(score, 4), distance_miles=dist, confidence=round(known, 2),
                unverified=unverified, reasons=reasons, hours_note=hours_note,
                open_at_arrival=is_open, matched_cuisines=sorted(matched),
            )
        )

    pool.sort(key=lambda r: (len(r.unverified), -r.score, r.distance_miles, r.place.name, r.place.provider_place_id))
    return pool, excluded


def select_shortlist(pool: list[RankedPlace], plan: SearchPlan, k: int = 3, exclude_ids: frozenset[str] = frozenset()) -> list[RankedPlace]:
    """Top-k, but for a cuisine conflict prefer a real compromise, then one per member's craving."""
    candidates = [r for r in pool if r.place.provider_place_id not in exclude_ids]
    if plan.conflict != "cuisine":
        return candidates[:k]
    member_sets = [s for s in plan.cuisines_by_member.values() if s]
    chosen: list[RankedPlace] = []

    def satisfies(r: RankedPlace, prefs: set[str]) -> bool:
        return bool(set(r.matched_cuisines) & prefs)

    for r in candidates:
        if sum(satisfies(r, s) for s in member_sets) >= 2:
            chosen.append(r)
            break
    for prefs in member_sets:
        if len(chosen) >= k:
            break
        if any(satisfies(c, prefs) for c in chosen):
            continue
        best = next((r for r in candidates if r not in chosen and satisfies(r, prefs)), None)
        if best:
            chosen.append(best)
    for r in candidates:
        if len(chosen) >= k:
            break
        if r not in chosen:
            chosen.append(r)
    return sorted(chosen, key=candidates.index)
