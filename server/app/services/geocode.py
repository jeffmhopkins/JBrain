"""Street-address geocoding via Nominatim (OpenStreetMap) — JBrain's first outside
information source.

  reverse(lat, lon)   — a coordinate → a SUSPECTED street address.
  forward(query)      — an address / place name → ranked coordinate candidates.

Every call is cached in `geocode_cache` so a given spot or query is only ever sent to the
public geocoder once (this also keeps us inside Nominatim's ≤1 req/s usage policy; live
calls are throttled too). Results are SUSPECTED/external — tagged with their source, never
treated as owner-asserted fact. Coordinates are rounded before they leave (privacy + a
stable cache key). `_http_get` is factored out so tests stub it (no network).
"""
from __future__ import annotations

import json
import threading
import time
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone

from ..config import get_settings

_UA = "JBrain/0.1 (+https://github.com/jeffmhopkins/JBrain; self-hosted personal brain)"
_TIMEOUT = 8.0
_MIN_INTERVAL = 1.1          # seconds between LIVE calls (Nominatim public policy ≤1/s)
_CACHE_TTL_DAYS = 180        # addresses are stable; let an entry re-validate twice a year
_REV_PRECISION = 5           # ~1 m grid for the reverse cache key (also caps stored precision)

_throttle = threading.Lock()
_last_call = [0.0]


def _base() -> str:
    return (get_settings().geocoder_url or "").strip().rstrip("/")


def enabled() -> bool:
    """Geocoding is on only when a geocoder URL is configured (blank = disabled)."""
    return bool(_base())


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%f")


def _http_get(url: str):
    """One Nominatim GET → parsed JSON. Factored out so tests stub it (no network)."""
    req = urllib.request.Request(url, headers={"User-Agent": _UA})
    with urllib.request.urlopen(req, timeout=_TIMEOUT) as r:
        return json.loads(r.read().decode("utf-8"))


def _throttled_get(url: str):
    """Serialize live calls and keep ≥ _MIN_INTERVAL between them (good Nominatim citizen)."""
    with _throttle:
        wait = _MIN_INTERVAL - (time.monotonic() - _last_call[0])
        if wait > 0:
            time.sleep(wait)
        try:
            return _http_get(url)
        finally:
            _last_call[0] = time.monotonic()


def _cache_get(conn, kind: str, key: str):
    row = conn.execute(
        "SELECT payload_json, fetched_at FROM geocode_cache WHERE kind=? AND key=?", (kind, key)
    ).fetchone()
    if not row:
        return None
    try:
        t = datetime.fromisoformat(row["fetched_at"])
        if t.tzinfo is None:
            t = t.replace(tzinfo=timezone.utc)
        if datetime.now(timezone.utc) - t > timedelta(days=_CACHE_TTL_DAYS):
            return None                      # stale → re-fetch
    except Exception:  # noqa: BLE001 — a bad timestamp just means "miss"
        pass
    try:
        return json.loads(row["payload_json"])
    except Exception:  # noqa: BLE001
        return None


def _cache_put(conn, kind: str, key: str, payload) -> None:
    conn.execute(
        "INSERT INTO geocode_cache (kind, key, payload_json, fetched_at) "
        "VALUES (?,?,?,?) ON CONFLICT(kind, key) DO UPDATE SET "
        "payload_json=excluded.payload_json, fetched_at=excluded.fetched_at",
        (kind, key, json.dumps(payload), _now()),
    )
    conn.commit()


def reverse(conn, lat: float, lon: float) -> dict | None:
    """A suspected street address for a coordinate, or None (disabled / nothing found).
    Returns {address, lat, lon, type, components, source, fetched_at, cached}."""
    if not enabled():
        return None
    rlat, rlon = round(float(lat), _REV_PRECISION), round(float(lon), _REV_PRECISION)
    key = f"{rlat},{rlon}"
    cached = _cache_get(conn, "reverse", key)
    if cached is not None:
        return {**cached, "cached": True} if cached else None   # {} == cached "no match"
    url = f"{_base()}/reverse?lat={rlat}&lon={rlon}&format=jsonv2&addressdetails=1&zoom=18"
    try:
        data = _throttled_get(url)
    except Exception:  # noqa: BLE001 — network hiccup: don't cache, just report nothing
        return None
    if not isinstance(data, dict) or not data.get("display_name"):
        _cache_put(conn, "reverse", key, {})   # negative cache: don't re-hit a dead spot
        return None
    out = {
        "address": data.get("display_name"),
        "lat": float(data.get("lat", rlat)),
        "lon": float(data.get("lon", rlon)),
        "type": data.get("addresstype") or data.get("type"),
        "components": data.get("address") or {},
        "source": "nominatim",
        "fetched_at": _now(),
    }
    _cache_put(conn, "reverse", key, out)
    return {**out, "cached": False}


def forward(conn, query: str, limit: int = 5) -> list[dict]:
    """Ranked coordinate candidates for an address/place query, or [] (disabled / none).
    Each: {address, lat, lon, type, importance, source}."""
    if not enabled() or not (query or "").strip():
        return []
    q = " ".join(query.split())[:200]
    lim = max(1, min(int(limit or 5), 10))
    key = f"{lim}|{q.lower()}"
    cached = _cache_get(conn, "forward", key)
    if cached is not None:
        return cached
    url = f"{_base()}/search?q={urllib.parse.quote(q)}&format=jsonv2&addressdetails=1&limit={lim}"
    try:
        data = _throttled_get(url)
    except Exception:  # noqa: BLE001
        return []
    out: list[dict] = []
    if isinstance(data, list):
        for d in data:
            try:
                out.append({
                    "address": d.get("display_name"),
                    "lat": float(d["lat"]), "lon": float(d["lon"]),
                    "type": d.get("addresstype") or d.get("type"),
                    "importance": d.get("importance"),
                    "source": "nominatim",
                })
            except (KeyError, TypeError, ValueError):
                continue
    _cache_put(conn, "forward", key, out)
    return out
