"""One-off manual check of LIVE restaurant search (1 geocode + 1 area fetch), using PLACES_PROVIDER.

Runs the cuisine search twice: the second is answered from the saved area with no request.
Not run by tests. Respects public service policies: identifying User-Agent, single requests.
Run: python scripts/live_osm_check.py "Tempe, AZ" burger
"""

from __future__ import annotations

import asyncio
import sys
import time
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.config import Settings  # noqa: E402
from app.domain.parsing import CUISINE_TAGS  # noqa: E402
from app.main import live_places  # noqa: E402
from app.restaurants.base import ProviderError  # noqa: E402
from app.restaurants.maps import haversine_miles  # noqa: E402
from app.restaurants.osm import NominatimGeocoder  # noqa: E402


async def main(area: str, cuisine: str) -> int:
    s = Settings()
    async with httpx.AsyncClient() as client:
        geo = NominatimGeocoder(client, s.nominatim_url, s.osm_user_agent)
        point = await geo.geocode(area)
        if not point:
            print(f"Nominatim found nothing for {area!r}")
            return 1
        print(f"Geocoded: {point.label} ({point.lat:.5f}, {point.lon:.5f})")
        places = live_places(s, client)
        tags = set(CUISINE_TAGS.get(cuisine, {cuisine}))
        try:
            start = time.perf_counter()
            found = await places.search(point, 2.0, tags)
            first = time.perf_counter() - start
            start = time.perf_counter()
            await places.search(point, 2.0, set())
            second = time.perf_counter() - start
        except ProviderError as e:
            print(f"{places.name} unavailable: {type(e).__name__}: {e}. Public Overpass is often at capacity; try later or set a Geoapify key.")
            return 2
    print(f"Source: {places.name}. First search {first:.1f} s (area fetch), next search {second * 1000:.0f} ms (saved area, {places.fetches} fetch).")
    print(f"{len(found)} {cuisine} places within 2 mi:")
    for p in found[:8]:
        d = haversine_miles(point.lat, point.lon, p.latitude, p.longitude)
        print(f"- {p.name} | {', '.join(p.cuisine_tags) or 'cuisine unknown'} | {d:.1f} mi | hours: {p.opening_hours or 'unknown'} | {p.source_url}")
    return 0


if __name__ == "__main__":
    area = sys.argv[1] if len(sys.argv) > 1 else "Tempe, AZ"
    cuisine = sys.argv[2] if len(sys.argv) > 2 else "burger"
    raise SystemExit(asyncio.run(main(area, cuisine)))
