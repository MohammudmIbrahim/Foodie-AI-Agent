"""Area cache (one fetch per area, local filtering) and the Geoapify source (mocked HTTP)."""

import asyncio
from urllib.parse import parse_qs

import httpx
import pytest
import respx
from conftest import FIXTURES

from app.config import ConfigError, Settings
from app.domain.models import GeoPoint
from app.restaurants.area_cache import AreaCachedPlaces, FallbackAreaSource
from app.restaurants.base import ProviderUnavailable
from app.restaurants.geoapify import GeoapifyPlacesProvider, place_from_geoapify_feature
from app.restaurants.mock import FixturePlacesProvider
from app.restaurants.osm import OverpassPlacesProvider

TEMPE = GeoPoint(33.4255117, -111.9400160, "Tempe")
MESA = GeoPoint(33.4151005, -111.8314550, "Mesa")
GEOAPIFY = "https://geoapify.test/v2/places"
OVERPASS = "https://overpass.test/api/interpreter"


class SnapshotSource:
    """Area source over the real OSM snapshot that counts fetches."""

    name = "snapshot"
    attribution = "test"

    def __init__(self, covered: float | None = None, delay: float = 0) -> None:
        self.inner = FixturePlacesProvider(FIXTURES / "osm_snapshot.json")
        self.covered, self.delay, self.calls = covered, delay, 0

    async def fetch_area(self, center, radius_miles):
        self.calls += 1
        await asyncio.sleep(self.delay)
        places = await self.inner.search(center, radius_miles, set())
        return places, radius_miles if self.covered is None else self.covered


async def test_one_fetch_answers_other_cuisines_and_smaller_radius():
    src = SnapshotSource()
    cache = AreaCachedPlaces(src, min_area_miles=5, ttl_seconds=3600)
    burgers = await cache.search(TEMPE, 5, {"burger"})
    chicken = await cache.search(TEMPE, 3, {"chicken"})
    everything = await cache.search(TEMPE, 5, set())
    assert src.calls == 1 and cache.hits == 2
    assert burgers and chicken and len(everything) > len(burgers)
    # Same answer the uncached provider gives.
    direct = await src.inner.search(TEMPE, 3, {"chicken"})
    assert {p.provider_place_id for p in chicken} == {p.provider_place_id for p in direct}


async def test_parallel_searches_share_one_fetch():
    src = SnapshotSource(delay=0.05)
    cache = AreaCachedPlaces(src, min_area_miles=5, ttl_seconds=3600)
    await asyncio.gather(cache.search(TEMPE, 5, {"burger"}), cache.search(TEMPE, 5, {"halal"}))
    assert src.calls == 1


async def test_new_area_wider_radius_and_expiry_refetch():
    now = [0.0]
    src = SnapshotSource()
    cache = AreaCachedPlaces(src, min_area_miles=5, ttl_seconds=60, clock=lambda: now[0])
    await cache.search(TEMPE, 5, set())
    await cache.search(MESA, 5, set())  # 6 mi away: not inside the Tempe area
    assert src.calls == 2
    await cache.search(TEMPE, 10, set())  # wider than what was fetched
    assert src.calls == 3
    await cache.search(TEMPE, 2, set())
    assert src.calls == 3
    now[0] = 61
    await cache.search(TEMPE, 2, set())
    assert src.calls == 4


async def test_uncovered_fetch_is_used_once_but_not_saved():
    src = SnapshotSource(covered=0.0)  # e.g. Overpass hit its result cap
    cache = AreaCachedPlaces(src, min_area_miles=5, ttl_seconds=3600)
    assert await cache.search(TEMPE, 5, {"burger"})
    await cache.search(TEMPE, 5, {"burger"})
    assert src.calls == 2


def feature(name, lat, lon, raw=None, **props):
    p = {"name": name, "lat": lat, "lon": lon, "categories": ["catering.fast_food.burger", "catering.fast_food"], **props}
    if raw is not None:
        p["datasource"] = {"sourcename": "openstreetmap", "raw": raw}
    return {"type": "Feature", "properties": p, "geometry": {"type": "Point", "coordinates": [lon, lat]}}


def test_feature_with_osm_tags_matches_osm_normalization():
    p = place_from_geoapify_feature(feature("Five Guys", 33.4239, -111.9401, raw={
        "osm_id": 2391078492, "osm_type": "n", "name": "Five Guys", "amenity": "fast_food",
        "cuisine": "burger", "opening_hours": "Mo-Su 11:00-22:00", "diet:halal": "no",
    }), "2026-11-03")
    assert p.provider_place_id == "osm:node/2391078492"
    assert p.source_url == "https://www.openstreetmap.org/node/2391078492"
    assert p.cuisine_tags == ["burger"] and p.amenity == "fast_food"
    assert p.opening_hours == "Mo-Su 11:00-22:00" and p.dietary_evidence["halal"].value == "no"
    assert p.rating is None and p.price_level is None


