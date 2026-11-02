"""Places providers: real OSM snapshot, live Overpass/Nominatim adapters (mocked HTTP), maps."""

import json
import re
from datetime import datetime
from urllib.parse import parse_qs, urlparse

import httpx
import pytest
import respx
from conftest import ALEX, CHAT, FIXTURES, MIKE, SAM, Harness

from app.domain.models import GeoPoint
from app.restaurants.base import (
    ProviderRateLimited,
    ProviderTimeout,
    ProviderUnavailable,
    place_from_osm_element,
)
from app.restaurants.hours import open_at
from app.restaurants.maps import haversine_miles, maps_directions_url, maps_search_url
from app.restaurants.mock import FailingPlacesProvider, FixtureGeocoder, FixturePlacesProvider
from app.restaurants.osm import NominatimGeocoder, OverpassPlacesProvider

TEMPE = GeoPoint(33.4255117, -111.9400160, "Tempe")
OVERPASS = "https://overpass.test/api/interpreter"
NOMINATIM = "https://nominatim.test/search"


def snapshot_ids() -> set[str]:
    data = json.loads((FIXTURES / "osm_snapshot.json").read_text())
    return {f"osm:{e['type']}/{e['id']}" for e in data["elements"]}


def test_haversine_known_distance():
    # Tempe city center to Mesa city center (both from Nominatim) is ~6.3 miles straight-line.
    assert 6.0 < haversine_miles(33.4255117, -111.9400160, 33.4151005, -111.8314550) < 6.6
    assert haversine_miles(1, 1, 1, 1) == 0


def test_maps_urls_are_https_and_encoded():
    url = maps_search_url(33.4255, -111.94)
    parsed = urlparse(url)
    assert parsed.scheme == "https" and parsed.netloc == "www.google.com"
    assert "%2C" in url and "," not in parsed.query
    assert parse_qs(parsed.query) == {"api": ["1"], "query": ["33.425500,-111.940000"]}
    assert maps_directions_url(33.4, -111.9).startswith("https://www.google.com/maps/dir/?api=1&destination=")
    with pytest.raises(ValueError):
        maps_search_url(100, 0)


def test_osm_element_normalization_keeps_unknowns_unknown():
    p = place_from_osm_element(
        {"type": "node", "id": 1, "lat": 33.4, "lon": -111.9,
         "tags": {"name": "Real Name", "cuisine": "burger;american", "diet:halal": "no", "diet:vegan": "yes"}},
        "2026-10-26",
    )
    assert p.cuisine_tags == ["burger", "american"]
    assert p.rating is None and p.review_count is None and p.price_level is None and p.opening_hours is None
    assert p.dietary_evidence["halal"].value == "no"
    assert p.source_url == "https://www.openstreetmap.org/node/1"
    assert place_from_osm_element({"type": "node", "id": 2, "lat": 1, "lon": 1, "tags": {}}, "t") is None
    assert place_from_osm_element({"type": "way", "id": 3, "tags": {"name": "No coords"}}, "t") is None


async def test_fixture_provider_is_real_snapshot_and_filters_radius():
    provider = FixturePlacesProvider(FIXTURES / "osm_snapshot.json")
    results = await provider.search(TEMPE, 1.0, {"burger"})
    assert results
    ids = snapshot_ids()
    for p in results:
        assert p.provider_place_id in ids
        assert haversine_miles(TEMPE.lat, TEMPE.lon, p.latitude, p.longitude) <= 1.0
        assert "burger" in p.cuisine_tags or p.amenity == "food_court"
        assert p.rating is None  # OSM has no ratings; never invented


async def test_fixture_geocoder_unknown_returns_none():
    g = FixtureGeocoder(FIXTURES / "geocode_snapshot.json")
    assert (await g.geocode("Tempe, AZ")).lat == pytest.approx(33.4255117)
    assert await g.geocode("Atlantis") is None


async def test_end_to_end_shortlist_contains_only_sourced_places(h):
    await h.onboard()
    await h.send(ALEX, "Chef find mexican food near Tempe")
    text = h.last
    assert text.count("Map: https://www.google.com/maps/search/?api=1&query=") <= 3
    session = h.repo.active_session(h.repo.group_by_chat(CHAT)["id"])
    shortlist = h.repo.shortlist(session["id"])
    assert 1 <= len(shortlist) <= 3
    ids = snapshot_ids()
    for row in shortlist:
        assert row["place_id"] in ids
    assert "stars" not in text and "$$" not in text
    assert "OpenStreetMap" in text


def test_opening_hours_parser():
    fri_7pm = datetime(2026, 10, 23, 19, 0)
    sat_1am = datetime(2026, 10, 24, 1, 0)
    assert open_at("Mo-Su 11:00-22:00", fri_7pm) is True
    assert open_at("Mo-Fr 06:00-14:00", fri_7pm) is False
    assert open_at("Fr 18:00-02:00", sat_1am) is True
    assert open_at("24/7", fri_7pm) is True
    assert open_at("Mo-Su 11:00-22:00; PH off", fri_7pm) is True
    assert open_at("sunrise-sunset", fri_7pm) is None
    assert open_at("Mo-Th 11:00-21:00", fri_7pm) is None  # Friday not listed -> unknown, not closed
    assert open_at(None, fri_7pm) is None
    assert open_at("garbage", fri_7pm) is None


def overpass_provider(client):
    return OverpassPlacesProvider(client, OVERPASS, "FoodieAI-test/0.1", timeout=2)


