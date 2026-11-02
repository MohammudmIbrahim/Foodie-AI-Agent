"""Google Maps URLs from real coordinates and straight-line distance."""

from __future__ import annotations

import math
from urllib.parse import quote, urlencode

EARTH_RADIUS_MILES = 3958.7613
METERS_PER_MILE = 1609.344


def haversine_miles(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = p2 - p1, math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * EARTH_RADIUS_MILES * math.asin(math.sqrt(a))


def _coords(lat: float, lon: float) -> str:
    if not (-90 <= lat <= 90 and -180 <= lon <= 180):
        raise ValueError("invalid coordinates")
    return f"{lat:.6f},{lon:.6f}"


def maps_search_url(lat: float, lon: float, name: str | None = None) -> str:
    """https://www.google.com/maps/search/?api=1&query=<url-encoded lat,lng>.

    Name is deliberately excluded from `query` so the pin is the provider's real coordinates.
    """
    return "https://www.google.com/maps/search/?" + urlencode(
        {"api": "1", "query": _coords(lat, lon)}, quote_via=quote
    )


def maps_directions_url(lat: float, lon: float) -> str:
    """No origin: each person's own device resolves their location; Foodie never learns it."""
    return "https://www.google.com/maps/dir/?" + urlencode(
        {"api": "1", "destination": _coords(lat, lon)}, quote_via=quote
    )