def test_feature_without_osm_tags_uses_geoapify_fields_and_no_fake_osm_link():
    p = place_from_geoapify_feature(feature(
        "Halal Spot", 33.41, -111.92, place_id="abc123", housenumber="10", street="Mill Ave", city="Tempe",
        catering={"cuisine": "middle_eastern", "diet": {"halal": True}},
    ), "2026-11-03")
    assert p.provider_place_id == "geoapify:abc123" and p.source_url is None
    assert p.cuisine_tags == ["middle_eastern"] and p.dietary_evidence["halal"].value == "yes"
    assert p.address == "10 Mill Ave, Tempe" and p.amenity == "fast_food"
    assert place_from_geoapify_feature(feature("", 33.4, -111.9), "x") is None


@respx.mock
async def test_geoapify_request_and_paging_cap_sets_covered_radius():
    near = [feature(f"Near {i}", 33.4255 + i * 0.0001, -111.94, raw={"osm_id": i + 1, "osm_type": "n"}) for i in range(500)]
    far = [feature(f"Far {i}", 33.4255 + 0.01 + i * 0.0001, -111.94, raw={"osm_id": 1000 + i, "osm_type": "w"}) for i in range(500)]
    route = respx.get(GEOAPIFY).mock(side_effect=[
        httpx.Response(200, json={"features": near}), httpx.Response(200, json={"features": far}),
    ])
    async with httpx.AsyncClient() as client:
        geo = GeoapifyPlacesProvider(client, GEOAPIFY, "test-key", max_places=1000)
        places, covered = await geo.fetch_area(TEMPE, 5)
    assert len(places) == 1000 and route.call_count == 2
    q = parse_qs(str(route.calls[0].request.url.query, "utf-8"))
    assert q["apiKey"] == ["test-key"] and q["limit"] == ["500"] and q["offset"] == ["0"]
    assert q["filter"] == ["circle:-111.940016,33.425512,8046"] and q["bias"] == ["proximity:-111.940016,33.425512"]
    assert q["categories"] == ["catering.restaurant,catering.fast_food,catering.food_court"]
    assert parse_qs(str(route.calls[1].request.url.query, "utf-8"))["offset"] == ["500"]
    assert 0.6 < covered < 5  # capped: only complete out to the farthest place returned


@respx.mock
async def test_geoapify_small_area_covers_full_radius():
    respx.get(GEOAPIFY).mock(return_value=httpx.Response(200, json={"features": [feature("Only", 33.42, -111.94, raw={"osm_id": 5, "osm_type": "n"})]}))
    async with httpx.AsyncClient() as client:
        places, covered = await GeoapifyPlacesProvider(client, GEOAPIFY, "k").fetch_area(TEMPE, 5)
    assert [p.name for p in places] == ["Only"] and covered == 5


@respx.mock
async def test_bad_key_falls_back_to_overpass():
    respx.get(GEOAPIFY).mock(return_value=httpx.Response(401, json={"message": "Invalid apiKey"}))
    overpass = respx.post(OVERPASS).mock(return_value=httpx.Response(200, json={"elements": [
        {"type": "node", "id": 9, "lat": 33.42, "lon": -111.94, "tags": {"name": "Backup Place", "amenity": "restaurant"}},
    ]}))
    async with httpx.AsyncClient() as client:
        geo = GeoapifyPlacesProvider(client, GEOAPIFY, "bad")
        with pytest.raises(ProviderUnavailable):
            await geo.fetch_area(TEMPE, 5)
        cache = AreaCachedPlaces(FallbackAreaSource(geo, OverpassPlacesProvider(client, OVERPASS, "ua", timeout=2)), 5, 3600)
        found = await cache.search(TEMPE, 5, set())
    assert [p.name for p in found] == ["Backup Place"] and overpass.call_count == 1
    assert "out center tags 3000" in parse_qs(overpass.calls[0].request.content.decode())["data"][0]


def test_geoapify_requires_key():
    with pytest.raises(ConfigError):
        Settings(_env_file=None, places_provider="geoapify", enable_live_search=True)
    s = Settings(_env_file=None, places_provider="geoapify", enable_live_search=True, geoapify_api_key="k")
    assert s.places_live
