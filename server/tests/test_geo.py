import math
from app.services import geo


def test_haversine_known_distances():
    # NYC (40.7128,-74.0060) -> LA (34.0522,-118.2437) ≈ 3936 km
    d = geo.haversine_km(40.7128, -74.0060, 34.0522, -118.2437)
    assert abs(d - 3936) < 30
    # 1° of latitude at the equator ≈ 111 km
    assert abs(geo.haversine_km(0, 0, 1, 0) - 111.19) < 0.5
    # identical points -> 0 (no FP blowup)
    assert geo.haversine_km(51.5, -0.12, 51.5, -0.12) == 0.0
    # antimeridian: +179.9 to -179.9 is ~22 km near the equator, not ~40000
    assert geo.haversine_km(0, 179.9, 0, -179.9) < 30


def test_valid_coord_and_units():
    assert geo.valid_coord(0, 0) and geo.valid_coord(-90, 180)
    assert not geo.valid_coord(91, 0) and not geo.valid_coord(0, 181)
    assert not geo.valid_coord(float("nan"), 0) and not geo.valid_coord("x", 0)
    assert abs(geo.km_to_miles(100) - 62.137) < 0.01
    assert geo.compass(0) == "N" and geo.compass(90) == "E"
