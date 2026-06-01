"""Pure geo helpers — great-circle distance + bearing. No DB, no network.

Done in Python (not SQL) so it's deterministic and independent of the SQLite
build flags (some builds lack the math functions).
"""
from __future__ import annotations

import math

EARTH_KM = 6371.0088   # mean Earth radius


def valid_coord(lat, lon) -> bool:
    try:
        lat, lon = float(lat), float(lon)
    except (TypeError, ValueError):
        return False
    return math.isfinite(lat) and math.isfinite(lon) and -90 <= lat <= 90 and -180 <= lon <= 180


def haversine_km(lat1, lon1, lat2, lon2) -> float:
    """Great-circle distance in km. Correct across the antimeridian and at the
    poles; the min(1, …) clamps floating-point overshoot at near-identical points."""
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlam = math.radians(lon2 - lon1)
    a = math.sin(dphi / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dlam / 2) ** 2
    return 2 * EARTH_KM * math.asin(min(1.0, math.sqrt(a)))


def km_to_miles(km: float) -> float:
    return km * 0.621371


def bearing_deg(lat1, lon1, lat2, lon2) -> float:
    """Initial compass bearing (degrees from true north, clockwise)."""
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dlam = math.radians(lon2 - lon1)
    y = math.sin(dlam) * math.cos(p2)
    x = math.cos(p1) * math.sin(p2) - math.sin(p1) * math.cos(p2) * math.cos(dlam)
    return (math.degrees(math.atan2(y, x)) + 360) % 360


_COMPASS = ["N", "NNE", "NE", "ENE", "E", "ESE", "SE", "SSE",
            "S", "SSW", "SW", "WSW", "W", "WNW", "NW", "NNW"]


def compass(bearing: float) -> str:
    return _COMPASS[round(bearing / 22.5) % 16]
