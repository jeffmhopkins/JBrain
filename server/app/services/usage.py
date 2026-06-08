"""LLM token metering — a per-call ledger for in-app cost awareness.

Rows are written on a DEDICATED connection (WAL serialises writers, busy_timeout
waits) so recording never touches — or commits — the caller's transaction. Token
counts are EXACT; the dollar figure is an ESTIMATE from a static price table and
will NOT match the Anthropic console (prompt-cache/batch pricing, rounding, or any
usage on a shared key are not reflected). Boundaries are the owner's local day/month
(app_tz), so "today"/"this month" read the way the owner expects.
"""
from __future__ import annotations

from datetime import datetime, timezone

from . import clock

# USD per 1M tokens by model substring — (input, output, cache_read, cache_write).
# Dated 2026-06; update when Anthropic pricing changes. Substring match so a dated
# model id ("claude-opus-4-8") still resolves.
_PRICES = {
    "opus":   (15.0, 75.0, 1.50, 18.75),
    "sonnet": (3.0, 15.0, 0.30, 3.75),
    "haiku":  (1.0, 5.0, 0.10, 1.25),
    # xAI Grok 4.3 (per x.ai pricing): $1.25 in / $2.50 out per 1M. The OpenAI-compat
    # usage we record has no separate cache fields, so cache rates are unused here.
    "grok":   (1.25, 2.50, 0.0, 0.0),
}
_DEFAULT_PRICE = (3.0, 15.0, 0.30, 3.75)   # assume Sonnet-class if unknown


def _rates(model: str) -> tuple:
    """Return the (input, output, cache_read, cache_write) price tuple for a model.

    Args:
        model: Model identifier string (substring-matched against _PRICES keys).

    Returns:
        Tuple of USD per 1M token rates; falls back to _DEFAULT_PRICE if unknown.
    """
    m = (model or "").lower()
    for key, r in _PRICES.items():
        if key in m:
            return r
    return _DEFAULT_PRICE


def _cost(model: str, i: int, o: int, cr: int, cw: int) -> float:
    """Estimate USD cost for one LLM call given token counts.

    Args:
        model: Model identifier used to look up pricing.
        i: Input tokens.
        o: Output tokens.
        cr: Cache-read tokens.
        cw: Cache-write tokens.

    Returns:
        Estimated cost in USD.
    """
    ri, ro, rcr, rcw = _rates(model)
    return (i * ri + o * ro + cr * rcr + cw * rcw) / 1_000_000.0


def record(model: str, input_tokens: int = 0, output_tokens: int = 0,
           cache_read: int = 0, cache_write: int = 0, context: str | None = None) -> None:
    """Log one LLM call to the usage ledger on a dedicated connection.

    Never raises — metering must never break a generation.

    Args:
        model: Model identifier string (e.g. 'claude-sonnet-4').
        input_tokens: Number of input tokens billed.
        output_tokens: Number of output tokens billed.
        cache_read: Cache-read tokens (Anthropic prompt-cache).
        cache_write: Cache-write tokens (Anthropic prompt-cache).
        context: Optional free-text label identifying the call site.
    """
    if not model or not (input_tokens or output_tokens or cache_read or cache_write):
        return
    try:
        from ..db import _connect
        conn = _connect()
        try:
            conn.execute(
                "INSERT INTO llm_usage (model, input_tokens, output_tokens, "
                "cache_read_tokens, cache_write_tokens, context) VALUES (?, ?, ?, ?, ?, ?)",
                (model, int(input_tokens), int(output_tokens), int(cache_read), int(cache_write), context),
            )
            conn.commit()
        finally:
            conn.close()
    except Exception:  # noqa: BLE001
        pass


def _agg(conn, since_utc: str) -> dict:
    """Aggregate usage rows since a UTC timestamp into a summary dict.

    Args:
        conn: SQLite connection.
        since_utc: ISO-format UTC datetime string (inclusive lower bound).

    Returns:
        Dict with keys: input, output, cost, calls, by_model (list sorted by cost desc).
    """
    rows = conn.execute(
        "SELECT model, SUM(input_tokens) i, SUM(output_tokens) o, SUM(cache_read_tokens) cr, "
        "SUM(cache_write_tokens) cw, COUNT(*) n FROM llm_usage WHERE ts >= ? GROUP BY model",
        (since_utc,),
    ).fetchall()
    by_model, tin, tout, tcost, calls = [], 0, 0, 0.0, 0
    for r in rows:
        c = _cost(r["model"], r["i"], r["o"], r["cr"], r["cw"])
        by_model.append({"model": r["model"], "input": r["i"], "output": r["o"],
                         "cache_read": r["cr"], "cache_write": r["cw"], "calls": r["n"], "cost": round(c, 4)})
        tin += r["i"]; tout += r["o"]; tcost += c; calls += r["n"]
    by_model.sort(key=lambda m: m["cost"], reverse=True)
    return {"input": tin, "output": tout, "cost": round(tcost, 2), "calls": calls, "by_model": by_model}


def summary(conn) -> dict:
    """Return today and month-to-date usage aggregates on local day/month boundaries.

    Boundaries use app_tz (the owner's local timezone) converted to the UTC timestamps
    stored in the ledger.

    Args:
        conn: SQLite connection.

    Returns:
        Dict with keys: tz, estimated (True — cost is an estimate), today, month.
        Each period dict has keys: input, output, cost, calls, by_model.
    """
    now_local = datetime.now(clock.app_tz())
    day_local = now_local.replace(hour=0, minute=0, second=0, microsecond=0)
    month_local = day_local.replace(day=1)

    def _utc(dt: datetime) -> str:
        """Convert a local datetime to a UTC string for the ledger query."""
        return dt.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")

    return {"tz": clock.app_tz_name(), "estimated": True,
            "today": _agg(conn, _utc(day_local)), "month": _agg(conn, _utc(month_local))}
