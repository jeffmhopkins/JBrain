"""Pure geo helpers — great-circle distance + bearing. No DB, no network.

Done in Python (not SQL) so it's deterministic and independent of the SQLite
build flags (some builds lack the math functions).
"""
from __future__ import annotations

import math

EARTH_KM = 6371.0088   # mean Earth radius


def valid_coord(lat, lon) -> bool:
    """Return True if lat and lon are finite, in-range coordinate values.

    Args:
        lat: Latitude value (any type coercible to float).
        lon: Longitude value (any type coercible to float).

    Returns:
        True if both values are finite and within [-90, 90] / [-180, 180].
    """
    try:
        lat, lon = float(lat), float(lon)
    except (TypeError, ValueError):
        return False
    return math.isfinite(lat) and math.isfinite(lon) and -90 <= lat <= 90 and -180 <= lon <= 180


def haversine_km(lat1, lon1, lat2, lon2) -> float:
    """Return the great-circle distance in kilometres between two coordinates.

    Correct across the antimeridian and at the poles. The min(1, ...) clamps
    floating-point overshoot at near-identical points.

    Args:
        lat1: Latitude of the first point in degrees.
        lon1: Longitude of the first point in degrees.
        lat2: Latitude of the second point in degrees.
        lon2: Longitude of the second point in degrees.

    Returns:
        Distance in kilometres.
    """
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlam = math.radians(lon2 - lon1)
    a = math.sin(dphi / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dlam / 2) ** 2
    return 2 * EARTH_KM * math.asin(min(1.0, math.sqrt(a)))


def km_to_miles(km: float) -> float:
    """Convert kilometres to miles.

    Args:
        km: Distance in kilometres.

    Returns:
        Distance in miles.
    """
    return km * 0.621371


def bearing_deg(lat1, lon1, lat2, lon2) -> float:
    """Return the initial compass bearing in degrees from true north (clockwise).

    Args:
        lat1: Latitude of the origin point in degrees.
        lon1: Longitude of the origin point in degrees.
        lat2: Latitude of the destination point in degrees.
        lon2: Longitude of the destination point in degrees.

    Returns:
        Bearing in degrees [0, 360).
    """
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dlam = math.radians(lon2 - lon1)
    y = math.sin(dlam) * math.cos(p2)
    x = math.cos(p1) * math.sin(p2) - math.sin(p1) * math.cos(p2) * math.cos(dlam)
    return (math.degrees(math.atan2(y, x)) + 360) % 360


_COMPASS = ["N", "NNE", "NE", "ENE", "E", "ESE", "SE", "SSE",
            "S", "SSW", "SW", "WSW", "W", "WNW", "NW", "NNW"]


def compass(bearing: float) -> str:
    """Convert a bearing in degrees to a 16-point compass rose abbreviation.

    Args:
        bearing: Bearing in degrees [0, 360).

    Returns:
        Compass abbreviation string, e.g. 'N', 'NNE', 'SW'.
    """
    return _COMPASS[round(bearing / 22.5) % 16]
