"""Single source of truth for time, timezone, and live time-tokens.

Storage stays UTC everywhere (SQLite datetime('now')); this module is how the app
reasons about the owner's LOCAL time — for the agent's "now", date bucketing, log
dates, UI display, and the @t[...] live values in note bodies. ONE resolver
(meta 'app_tz' -> env TZ -> UTC, validated) so cron, daily buckets, the agent,
and the UI never disagree.

The @t[...] token expander has a byte-for-byte twin in web/src/timeTokens.ts; a
shared fixture (server/tests/fixtures/time_tokens.json) pins them together.
"""
from __future__ import annotations

import os
import re
from datetime import date, datetime, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

_UTC = timezone.utc


def _resolve_tz_name() -> str:
    """meta 'app_tz' (settable without redeploy) -> container TZ env -> UTC."""
    name = None
    try:
        from ..db import get_meta
        name = get_meta("app_tz")
    except Exception:
        name = None
    return name or os.environ.get("TZ") or "UTC"


def app_tz() -> ZoneInfo:
    name = _resolve_tz_name()
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError, OSError):
        return ZoneInfo("UTC")   # a bad/typo'd zone must never brick scheduling


def app_tz_name() -> str:
    """The EFFECTIVE zone name (falls back to 'UTC' if the configured one is bad)."""
    name = _resolve_tz_name()
    try:
        ZoneInfo(name)
        return name
    except Exception:
        return "UTC"


def now_utc() -> datetime:
    return datetime.now(_UTC)


def now_local() -> datetime:
    return datetime.now(app_tz())


def today_local() -> date:
    return now_local().date()


def today_iso() -> str:
    return now_local().date().isoformat()


def today_path() -> str:
    """Today as 'YYYY/MM/DD' in the app tz (the dated-capture bucket)."""
    return now_local().strftime("%Y/%m/%d")


def now_prompt() -> str:
    """Human grounding string for the agent system prompt, e.g.
    'Monday, 2026-06-01 14:30 EDT (UTC-04:00)'."""
    n = now_local()
    off = n.strftime("%z") or "+0000"
    off = f"{off[:3]}:{off[3:]}"
    return f"{n:%A, %Y-%m-%d %H:%M} {n.tzname()} (UTC{off})"


# --- Live time tokens: @t[age:DATE] / @t[until:ISO] / @t[since:ISO] ---------
# Single bracket (never [[wikilink]]), no braces (never collides with the recipe
# Jinja engine or the legacy {date} token), safe inside JSON string values.

_TOKEN_RE = re.compile(r"@t\[(age|until|since):([^\]]+)\]")
_UNITS = (("year", 31536000), ("month", 2592000), ("week", 604800),
          ("day", 86400), ("hour", 3600), ("minute", 60))


def _humanize(seconds: float) -> str:
    secs = abs(int(seconds))
    for unit, n in _UNITS:
        if secs >= n:
            v = secs // n
            return f"{v} {unit}{'s' if v != 1 else ''}"
    return "less than a minute"


def _to_dt(arg: str, tz: ZoneInfo) -> datetime | None:
    s = arg.strip().replace(" ", "T")
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    try:
        d = datetime.fromisoformat(s)
    except ValueError:
        return None
    return d.replace(tzinfo=tz) if d.tzinfo is None else d


def expand_tokens(text: str, *, snapshot: bool = False, now: datetime | None = None) -> str:
    """Replace @t[...] tokens with their live value. A malformed/parse-failed
    token is left VERBATIM (never throws, never blanks). `snapshot=True` renders
    a dated, self-explaining value for the evergreen KB (e.g.
    '40 (as of 2026-06-01; born 1986-03-01)') instead of a bare live value.
    `now` (an aware datetime) is injectable for deterministic tests."""
    if not text or "@t[" not in text:
        return text
    tz = app_tz()
    now = now.astimezone(tz) if now is not None else datetime.now(tz)
    today = now.date()

    def repl(m: re.Match) -> str:
        kind, arg = m.group(1), m.group(2).strip()
        dt = _to_dt(arg, tz)
        if dt is None:
            return m.group(0)
        if kind == "age":
            b = dt.date()
            years = today.year - b.year - ((today.month, today.day) < (b.month, b.day))
            if years < 0:
                return m.group(0)
            return (f"{years} (as of {today.isoformat()}; born {b.isoformat()})"
                    if snapshot else str(years))
        delta = (dt - now).total_seconds()
        if kind == "until":
            base = "now" if abs(delta) < 60 else (
                f"in {_humanize(delta)}" if delta > 0 else f"{_humanize(delta)} ago")
        else:  # since
            base = "just now" if abs(delta) < 60 else (
                f"{_humanize(delta)} ago" if delta < 0 else f"in {_humanize(delta)}")
        return f"{base} (as of {today.isoformat()})" if snapshot else base

    return _TOKEN_RE.sub(repl, text)
