"""Geoapify Places: OpenStreetMap restaurant data from a fast hosted API (free key, ~3,000 credits/day).

A request costs 1 credit plus 1 per 20 places returned, so an area is fetched once and cached by
AreaCachedPlaces rather than queried per search. Results are biased to the center, which makes them
nearest-first: when the place cap is hit, everything closer than the farthest place returned is
still complete. Free-plan terms ask for "Powered by Geoapify" next to the data.
"""

from __future__ import annotations

from datetime import datetime, timezone

import httpx

from app.domain.models import GeoPoint, Place
from app.restaurants.base import (
    ProviderRateLimited,
    ProviderTimeout,
    ProviderUnavailable,
    place_from_osm_element,
)
from app.restaurants.maps import METERS_PER_MILE, haversine_miles

ATTRIBUTION = "Data (c) OpenStreetMap contributors (ODbL), powered by Geoapify"
CATEGORIES = "catering.restaurant,catering.fast_food,catering.food_court"
PAGE_LIMIT = 500
_OSM_TYPES = {"n": "node", "w": "way", "r": "relation", "node": "node", "way": "way", "relation": "relation"}


def _amenity(categories: list[str]) -> str:
    if any(c.startswith("catering.food_court") for c in categories):
        return "food_court"
    if any(c.startswith("catering.fast_food") for c in categories):
        return "fast_food"
    return "restaurant"


def place_from_geoapify_feature(feature: dict, fetched_at: str) -> Place | None:
    """Normalize a Geoapify feature through the OSM path so evidence rules stay identical.

    Prefers the original OSM tags (datasource.raw); falls back to Geoapify's own fields.
    """
    props = feature.get("properties") or {}
    raw = dict((props.get("datasource") or {}).get("raw") or {})
    osm_id, osm_type = raw.pop("osm_id", None), _OSM_TYPES.get(str(raw.pop("osm_type", "")), "node")
    tags = {k: str(v) for k, v in raw.items() if isinstance(v, (str, int, float))}
    tags.setdefault("name", props.get("name") or "")
    tags.setdefault("amenity", _amenity(props.get("categories") or []))
    catering = props.get("catering") or {}
    if catering.get("cuisine"):
        tags.setdefault("cuisine", str(catering["cuisine"]))
    for diet, value in (catering.get("diet") or {}).items():
        if isinstance(value, bool):
            tags.setdefault(f"diet:{diet}", "yes" if value else "no")
    if props.get("opening_hours"):
        tags.setdefault("opening_hours", str(props["opening_hours"]))
    for key, prop in (("addr:housenumber", "housenumber"), ("addr:street", "street"), ("addr:city", "city")):
        if props.get(prop):
            tags.setdefault(key, str(props[prop]))
    if props.get("website"):
        tags.setdefault("website", str(props["website"]))
    el = {"type": osm_type, "id": abs(int(osm_id)) if osm_id is not None else 0, "lat": props.get("lat"), "lon": props.get("lon"), "tags": tags}
    place = place_from_osm_element(el, fetched_at)
    if place and osm_id is None:
        # No OSM reference: identify by Geoapify's id and don't link a made-up OSM page.
        place.provider_place_id = f"geoapify:{props.get('place_id') or f'{place.latitude:.6f},{place.longitude:.6f}'}"
        place.source_url = None
    return place


class GeoapifyPlacesProvider:
    name = "geoapify"
    attribution = ATTRIBUTION

    def __init__(self, client: httpx.AsyncClient, url: str, api_key: str, timeout: float = 10, max_places: int = 1000) -> None:
        self.client, self.url, self.api_key, self.timeout = client, url, api_key, timeout
        self.max_places = max_places
        self.calls = 0

    async def fetch_area(self, center: GeoPoint, radius_miles: float) -> tuple[list[Place], float]:
        fetched_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
        places: list[Place] = []
        offset, capped = 0, False
        while True:
            limit = min(PAGE_LIMIT, self.max_places - offset)
            features = await self._page(center, radius_miles, limit, offset)
            places += [p for f in features if (p := place_from_geoapify_feature(f, fetched_at))]
            offset += len(features)
            if len(features) < limit:
                break
            if offset >= self.max_places:
                capped = True
                break
        if not capped or not places:
            return places, radius_miles
        return places, max(haversine_miles(center.lat, center.lon, p.latitude, p.longitude) for p in places)

    async def _page(self, center: GeoPoint, radius_miles: float, limit: int, offset: int) -> list[dict]:
        self.calls += 1
        params = {
            "categories": CATEGORIES,
            "filter": f"circle:{center.lon:.6f},{center.lat:.6f},{int(radius_miles * METERS_PER_MILE)}",
            "bias": f"proximity:{center.lon:.6f},{center.lat:.6f}",
            "limit": limit,
            "offset": offset,
            "apiKey": self.api_key,
        }
        try:
            resp = await self.client.get(self.url, params=params, timeout=self.timeout)
        except httpx.TimeoutException as e:
            raise ProviderTimeout("geoapify timeout") from e
        except httpx.HTTPError as e:
            raise ProviderUnavailable("geoapify unreachable") from e
        if resp.status_code == 429:
            raise ProviderRateLimited("geoapify rate limited")
        if resp.status_code in (401, 403):
            raise ProviderUnavailable("geoapify key rejected")
        if resp.status_code >= 400:
            raise ProviderUnavailable(f"geoapify {resp.status_code}")
        try:
            features = resp.json()["features"]
        except (ValueError, KeyError, TypeError) as e:
            raise ProviderUnavailable("unparseable geoapify response") from e
        return features if isinstance(features, list) else []
