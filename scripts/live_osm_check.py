"""One-off manual check of the LIVE OpenStreetMap adapters (1 Nominatim + 1 Overpass request).

Not run by tests. Respects public service policies: identifying User-Agent, single requests.
Run: python scripts/live_osm_check.py "Tempe, AZ" burger
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.config import Settings  # noqa: E402
from app.domain.parsing import CUISINE_TAGS  # noqa: E402
from app.restaurants.base import ProviderError  # noqa: E402
from app.restaurants.maps import haversine_miles  # noqa: E402
from app.restaurants.osm import NominatimGeocoder, OverpassPlacesProvider  # noqa: E402


async def main(area: str, cuisine: str) -> int:
    s = Settings()
    async with httpx.AsyncClient() as client:
        geo = NominatimGeocoder(client, s.nominatim_url, s.osm_user_agent)
        point = await geo.geocode(area)
        if not point:
            print(f"Nominatim found nothing for {area!r}")
            return 1
        print(f"Geocoded: {point.label} ({point.lat:.5f}, {point.lon:.5f})")
        places = OverpassPlacesProvider(client, s.overpass_url, s.osm_user_agent, s.places_timeout_seconds)
        try:
            found = await places.search(point, 2.0, set(CUISINE_TAGS.get(cuisine, {cuisine})))
        except ProviderError as e:
            print(f"Overpass unavailable: {type(e).__name__}: {e}. Public instances are often at capacity; try later.")
            return 2
    print(f"Overpass returned {len(found)} places within 2 mi:")
    for p in found[:8]:
        d = haversine_miles(point.lat, point.lon, p.latitude, p.longitude)
        print(f"- {p.name} | {', '.join(p.cuisine_tags) or 'cuisine unknown'} | {d:.1f} mi | hours: {p.opening_hours or 'unknown'} | {p.source_url}")
    return 0


if __name__ == "__main__":
    area = sys.argv[1] if len(sys.argv) > 1 else "Tempe, AZ"
    cuisine = sys.argv[2] if len(sys.argv) > 2 else "burger"
    raise SystemExit(asyncio.run(main(area, cuisine)))
