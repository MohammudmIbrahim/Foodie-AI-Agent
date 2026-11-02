"""Live OpenStreetMap providers: Nominatim geocoding and Overpass place search.

Policy notes (https://operations.osmfoundation.org/policies/nominatim/, Overpass etiquette):
identify the app via User-Agent, <= 1 request/second, cache results, no bulk/auto-complete use,
show attribution. Public instances may be slow or at capacity; failures surface as provider
errors and the bot tells the group rather than retrying in a loop.
"""

from __future__ import annotations

import asyncio
import time
from datetime import datetime, timezone

import httpx

from app.domain.models import GeoPoint, Place
from app.restaurants.base import (
    ProviderRateLimited,
    ProviderTimeout,
    ProviderUnavailable,
    place_from_osm_element,
)
from app.restaurants.maps import METERS_PER_MILE

ATTRIBUTION = "Data (c) OpenStreetMap contributors (ODbL)"


class _RateGate:
    """Minimum interval between requests to one public service (shared across groups)."""

    def __init__(self, min_interval: float) -> None:
        self.min_interval = min_interval
        self._lock = asyncio.Lock()
        self._last = 0.0

    async def wait(self) -> None:
        async with self._lock:
            delay = self._last + self.min_interval - time.monotonic()
            if delay > 0:
                await asyncio.sleep(delay)
            self._last = time.monotonic()


class _TTLCache:
    def __init__(self, ttl: float) -> None:
        self.ttl = ttl
        self._data: dict[object, tuple[float, object]] = {}
        self.hits = 0
        self.misses = 0

    def get(self, key: object) -> object | None:
        item = self._data.get(key)
        if item and time.monotonic() - item[0] < self.ttl:
            self.hits += 1
            return item[1]
        self.misses += 1
        return None

    def put(self, key: object, value: object) -> None:
        if len(self._data) > 256:
            self._data.clear()
        self._data[key] = (time.monotonic(), value)


def _raise_for(resp: httpx.Response) -> None:
    if resp.status_code == 429:
        raise ProviderRateLimited("rate limited")
    if resp.status_code in (502, 503, 504) or resp.status_code >= 500:
        raise ProviderUnavailable(f"upstream {resp.status_code}")
    if resp.status_code >= 400:
        raise ProviderUnavailable(f"bad request {resp.status_code}")


class NominatimGeocoder:
    def __init__(self, client: httpx.AsyncClient, url: str, user_agent: str, timeout: float = 10, cache_seconds: int = 86400) -> None:
        self.client, self.url, self.user_agent, self.timeout = client, url, user_agent, timeout
        self.cache = _TTLCache(cache_seconds)
        self.gate = _RateGate(1.1)
        self.calls = 0

    async def geocode(self, query: str) -> GeoPoint | None:
        q = " ".join(query.split())[:100]
        if not q:
            return None
        cached = self.cache.get(q.lower())
        if cached is not None:
            return cached if isinstance(cached, GeoPoint) else None
        await self.gate.wait()
        self.calls += 1
        try:
            resp = await self.client.get(
                self.url,
                params={"q": q, "format": "jsonv2", "limit": 1},
                headers={"User-Agent": self.user_agent},
                timeout=self.timeout,
            )
        except httpx.TimeoutException as e:
            raise ProviderTimeout("geocoder timeout") from e
        except httpx.HTTPError as e:
            raise ProviderUnavailable("geocoder unreachable") from e
        _raise_for(resp)
        try:
            rows = resp.json()
            point = GeoPoint(float(rows[0]["lat"]), float(rows[0]["lon"]), str(rows[0]["display_name"])[:120]) if rows else None
        except (ValueError, KeyError, IndexError, TypeError) as e:
            raise ProviderUnavailable("unparseable geocoder response") from e
        self.cache.put(q.lower(), point or False)
        return point


class OverpassPlacesProvider:
    name = "osm"
    attribution = ATTRIBUTION

    def __init__(self, client: httpx.AsyncClient, url: str, user_agent: str, timeout: float = 20, cache_seconds: int = 900) -> None:
        self.client, self.url, self.user_agent, self.timeout = client, url, user_agent, timeout
        self.cache = _TTLCache(cache_seconds)
        self.gate = _RateGate(1.0)
        self.calls = 0

    @staticmethod
    def build_query(center: GeoPoint, radius_miles: float, cuisine_tags: set[str], timeout: int) -> str:
        radius_m = int(radius_miles * METERS_PER_MILE)
        cuisine = ""
        if cuisine_tags:
            alts = "|".join(sorted(t for t in cuisine_tags if t.replace("_", "").replace("-", "").isalnum()))
            cuisine = f'["cuisine"~"(^|;)({alts})($|;)"]'
        sel = f'["amenity"~"^(restaurant|fast_food|food_court)$"]["name"]{cuisine}(around:{radius_m},{center.lat:.6f},{center.lon:.6f})'
        out = f"[out:json][timeout:{timeout}];(nwr{sel};"
        if cuisine:
            # Food courts / mixed venues are the natural compromise candidates.
            out += f'nwr["amenity"="food_court"]["name"](around:{radius_m},{center.lat:.6f},{center.lon:.6f});'
        return out + ");out center tags 300;"

    async def search(self, center: GeoPoint, radius_miles: float, cuisine_tags: set[str]) -> list[Place]:
        key = (round(center.lat, 3), round(center.lon, 3), round(radius_miles, 1), tuple(sorted(cuisine_tags)))
        cached = self.cache.get(key)
        if cached is not None:
            return list(cached)  # type: ignore[arg-type]
        query = self.build_query(center, radius_miles, cuisine_tags, int(self.timeout))
        await self.gate.wait()
        self.calls += 1
        try:
            resp = await self.client.post(
                self.url, data={"data": query}, headers={"User-Agent": self.user_agent}, timeout=self.timeout + 5
            )
        except httpx.TimeoutException as e:
            raise ProviderTimeout("overpass timeout") from e
        except httpx.HTTPError as e:
            raise ProviderUnavailable("overpass unreachable") from e
        _raise_for(resp)
        try:
            data = resp.json()
            elements = data["elements"]
        except (ValueError, KeyError, TypeError) as e:
            raise ProviderUnavailable("unparseable overpass response") from e
        fetched_at = (data.get("osm3s") or {}).get("timestamp_osm_base") or datetime.now(timezone.utc).isoformat()
        places = [p for el in elements if (p := place_from_osm_element(el, fetched_at))]
        self.cache.put(key, places)
        return places