@respx.mock
async def test_overpass_query_parse_and_cache():
    body = {"osm3s": {"timestamp_osm_base": "2026-10-26T00:00:00Z"}, "elements": [
        {"type": "node", "id": 9, "lat": 33.42, "lon": -111.94, "tags": {"name": "A Real Place", "amenity": "restaurant", "cuisine": "burger"}},
        {"type": "way", "id": 10, "center": {"lat": 33.43, "lon": -111.93}, "tags": {"name": "Way Place", "amenity": "fast_food"}},
        {"type": "node", "id": 11, "lat": 33.0, "lon": -111.0, "tags": {"amenity": "restaurant"}},
    ]}
    route = respx.post(OVERPASS).mock(return_value=httpx.Response(200, json=body))
    async with httpx.AsyncClient() as client:
        prov = overpass_provider(client)
        places = await prov.search(TEMPE, 5, {"burger"})
        again = await prov.search(TEMPE, 5, {"burger"})
    assert [p.name for p in places] == ["A Real Place", "Way Place"]
    assert len(again) == 2 and route.call_count == 1 and prov.cache.hits == 1
    sent = route.calls[0].request
    assert sent.headers["user-agent"] == "FoodieAI-test/0.1"
    query = parse_qs(sent.content.decode())["data"][0]
    assert "around:8046" in query and re.search(r"cuisine.*burger", query)


@pytest.mark.parametrize("status,exc", [(429, ProviderRateLimited), (504, ProviderUnavailable), (502, ProviderUnavailable)])
@respx.mock
async def test_overpass_errors(status, exc):
    respx.post(OVERPASS).mock(return_value=httpx.Response(status, text="busy"))
    async with httpx.AsyncClient() as client:
        with pytest.raises(exc):
            await overpass_provider(client).search(TEMPE, 5, set())


@respx.mock
async def test_overpass_timeout_and_bad_json():
    respx.post(OVERPASS).mock(side_effect=httpx.ReadTimeout("slow"))
    async with httpx.AsyncClient() as client:
        with pytest.raises(ProviderTimeout):
            await overpass_provider(client).search(TEMPE, 5, set())
    respx.post(OVERPASS).mock(return_value=httpx.Response(200, text="<html>"))
    async with httpx.AsyncClient() as client:
        with pytest.raises(ProviderUnavailable):
            await overpass_provider(client).search(TEMPE, 5, set())


@respx.mock
async def test_overpass_empty_set():
    respx.post(OVERPASS).mock(return_value=httpx.Response(200, json={"elements": []}))
    async with httpx.AsyncClient() as client:
        assert await overpass_provider(client).search(TEMPE, 5, set()) == []


@respx.mock
async def test_nominatim_geocoder():
    route = respx.get(NOMINATIM).mock(return_value=httpx.Response(200, json=[{"lat": "33.4", "lon": "-111.9", "display_name": "Tempe, AZ"}]))
    async with httpx.AsyncClient() as client:
        g = NominatimGeocoder(client, NOMINATIM, "FoodieAI-test/0.1")
        assert (await g.geocode("Tempe")).lat == 33.4
        assert (await g.geocode("tempe")).lat == 33.4
    assert route.call_count == 1
    assert route.calls[0].request.url.params["format"] == "jsonv2"


async def test_provider_outage_is_reported_not_retried(tmp_path):
    failing = FailingPlacesProvider(ProviderRateLimited("429"))
    h = Harness(tmp_path, places=failing)
    await h.onboard()
    await h.send(ALEX, "Chef find tacos near Tempe")
    assert "rate limited" in h.last
    assert failing.calls == 1
    s = h.repo.active_session(h.repo.group_by_chat(CHAT)["id"])
    assert s["status"] == "collecting"
    await h.send(ALEX, "Chef go")
    assert failing.calls == 2


async def test_no_results_never_relaxes_requirements(h):
    await h.onboard()
    await h.send(ALEX, "Chef find kosher bbq near Tempe within 1 mile")
    assert "won't relax" in h.last or "kosher not verified" in h.last or "any option: kosher" in h.last
    assert "1 mi" in h.last or "1 miles" in h.last


async def test_shortlist_is_compact_and_never_shows_zero_miles(h):
    await h.onboard()
    await h.send(ALEX, "Chef find us dinner near Tempe, under $25.")
    await h.send(SAM, "Halal for me.")
    await h.send(MIKE, "No seafood.")
    text = h.last
    assert "0.0 mi" not in text and "<0.1 mi" in text
    places = len(re.findall(r"^\d\. ", text, re.M))
    # header + 2 lines per place + at most 3 footer lines
    assert len(text.splitlines()) <= 1 + 2 * places + 3
    assert text.count("Distances are straight-line") == 1
    assert "lists halal" in text


async def test_verified_diet_alternative_is_offered_when_cuisine_hides_it(h):
    await h.onboard()
    await h.send(ALEX, "Chef find us dinner near Tempe")
    await h.send(SAM, "Halal for me.")
    await h.send(MIKE, "Burgers for me")
    text = h.last
    assert "4. The Halal Guys" in text and "not burger" in text
    assert "Option 4 lists halal but isn't the cuisine you asked for." in text
    session = h.repo.active_session(h.repo.group_by_chat(CHAT)["id"])
    assert [r["option_number"] for r in h.repo.shortlist(session["id"])] == [1, 2, 3, 4]
    await h.send(SAM, "Chef vote 4")
    assert "option 4" in h.last


async def test_blocked_cuisine_asks_for_another_choice(h):
    await h.onboard()
    await h.send(ALEX, "Chef find dinner near Tempe")
    await h.send(MIKE, "No seafood.")
    await h.send(ALEX, "sushi please")
    await h.send(SAM, "Burgers for me")
    assert "Sushi is out because someone can't have seafood" in h.last
    assert "name another cuisine" in h.last
