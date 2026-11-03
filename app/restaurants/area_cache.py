"""Fetch every food place around an area once, then answer searches from memory.

Changing cuisine, budget, diet or a smaller radius costs no new request: the bot filters the saved
list. Concurrent searches for the same area (the parallel cuisine + diet-alternative gather) share
one fetch. Areas expire after `ttl_seconds` so hours and new places refresh.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Callable
from dataclasses import dataclass

from app.domain.models import GeoPoint, Place
from app.restaurants.base import AreaSource, ProviderError, filter_places
from app.restaurants.maps import haversine_miles

log = logging.getLogger("foodie.places")
SAME_SPOT_MILES = 0.25
MAX_AREAS = 64


@dataclass
class _Area:
    center: GeoPoint
    fetched_radius: float
    covered_radius: float
    places: list[Place]
    fetched_at: float

    def answers(self, center: GeoPoint, radius: float) -> bool:
        d = haversine_miles(self.center.lat, self.center.lon, center.lat, center.lon)
        if d + radius <= self.covered_radius:
            return True
        # A capped (nearest-first) fetch from this same spot is the best answer the source will give.
        return d <= SAME_SPOT_MILES and radius <= self.fetched_radius and self.covered_radius > 0


class AreaCachedPlaces:
    def __init__(self, source: AreaSource, min_area_miles: float, ttl_seconds: float, clock: Callable[[], float] = time.monotonic) -> None:
        self.source, self.min_area_miles, self.ttl, self.clock = source, min_area_miles, ttl_seconds, clock
        self._areas: list[_Area] = []
        self._lock = asyncio.Lock()
        self.hits = 0
        self.fetches = 0

    @property
    def name(self) -> str:
        return self.source.name

    @property
    def attribution(self) -> str:
        return self.source.attribution

    async def search(self, center: GeoPoint, radius_miles: float, cuisine_tags: set[str]) -> list[Place]:
        area = self._find(center, radius_miles)
        if area is None:
            async with self._lock:
                area = self._find(center, radius_miles)
                if area is None:
                    area = await self._fetch(center, max(radius_miles, self.min_area_miles))
                else:
                    self.hits += 1
        else:
            self.hits += 1
        return filter_places(area.places, center, radius_miles, cuisine_tags)

    def _find(self, center: GeoPoint, radius: float) -> _Area | None:
        now = self.clock()
        self._areas = [a for a in self._areas if now - a.fetched_at < self.ttl]
        return next((a for a in self._areas if a.answers(center, radius)), None)

    async def _fetch(self, center: GeoPoint, radius: float) -> _Area:
        self.fetches += 1
        places, covered = await self.source.fetch_area(center, radius)
        area = _Area(center, radius, covered, places, self.clock())
        if covered > 0:
            self._areas = (self._areas + [area])[-MAX_AREAS:]
        log.info("area fetched provider=%s places=%d radius=%.1f covered=%.1f", self.source.name, len(places), radius, covered)
        return area


class FallbackAreaSource:
    """Use the fast source; on any provider error fall back to the free public one."""

    def __init__(self, primary: AreaSource, backup: AreaSource) -> None:
        self.primary, self.backup = primary, backup
        self.name = primary.name
        self.attribution = primary.attribution

    async def fetch_area(self, center: GeoPoint, radius_miles: float) -> tuple[list[Place], float]:
        try:
            return await self.primary.fetch_area(center, radius_miles)
        except ProviderError as e:
            log.warning("%s failed (%s); falling back to %s", self.primary.name, type(e).__name__, self.backup.name)
            return await self.backup.fetch_area(center, radius_miles)
