"""Offline providers backed by a *real* OpenStreetMap snapshot saved under tests/fixtures.

No place here is invented: every record came from Overpass with its OSM id, and fields OSM
does not provide (ratings, reviews, price) are left empty.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

from app.domain.models import GeoPoint, Place
from app.restaurants.base import ProviderError, place_from_osm_element
from app.restaurants.maps import haversine_miles


class FixturePlacesProvider:
    name = "osm-snapshot"

    def __init__(self, path: str | Path, latency_seconds: float = 0.0) -> None:
        data = json.loads(Path(path).read_text())
        self.attribution = "Data (c) OpenStreetMap contributors (ODbL), snapshot " + data.get("fetched_at", "")[:10]
        self._fetched_at = data.get("fetched_at", "")
        self._places = [p for el in data["elements"] if (p := place_from_osm_element(el, self._fetched_at))]
        self.latency_seconds = latency_seconds
        self.calls = 0

    async def search(self, center: GeoPoint, radius_miles: float, cuisine_tags: set[str]) -> list[Place]:
        self.calls += 1
        if self.latency_seconds:
            await asyncio.sleep(self.latency_seconds)
        out = []
        for p in self._places:
            if haversine_miles(center.lat, center.lon, p.latitude, p.longitude) > radius_miles:
                continue
            if cuisine_tags and not (set(p.cuisine_tags) & cuisine_tags) and p.amenity != "food_court":
                continue
            out.append(p)
        return out


class FixtureGeocoder:
    """Resolves only the place names captured from Nominatim in the fixture; else unknown."""

    def __init__(self, path: str | Path) -> None:
        data = json.loads(Path(path).read_text())
        self._results = data["results"]
        self.calls = 0

    async def geocode(self, query: str) -> GeoPoint | None:
        self.calls += 1
        key = query.lower().split(",")[0].strip()
        hit = self._results.get(key)
        if not hit:
            return None
        return GeoPoint(hit["lat"], hit["lon"], hit["display_name"])


class FailingPlacesProvider:
    """Test helper: always raises the given provider error."""

    name = "failing"
    attribution = ""

    def __init__(self, exc: ProviderError) -> None:
        self.exc = exc
        self.calls = 0

    async def search(self, center: GeoPoint, radius_miles: float, cuisine_tags: set[str]) -> list[Place]:
        self.calls += 1
        raise self.exc
