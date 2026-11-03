"""Places / geocoding provider interfaces and shared OSM element normalization."""

from __future__ import annotations

import re
from typing import Protocol

from app.domain.models import Evidence, GeoPoint, Place
from app.restaurants.maps import haversine_miles, maps_search_url


class ProviderError(Exception):
    """Base error for places/geocoding providers."""


class ProviderTimeout(ProviderError):
    pass


class ProviderRateLimited(ProviderError):
    pass


class ProviderUnavailable(ProviderError):
    pass


class PlacesProvider(Protocol):
    name: str
    attribution: str

    async def search(self, center: GeoPoint, radius_miles: float, cuisine_tags: set[str]) -> list[Place]:
        """Return real places near `center`. May return more than needed; caller filters/ranks."""
        ...


class AreaSource(Protocol):
    """Fetches every food place around a point, nearest first when the result is capped."""

    name: str
    attribution: str

    async def fetch_area(self, center: GeoPoint, radius_miles: float) -> tuple[list[Place], float]:
        """Return (places, covered_radius_miles): every place within the covered radius is included."""
        ...


class Geocoder(Protocol):
    async def geocode(self, query: str) -> GeoPoint | None: ...


def filter_places(places: list[Place], center: GeoPoint, radius_miles: float, cuisine_tags: set[str]) -> list[Place]:
    """Places within the radius that match a cuisine tag; food courts count as a compromise for any cuisine."""
    out = []
    for p in places:
        if haversine_miles(center.lat, center.lon, p.latitude, p.longitude) > radius_miles:
            continue
        if cuisine_tags and not (set(p.cuisine_tags) & cuisine_tags) and p.amenity != "food_court":
            continue
        out.append(p)
    return out


_DIET_KEYS = {
    "halal": "diet:halal", "kosher": "diet:kosher", "vegetarian": "diet:vegetarian",
    "vegan": "diet:vegan", "gluten_free": "diet:gluten_free",
}


def place_from_osm_element(el: dict, fetched_at: str) -> Place | None:
    """Normalize an Overpass element. Returns None when name/coordinates are missing."""
    tags = el.get("tags") or {}
    name = (tags.get("name") or "").strip()
    lat = el.get("lat", (el.get("center") or {}).get("lat"))
    lon = el.get("lon", (el.get("center") or {}).get("lon"))
    if not name or lat is None or lon is None:
        return None
    cuisine = [c.strip().lower() for c in re.split(r"[;,]", tags.get("cuisine", "")) if c.strip()]
    evidence: dict[str, Evidence] = {}
    for diet, key in _DIET_KEYS.items():
        v = (tags.get(key) or "").lower()
        if v in ("yes", "only"):
            evidence[diet] = Evidence.YES
        elif v == "no":
            evidence[diet] = Evidence.NO
    # Vegan-only places satisfy vegetarian.
    if evidence.get("vegan") == Evidence.YES and (tags.get("diet:vegan") == "only") and "vegetarian" not in evidence:
        evidence["vegetarian"] = Evidence.YES
    addr = " ".join(p for p in (tags.get("addr:housenumber"), tags.get("addr:street")) if p)
    if addr and tags.get("addr:city"):
        addr = f"{addr}, {tags['addr:city']}"
    osm_type, osm_id = el.get("type", "node"), el.get("id")
    try:
        return Place(
            provider="osm",
            provider_place_id=f"osm:{osm_type}/{osm_id}",
            name=name[:120],
            latitude=float(lat),
            longitude=float(lon),
            maps_url=maps_search_url(float(lat), float(lon)),
            last_fetched_at=fetched_at,
            address=addr or None,
            cuisine_tags=cuisine,
            opening_hours=tags.get("opening_hours"),
            dietary_evidence=evidence,
            source_url=f"https://www.openstreetmap.org/{osm_type}/{osm_id}",
            amenity=tags.get("amenity"),
            website=tags.get("website"),
        )
    except ValueError:
        return None
