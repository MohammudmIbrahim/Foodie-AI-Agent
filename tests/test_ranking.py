"""Ranking and hard-filter rules.

Places in this file are explicitly synthetic *unit-test inputs* for the algorithm (names start
with "Test"); they are never shown to users. User-facing data comes from the real OSM snapshot.
"""

from datetime import datetime

from app.domain.conflict import blocked_cuisines, build_plan
from app.domain.models import Constraint, ConstraintKind, Evidence, GeoPoint, Place
from app.domain.ranking import bayesian_rating, budget_to_level, dedupe, rank, select_shortlist
from app.restaurants.maps import maps_search_url

CENTER = GeoPoint(33.4255, -111.9400, "Tempe")
ARRIVAL = datetime(2026, 10, 23, 18, 50)  # Friday


def place(pid, name, dlat=0.0, **kw):
    lat, lon = CENTER.lat + dlat, CENTER.lon
    return Place(provider="test", provider_place_id=pid, name=name, latitude=lat, longitude=lon,
                 maps_url=maps_search_url(lat, lon), last_fetched_at="t", **kw)


def plan(*cons, location=CENTER, radius=5.0):
    return build_plan(list(cons), location=location, default_radius=radius, max_radius=15)


def C(kind, value, hard=False, member="m1"):
    return Constraint(ConstraintKind(kind), value, hard, member)


def test_hard_dietary_requirement_beats_high_rating():
    star = place("a", "Test Star", rating=4.9, review_count=3000)
    verified = place("b", "Test Verified", dlat=0.03, rating=3.1, review_count=12,
                     dietary_evidence={"halal": Evidence.YES})
    pool, _ = rank([star, verified], plan(C("dietary", "halal", True)), ARRIVAL)
    assert [r.place.name for r in pool] == ["Test Verified", "Test Star"]
    assert pool[1].unverified == ["halal"]


def test_negative_dietary_evidence_excludes():
    no = place("a", "Test No", dietary_evidence={"vegetarian": Evidence.NO})
    pool, excluded = rank([no], plan(C("dietary", "vegetarian", True)), ARRIVAL)
    assert pool == [] and excluded == {"dietary": 1}


def test_vegan_listing_counts_as_vegetarian_evidence():
    p = place("a", "Test Vegan", dietary_evidence={"vegan": Evidence.YES})
    pool, _ = rank([p], plan(C("dietary", "vegetarian", True)), ARRIVAL)
    assert pool[0].unverified == []


def test_strict_radius_cannot_be_exceeded():
    near = place("a", "Test Near", dlat=0.01)
    far = place("b", "Test Far", dlat=0.1)  # ~6.9 mi
    pool, excluded = rank([near, far], plan(C("radius", "5", True)), ARRIVAL)
    assert [r.place.name for r in pool] == ["Test Near"] and excluded["radius"] == 1


def test_hard_budget_excludes_known_over_and_flags_unknown():
    cheap = place("a", "Test Cheap", price_level=1)
    pricey = place("b", "Test Pricey", price_level=3)
    unknown = place("c", "Test Unknown")
    pool, excluded = rank([cheap, pricey, unknown], plan(C("budget", "15", True)), ARRIVAL)
    names = [r.place.name for r in pool]
    assert names == ["Test Cheap", "Test Unknown"]
    assert pool[1].unverified == ["budget"]
    assert excluded["budget"] == 1


def test_closed_at_arrival_excluded_unknown_hours_kept():
    closed = place("a", "Test Closed", opening_hours="Mo-Su 06:00-14:00")
    open_ = place("b", "Test Open", opening_hours="Mo-Su 11:00-22:00")
    unknown = place("c", "Test Unknown")
    pool, excluded = rank([closed, open_, unknown], plan(), ARRIVAL)
    assert excluded == {"closed": 1}
    by = {r.place.name: r for r in pool}
    assert by["Test Open"].open_at_arrival is True
    assert by["Test Unknown"].open_at_arrival is None
    assert by["Test Unknown"].hours_note == "hours not confirmed"


def test_avoid_excludes_conservatively():
    sushi = place("a", "Test Sushi", cuisine_tags=["sushi"])
    tacos = place("b", "Test Tacos", cuisine_tags=["mexican"])
    pool, _ = rank([sushi, tacos], plan(C("avoid", "seafood", True)), ARRIVAL)
    assert [r.place.name for r in pool] == ["Test Tacos"]


def test_missing_reviews_are_omitted_not_zero():
    no_reviews = place("a", "Test NoReviews")
    pool, _ = rank([no_reviews], plan(), ARRIVAL)
    assert pool[0].score > 0.4 and pool[0].confidence < 1


def test_bayesian_reviews_prefer_confidence():
    assert bayesian_rating(5.0, 2) < bayesian_rating(4.6, 2000)


def test_fairness_uses_least_satisfied_member():
    p = plan(C("cuisine", "burger", member="m1"), C("cuisine", "sushi", member="m2"))
    assert p.conflict == "cuisine"
    burger = place("a", "Test Burger", cuisine_tags=["burger"])
    both = place("b", "Test Food Hall", dlat=0.02, cuisine_tags=["burger", "sushi"])
    sushi = place("c", "Test Sushi", cuisine_tags=["sushi"])
    pool, _ = rank([burger, both, sushi], p, ARRIVAL)
    assert pool[0].place.name == "Test Food Hall"
    short = select_shortlist(pool, p)
    assert {r.place.name for r in short} == {"Test Food Hall", "Test Burger", "Test Sushi"}


def test_conflict_shortlist_represents_each_craving():
    p = plan(C("cuisine", "burger", member="m1"), C("cuisine", "sushi", member="m2"))
    burgers = [place(f"b{i}", f"Test Burger {i}", dlat=0.001 * i, cuisine_tags=["burger"]) for i in range(5)]
    sushi = place("s", "Test Sushi", dlat=0.05, cuisine_tags=["sushi"])
    pool, _ = rank([*burgers, sushi], p, ARRIVAL)
    short = select_shortlist(pool, p)
    assert "Test Sushi" in {r.place.name for r in short}
    assert len(short) == 3


def test_blocked_cuisine_detected():
    p = plan(C("cuisine", "sushi", member="m1"), C("avoid", "seafood", True, member="m2"))
    assert blocked_cuisines(p) == [("sushi", "seafood")]


def test_dedupe_by_id_and_name_location():
    a = place("x", "Test Same")
    b = place("x", "Test Same")
    c = place("y", "Test  same", dlat=0.0001)
    assert len(dedupe([a, b, c])) == 1


def test_budget_levels():
    assert [budget_to_level(x) for x in (10, 25, 50, 100)] == [1, 2, 3, 4]


def test_ranking_is_deterministic():
    ps = [place(str(i), f"Test {i}", dlat=0.001 * (i % 3)) for i in range(10)]
    a, _ = rank(ps, plan(), ARRIVAL)
    b, _ = rank(list(reversed(ps)), plan(), ARRIVAL)
    assert [r.place.provider_place_id for r in a] == [r.place.provider_place_id for r in b]
