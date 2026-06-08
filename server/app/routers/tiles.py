"""Map-tile proxy + on-disk cache.

So the browser only ever talks to OUR origin (not a third-party tile server), the
location-trail map loads tiles from /api/tiles, which proxies + caches OpenStreetMap
server-side. This keeps the CSP locked to 'self', stops the trail's coordinates from
leaking to a tile host, and respects OSM by caching aggressively.

Tiles are PUBLIC map imagery (no user data), and <img> can't carry the bearer token,
so this route is intentionally UNauthenticated. The sensitive data (the trail itself)
stays behind the access key on /api/locations.
"""
import tempfile
import urllib.request
from pathlib import Path

from fastapi import APIRouter, HTTPException
from fastapi.responses import Response

from ..config import get_settings

router = APIRouter(prefix="/api/tiles", tags=["tiles"])   # no CurrentUser: public imagery, <img>-loaded

_UA = "JBrain/0.1 (+https://github.com/jeffmhopkins/JBrain; self-hosted personal trail viewer)"


def _cache_dir() -> Path:
    """Return the on-disk tile cache directory, sibling to the database file.

    Returns:
        Path to the tilecache directory (not guaranteed to exist yet).
    """
    base = getattr(get_settings(), "db_path", None) or tempfile.gettempdir()
    return Path(base).resolve().parent / "tilecache"


def _fetch(z: int, x: int, y: int) -> bytes:
    """Fetch one OSM tile from the upstream tile server.

    Factored out so tests can stub the network call.

    Args:
        z: Zoom level.
        x: Tile column.
        y: Tile row.

    Returns:
        Raw PNG bytes for the tile.
    """
    url = f"https://tile.openstreetmap.org/{z}/{x}/{y}.png"
    req = urllib.request.Request(url, headers={"User-Agent": _UA})
    with urllib.request.urlopen(req, timeout=10) as r:   # noqa: S310 (fixed OSM host)
        return r.read()


@router.get("/{z}/{x}/{y}.png")
def tile(z: int, x: int, y: int):
    """Serve a cached OpenStreetMap tile, fetching and caching it on first access.

    This route is intentionally unauthenticated: tiles are public map imagery
    (no user data) and img elements cannot carry the bearer token.

    Args:
        z: Zoom level (0–19).
        x: Tile column.
        y: Tile row.

    Returns:
        PNG image response with a one-week cache header.

    Raises:
        HTTPException: 400 if the tile coordinates are out of range.
        HTTPException: 502 if the upstream tile server is unreachable.
    """
    n = 1 << z
    if not (0 <= z <= 19) or not (0 <= x < n) or not (0 <= y < n):
        raise HTTPException(status_code=400, detail="bad tile coordinates")
    path = _cache_dir() / str(z) / str(x) / f"{y}.png"
    if path.is_file():
        data = path.read_bytes()
    else:
        try:
            data = _fetch(z, x, y)
        except Exception:
            raise HTTPException(status_code=502, detail="tile unavailable")
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(data)
        except Exception:
            pass   # cache write is best-effort
    return Response(content=data, media_type="image/png",
                    headers={"Cache-Control": "public, max-age=604800", "X-Content-Type-Options": "nosniff"})
