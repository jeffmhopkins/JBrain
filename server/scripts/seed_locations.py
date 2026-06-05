"""Seed a large synthetic location trail for map-performance testing.

The ingest endpoints enforce keep-if ≥30 m moved OR ≥60 min elapsed (per source) and
`/bulk` caps 5000/request — so a generator that scatters tight dwell clusters through the
API has nearly every point dropped, and you can't reproduce the dense trails the map gets
laggy on. This script therefore writes DIRECTLY to the SQLite DB (mirroring the columns at
routers/locations.py), bypassing dedup, so you can land tens of thousands of clustered
fixes for profiling.

Run from the server/ directory. It writes DIRECTLY to the configured db_path, so point a
throwaway DB at it (it refuses an apparent production DB without --force):

    DB_PATH=/tmp/seed.db python -m scripts.seed_locations --count 18000 --people Mom,Dad --days 120

Each person gets a continuous random-walk track (mostly small steps with occasional long
hops + multi-fix dwells) spanning the window, plus a `people` row so the trail viewer
colours them distinctly. Re-running APPENDS; pass --reset to clear the locations table and
any people this script created first.
"""
from __future__ import annotations

import argparse
import math
import random
from datetime import datetime, timedelta, timezone

from app.db import get_conn, init_db
from app.services import people as people_svc

# Distinct, readable colours assigned round-robin to seeded people.
_COLORS = ["#4ea1ff", "#ff7043", "#66bb6a", "#ab47bc", "#ffca28", "#26c6da"]


def _ensure_person(conn, name: str, color: str) -> int:
    """Create the person (idempotent by unique name) so source→colour mapping works."""
    row = conn.execute("SELECT id FROM people WHERE name = ?", (name,)).fetchone()
    if row:
        return row["id"]
    cur = conn.execute(
        "INSERT INTO people (name, color, aliases) VALUES (?, ?, ?)", (name, color, name)
    )
    return cur.lastrowid


def _walk(n: int, start: datetime, span_s: float, lat0: float, lon0: float):
    """Yield (lat, lon, recorded_at) for a plausible moving track of n fixes whose
    timestamps spread, on average, across `span_s` seconds (jittered per step)."""
    lat, lon = lat0, lon0
    t = start
    mean_step = max(1.0, span_s / max(1, n))   # average seconds between fixes
    for _ in range(n):
        yield lat, lon, t.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
        r = random.random()
        if r < 0.12:                       # long hop (a drive leg): ~1–4 km
            d = random.uniform(0.01, 0.04)
        elif r < 0.30:                     # dwell: barely move, short interval (stacks points)
            d = random.uniform(0.0, 0.0002)
        else:                              # walk: ~30–200 m
            d = random.uniform(0.0003, 0.002)
        ang = random.uniform(0, 2 * math.pi)
        lat += d * math.sin(ang)
        lon += d * math.cos(ang) / max(0.2, math.cos(math.radians(lat)))
        # keep it on the planet
        lat = max(-85.0, min(85.0, lat))
        # advance time by a jittered fraction of the mean step (0.2×…1.8×)
        t += timedelta(seconds=mean_step * random.uniform(0.2, 1.8))


def main() -> None:
    ap = argparse.ArgumentParser(description="Seed synthetic location fixes (direct DB insert).")
    ap.add_argument("--count", type=int, default=18000, help="total fixes across all people")
    ap.add_argument("--people", default="Mom,Dad", help="comma-separated person names")
    ap.add_argument("--days", type=int, default=120, help="span the track over this many days, ending now")
    ap.add_argument("--reset", action="store_true", help="clear locations + seeded people first (DESTRUCTIVE)")
    ap.add_argument("--force", action="store_true", help="allow running against a non-throwaway DB path")
    args = ap.parse_args()

    names = [p.strip() for p in args.people.split(",") if p.strip()]
    if not names:
        raise SystemExit("need at least one --people name")

    # Footgun guard: this writes DIRECTLY to settings.db_path (default /data/brain.db, the
    # PRODUCTION DB) and --reset does `DELETE FROM locations` (ALL history, not just seeded
    # rows). Refuse the real DB unless the caller explicitly opts in. Point a throwaway DB
    # at it instead, e.g.  DB_PATH=/tmp/seed.db python -m scripts.seed_locations ...
    from app.config import get_settings
    db_path = get_settings().db_path
    print(f"Target DB: {db_path}")
    looks_production = db_path == "/data/brain.db" or db_path.endswith("brain.db")
    if looks_production and not args.force:
        raise SystemExit(
            "Refusing to seed what looks like the production DB. Re-run with DB_PATH=/tmp/… "
            "for a throwaway DB, or pass --force if you really mean this DB."
        )
    if args.reset and not args.force:
        raise SystemExit("--reset is destructive (DELETE FROM locations). Pass --force to confirm.")

    init_db()   # create the schema if this is a fresh DB (e.g. a throwaway test path)
    conn = get_conn()
    if args.reset:
        conn.execute("DELETE FROM locations")
        conn.execute("DELETE FROM people WHERE name IN (%s)" % ",".join("?" * len(names)), names)
        conn.commit()

    per = max(1, args.count // len(names))
    start = datetime.now(timezone.utc) - timedelta(days=args.days)
    # Spread fixes across the window: even spacing as a baseline, jittered by the walk.
    total = 0
    for idx, name in enumerate(names):
        _ensure_person(conn, name, _COLORS[idx % len(_COLORS)])
        # Resolve person_id the SAME way ingest does (routers/locations.py), so seeded
        # fixes carry the identical person_id production would assign for this source.
        resolved = people_svc.resolve(conn, name)
        pid = resolved["id"] if resolved else None
        # Give each person a different home base.
        lat0 = 40.0 + idx * 0.3 + random.uniform(-0.05, 0.05)
        lon0 = -74.0 + idx * 0.3 + random.uniform(-0.05, 0.05)
        rows = list(_walk(per, start, args.days * 86400, lat0, lon0))
        conn.executemany(
            "INSERT INTO locations (lat, lon, accuracy_m, recorded_at, source, person_id) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            [(lat, lon, random.uniform(5, 25), ts, name, pid) for (lat, lon, ts) in rows],
        )
        total += len(rows)
        print(f"  {name}: {len(rows)} fixes (person_id={pid})")
    conn.commit()
    # Sanity: each source must resolve to a person actually NAMED that source (not just the
    # default fallback resolve() returns for unknown sources) — i.e. colouring is distinct.
    for name in names:
        r = people_svc.resolve(conn, name)
        assert r and r["name"] == name, f"resolve() did not map {name!r} to its own person"
    print(f"Seeded {total} fixes for {len(names)} people over {args.days} days.")


if __name__ == "__main__":
    main()
