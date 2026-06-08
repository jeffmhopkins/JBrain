"""Deterministic, geometry-aware parsing of lab-result PDFs — NO LLM.

The motivating case is a "Result Trends" export: a TRANSPOSED matrix where each row is
an analyte and each column is a collection date (many dates per page, several tables per
document). Linearised text (pypdf) loses the column->date alignment and detaches the
High/Low flags, so we parse with pdfplumber word COORDINATES instead:

  * date columns are anchored by the x of each date token in the "Component …" header,
  * each analyte is anchored on its "Normal Range:" line (giving unit + reference range),
    with its name taken from the left-column text just above it,
  * every numeric value cell is mapped to (analyte by its y-band, date by nearest x-anchor)
    — which reassembles values even when one analyte is printed as two sub-rows (the lab
    changed its range formatting mid-history) or when cells are missing.

Flags are NOT read from the page (they're detached and unreliable); callers recompute them
from value vs reference range. Everything here is pure: bytes in, rows out, no DB, no LLM.
"""
from __future__ import annotations

import bisect
import io
import re

_MONTHS = {m: i for i, m in enumerate(
    ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"], start=1)}

_DATE_RE = re.compile(r"\b([A-Z][a-z]{2}) (\d{1,2}), (\d{4})\b")
# A per-table caption line ('… (Table 2 of 7)'): its index digit must not be read as a value.
_TABLE_CAPTION_RE = re.compile(r"\(Table \d+ of \d+\)")
# A value cell: an optional inequality, then a number (the unit is a separate word/source).
# Up to 6 integer digits so raw counts (e.g. 2942 cells/uL) parse, not just thousands.
_VALUE_RE = re.compile(r"^([<>]=?)?(\d{1,6}(?:\.\d+)?)$")
_RANGE_RE = re.compile(
    r"Normal Range:\s*(?:Less than\s*)?(?:<=|>=)?\s*"
    r"(?:(\d+\.?\d*)\s*-\s*)?(\d+\.?\d*)?\s*(.*)$")

# Canonical analyte keys for the common CBC panel (relative '%'/absolute counts kept distinct).
_ANALYTE_MAP = {
    "wbc": "wbc", "auto wbc": "wbc", "white blood cell": "wbc",
    "platelets": "platelets", "platelet": "platelets", "plt": "platelets",
    "rbc": "rbc", "red blood cell": "rbc",
    "hemoglobin": "hemoglobin", "hgb": "hemoglobin",
    "hematocrit": "hematocrit", "hct": "hematocrit",
    "mcv": "mcv", "mch": "mch", "mchc": "mchc",
    "rdw-sd": "rdw_sd", "rdw": "rdw", "mpv": "mpv",
    "neutrophils relative": "neutrophils_pct", "neutrophils absolute": "neutrophils_abs",
    "lymphocytes relative": "lymphocytes_pct", "lymphocytes absolute": "lymphocytes_abs",
    "monocytes relative": "monocytes_pct", "monocytes absolute": "monocytes_abs",
    "eosinophils relative": "eosinophils_pct", "eosinophils absolute": "eosinophils_abs",
    "basophils relative": "basophils_pct", "basophils absolute": "basophils_abs",
    "immature gran relative": "immature_gran_pct", "imm gran absolute": "immature_gran_abs",
    "immature granulocytes relative": "immature_gran_pct",
    "nrbc": "nrbc", "nrbc absolute": "nrbc_abs", "nucleated rbc": "nrbc",
    # Quest/PWNHealth consumer naming for the same CBC analytes.
    "mean rbc volume": "mcv", "mean rbc iron": "mch", "mean rbc iron concentration": "mchc",
    "rbc distribution width": "rdw", "rbc distribution width sd": "rdw_sd",
    "absolute neutrophils": "neutrophils_abs", "absolute lymphocytes": "lymphocytes_abs",
    "absolute monocytes": "monocytes_abs", "absolute eosinophils": "eosinophils_abs",
    "absolute basophils": "basophils_abs",
}


def analyte_key(name: str) -> str:
    """Normalize a printed analyte name to a stable trend-grouping slug.

    Known CBC names map to a canonical key; anything else falls back to a conservative slug
    so trends still group exact-name repeats.

    Args:
        name: Raw analyte name as printed in the lab document.

    Returns:
        Stable slug string suitable for grouping trends.
    """
    n = re.sub(r"\s+", " ", (name or "").strip().lower())
    n = re.sub(r"^auto\s+", "", n)
    n = re.sub(r"\s+count$", "", n)                   # "white blood cell count" -> "… cell"
    if n in _ANALYTE_MAP:
        return _ANALYTE_MAP[n]
    return re.sub(r"[^a-z0-9]+", "_", n).strip("_")


def _iso(year: int, mon: int, day: int) -> str | None:
    """Return an ISO date string, or None for an impossible calendar date.

    Uses datetime so day-for-month (and leap years) are validated — '2025-02-30' must never
    reach the DB, where date() would silently normalize it to a DIFFERENT day than raw-string
    ORDER BY sees.

    Args:
        year: Four-digit year.
        mon: Month (1–12).
        day: Day of month.

    Returns:
        ISO-8601 date string (YYYY-MM-DD), or None if the date is invalid.
    """
    import datetime
    try:
        return datetime.date(int(year), int(mon), int(day)).isoformat()
    except (ValueError, TypeError):
        return None


def parse_date(text: str) -> str | None:
    """Parse a textual date like 'Jul 3, 2022' to ISO format '2022-07-03', or None.

    The textual month name is unambiguous and avoids MM/DD vs DD/MM confusion.

    Args:
        text: String potentially containing a date in 'Mon DD, YYYY' format.

    Returns:
        ISO-8601 date string, or None if no recognizable date is found.
    """
    m = _DATE_RE.search(text or "")
    if not m:
        return None
    mon = _MONTHS.get(m.group(1))
    if not mon:
        return None
    return _iso(int(m.group(3)), mon, int(m.group(2)))


def mdy_to_iso(a: int, b: int, year: int) -> str | None:
    """Resolve a numeric 'a/b/year' date to ISO without blindly trusting MM/DD order.

    If the first field is >12 it can only be a day (DD/MM); if the second is >12 it can only
    be a day (MM/DD); otherwise default to US MM/DD. Returns None for impossible dates
    (validated as a real calendar date) rather than emitting a malformed string the DB will
    silently shift.

    Args:
        a: First numeric field of the date pair.
        b: Second numeric field of the date pair.
        year: Four-digit year.

    Returns:
        ISO-8601 date string, or None if the date is invalid.
    """
    if a > 12 and b <= 12:           # DD/MM
        day, mon = a, b
    else:                            # MM/DD, or ambiguous (both <=12) -> assume US MM/DD
        mon, day = a, b
    return _iso(year, mon, day)


def normalize_dob(raw: str) -> str | None:
    """Normalize a human-typed or printed DOB to a validated ISO date, or None.

    Handles 'Jul 4, 1990', '07/04/1990', and '1990-07-04'. Used to compare a document's
    patient DOB against the configured owner DOB on a common footing so mere format drift
    never fabricates a 'different patient' mismatch.

    Args:
        raw: Raw date-of-birth string in any supported format.

    Returns:
        ISO-8601 date string, or None if the input is empty or unrecognizable.
    """
    raw = (raw or "").strip()
    if not raw:
        return None
    m = re.match(r"^(\d{4})-(\d{1,2})-(\d{1,2})$", raw)
    if m:
        return _iso(int(m.group(1)), int(m.group(2)), int(m.group(3)))
    m = re.match(r"^(\d{1,2})/(\d{1,2})/(\d{4})$", raw)
    if m:
        return mdy_to_iso(int(m.group(1)), int(m.group(2)), int(m.group(3)))
    return parse_date(raw)


_DOB_RE = re.compile(r"(?:Date of Birth|DOB)\s*:?\s*"
                     r"([A-Z][a-z]{2}\s+\d{1,2},\s*\d{4}|\d{1,2}/\d{1,2}/\d{4})")


def parse_identity(text: str) -> dict:
    """Extract the document's patient identity (name + DOB) on a best-effort basis.

    Lets the reviewer catch a wrong-patient upload before it merges into the single owner's
    trends (P1). Pure and tolerant: returns {} when nothing recognizable is found — never
    guesses.

    Args:
        text: Full extracted text of the lab document.

    Returns:
        Dict with optional keys 'name' and 'dob' (ISO date), or {} if nothing was found.
    """
    out: dict = {}
    m = _DOB_RE.search(text or "")
    if m:
        iso = normalize_dob(m.group(1))
        if iso:                          # only a VALID ISO DOB is kept — never a raw/garbage
            out["dob"] = iso             # string that would force a false mismatch downstream
        # Name: text BEFORE 'Date of Birth'/'DOB' on the same line (CBC), else the non-empty
        # line just above (Quest 'LAST,FIRST' on its own line). Kept short and letter-bearing.
        line = next((ln for ln in (text or "").splitlines() if m.group(0) in ln), "")
        before = line[:line.find(m.group(0))].strip(" \t:-")
        cand = before
        if not cand:
            lines = [ln.strip() for ln in (text or "").splitlines()]
            try:
                idx = next(i for i, ln in enumerate(lines) if m.group(0) in ln)
                cand = next((lines[j] for j in range(idx - 1, -1, -1) if lines[j]), "")
            except StopIteration:
                cand = ""
        cand = cand.strip()
        if cand and re.search(r"[A-Za-z]", cand) and len(cand) <= 60:
            out["name"] = cand
    return out


def parse_range(text: str) -> tuple[float | None, float | None, str | None, str | None]:
    """Parse a 'Normal Range: …' left-column string into (low, high, unit, raw_text).

    Handles '3.90 - 11.20 thou/cumm', '0 - 13 %', '0.0 - 0.2 /100 WBC', and one-sided
    'Less than <=0.40 thou/cumm' (low=None, high=0.40).

    Args:
        text: Raw range string as found in the left column of the lab document.

    Returns:
        Tuple of (low, high, unit, raw_text); any element may be None if not present.
    """
    i = text.find("Normal Range:")
    raw = text[i:].strip() if i >= 0 else text.strip()
    m = _RANGE_RE.search(raw)
    if not m:
        return None, None, None, (raw or None)
    low = float(m.group(1)) if m.group(1) else None
    high = float(m.group(2)) if m.group(2) else None
    unit = re.sub(r"\s+", " ", (m.group(3) or "").strip()) or None
    ref_text = re.sub(r"^Normal Range:\s*", "", raw).strip() or None
    return low, high, unit, ref_text


def _is_unit(tok: str) -> bool:
    """Return True if tok is a unit token following a numeric value.

    Examples of recognized units: 'mg/dL', 'thou/cumm', '%', 'U/L', '/100'.

    Args:
        tok: Token string to test.

    Returns:
        True if the token looks like a unit, False otherwise.
    """
    if not tok or len(tok) > 12:
        return False
    if _VALUE_RE.match(tok):
        return False
    return bool(re.search(r"[A-Za-z%]", tok)) or tok == "/100"


def _value(tok: str) -> tuple[str, float | None] | None:
    """Parse a numeric value cell token into (value_text, value_num).

    Inequalities like '<0.01' keep the text but return None for value_num so trend math
    ignores them rather than guessing.

    Args:
        tok: Token string from a value cell.

    Returns:
        Tuple of (value_text, value_num), or None if the token is not a numeric value.
    """
    m = _VALUE_RE.match(tok)
    if not m:
        return None
    if m.group(1):
        return tok, None
    return tok, float(m.group(2))


def is_faithful(value_text: str, text: str) -> bool:
    """Check word-boundary faithfulness (F1): the value must appear in the document as its own token.

    The value must not be a substring of a longer number ('12' must not match inside '120' or
    '12.5'). Bounded by non-digit/non-decimal on both sides; a leading inequality ('<0.01') is
    fine. For an OCR corpus we also accept a numerically-equal form (OCR '15' vs model '15.0',
    or '1,234' vs '1234') so a trailing-zero/separator difference doesn't drop a real value —
    without ever accepting a DIFFERENT number.

    Args:
        value_text: Value string to verify (e.g. '12.5' or '<0.01').
        text: Faithfulness corpus (visible words extracted from the document).

    Returns:
        True if the value appears faithfully in the corpus, or if the corpus is empty.
    """
    if not text:
        return True                                   # no corpus to check against
    if not value_text:
        return False
    if re.search(r"(?<![\d.])" + re.escape(value_text) + r"(?![\d.])", text):
        return True
    # Numeric-equality fallback: same value written differently (trailing .0, thousands commas).
    m = re.match(r"^[<>]=?(\d.*)$", value_text) or re.match(r"^(\d.*)$", value_text)
    if not m:
        return False
    try:
        target = float(m.group(1).replace(",", ""))
    except ValueError:
        return False
    for tok in re.findall(r"\d[\d,]*(?:\.\d+)?", text):
        try:
            if float(tok.replace(",", "")) == target:
                return True
        except ValueError:
            continue
    return False


def _lines(words: list[dict]) -> list[list[dict]]:
    """Group words into visual lines by rounded baseline, each sorted left-to-right.

    Args:
        words: List of pdfplumber word dicts (each has 'top', 'x0', 'text').

    Returns:
        List of lines, each a list of word dicts sorted by x0.
    """
    rows: dict[int, list[dict]] = {}
    for w in words:
        rows.setdefault(round(w["top"] / 2.0), []).append(w)
    return [sorted(rows[k], key=lambda w: w["x0"]) for k in sorted(rows)]


def _parse_page(words: list[dict]) -> list[dict]:
    """Extract result rows from one page's words.

    A page may hold several stacked tables, each re-introduced by a 'Component <date> <date>
    …' header that resets the columns.

    Two passes: (1) downward, record each analyte ENTRY (anchored on its 'Normal Range:' line,
    named from the left-column line just above it) and every VALUE cell (mapped to a date by
    nearest column x); (2) assign each value to the nearest analyte name above it. This is
    robust to wrapped unit lines and to one analyte printed as two range-variant sub-rows (both
    share a name -> merge by analyte_key later).

    Args:
        words: List of pdfplumber word dicts for a single page.

    Returns:
        List of result row dicts, each with test_name, analyte_key, value_text, value_num,
        unit, ref_low, ref_high, ref_text, collected_at, collected_time, plus internal
        _name_margin and _date_margin confidence signals.
    """
    lines = _lines(words)
    entries: list[dict] = []       # {top, name, range_text}
    cells: list[dict] = []         # {top, date, vt, vn}
    anchors: list[float] = []      # x of each active date column
    dates: list[str] = []          # ISO date per active column
    left_max = 1e9                 # x boundary: left of it = label/range, right = value cells
    last_name: tuple[float, str] | None = None   # nearest candidate analyte-name line above
    cur: dict | None = None        # entry still accepting a wrapped range tail

    for ln in lines:
        text = " ".join(w["text"] for w in ln)
        # Each table is introduced by a caption like 'Jan 6, 2025 - Feb 3, 2026 (Table 2 of 7)'.
        # Skip it entirely: its index digit ('Table 2 of 7') sits in the first date column's
        # x-band and would otherwise bind to the topmost analyte (Auto WBC) as a bogus value.
        if _TABLE_CAPTION_RE.search(text):
            continue
        # A new table header resets the column anchors/dates (and the name cursor) below it.
        if ln and ln[0]["text"] == "Component" and _DATE_RE.search(text):
            anchors = [w["x0"] for w in ln[1:] if w["text"][:3] in _MONTHS]
            dates = [f"{int(y):04d}-{_MONTHS[mo]:02d}-{int(d):02d}"
                     for mo, d, y in _DATE_RE.findall(text)]
            n = min(len(anchors), len(dates))
            anchors, dates = anchors[:n], dates[:n]
            left_max = (min(anchors) - 22) if anchors else 1e9
            last_name, cur = None, None
            continue
        if not anchors:
            continue
        left = [w for w in ln if w["x0"] < left_max]
        left_text = " ".join(w["text"] for w in left)
        has_values = any(w["x0"] >= left_max and _value(w["text"]) for w in ln)
        if "Normal Range:" in text:
            top, name = (last_name if last_name else (ln[0]["top"], ""))
            cur = {"top": top, "name": name, "range_text": left_text}
            entries.append(cur)
        elif cur is not None and not has_values and re.match(r"^[<>]?\d", left_text):
            # A wrapped range tail ('11.20 thou/cumm', '440 thou/cumm', '0.2 /100 WBC')
            # always starts with the HIGH number — fold it back into the open range. The
            # tail can sit BELOW the value row, so the entry stays open across values until
            # the next name/range line closes it.
            cur["range_text"] += " " + left_text
        elif left_text and re.search(r"[A-Za-z]", left_text) and not re.match(r"^[<>]?\d", left_text):
            # A left-column name line (not a digit-leading range tail): the next analyte.
            last_name, cur = (ln[0]["top"], left_text), None
        # Record every value cell on this line (date = nearest column x-anchor). The unit is
        # taken from the token right after the value ('9.2 mg/dL') — more reliable than the
        # left-column range text, where it's often absent or split ('U/' vs 'U/L').
        for idx, w in enumerate(ln):
            if w["x0"] < left_max:
                continue
            v = _value(w["text"])
            if not v:
                continue
            dd = sorted(abs(w["x0"] - a) for a in anchors)
            j = min(range(len(anchors)), key=lambda k: abs(w["x0"] - anchors[k]))
            if dd[0] > 60:                                    # outside any column band
                continue
            date_margin = (dd[1] - dd[0]) if len(dd) > 1 else 999.0  # how clear the column win was
            unit = None
            if idx + 1 < len(ln) and _is_unit(ln[idx + 1]["text"]):
                unit = ln[idx + 1]["text"]
                if unit == "/100" and idx + 2 < len(ln):
                    unit = "/100 " + ln[idx + 2]["text"]
            cells.append({"top": w["top"], "date": dates[j], "vt": v[0], "vn": v[1], "unit": unit,
                          "date_margin": date_margin})

    for e in entries:
        e["low"], e["high"], e["unit"], e["ref_text"] = parse_range(e["range_text"])
    entries.sort(key=lambda e: e["top"])
    tops = [e["top"] for e in entries]
    out: list[dict] = []
    for c in cells:
        # Assign each value to the NEAREST analyte name by vertical distance (above OR below).
        # Labs vertically-center a row's value cells, so a value can sit a few px ABOVE its
        # own name (e.g. BUN/Creatinine Ratio's values rendered just above its label) — a
        # strict "nearest name above" rule would mis-bind them to the previous analyte.
        i = bisect.bisect_left(tops, c["top"])
        cand = []
        if i > 0:
            cand.append(entries[i - 1])                       # nearest at/above (wins ties)
        if i < len(entries):
            cand.append(entries[i])                           # nearest below
        if not cand:
            continue
        e = min(cand, key=lambda en: abs(en["top"] - c["top"]))
        dists = sorted(abs(en["top"] - c["top"]) for en in cand)
        name_margin = (dists[1] - dists[0]) if len(dists) > 1 else 999.0  # clarity of the name win
        out.append({
            "test_name": e["name"], "analyte_key": analyte_key(e["name"]),
            "value_text": c["vt"], "value_num": c["vn"], "unit": c["unit"] or e["unit"],
            "ref_low": e["low"], "ref_high": e["high"], "ref_text": e["ref_text"],
            "collected_at": c["date"], "collected_time": None,   # trend columns are date-only (P4)
            "_name_margin": name_margin, "_date_margin": c["date_margin"],
        })
    return out


_COLLECTED_RE = re.compile(r"Collected(?:\s+Date)?:\s*(\d{1,2})/(\d{1,2})/(\d{4})(?:\s+(\d{1,2}):(\d{2}))?")
_DRANGE_RE = re.compile(r"(?:Desired|Reference)\s+Range:\s*(.*)$")
_SPAN_RE = re.compile(r"([\d.]+)\s*-\s*([\d.]+)\s*(\S.*)?$")
_ONESIDED_RE = re.compile(r"(?:Less than\s*)?([<>])=?\s*([\d.]+)\s*(\S.*)?$")


def _report_range(s: str) -> tuple[float | None, float | None, str | None, str | None]:
    """Parse a single-report range string into (low, high, unit, raw).

    Handles a two-sided 'LOW - HIGH UNIT' and a one-sided '<0.4 BEU' / '>5 x' (the
    specialty-test form).

    Args:
        s: Raw range string from a Desired/Reference Range line.

    Returns:
        Tuple of (low, high, unit, raw); any element may be None.
    """
    s = (s or "").strip()
    m = _SPAN_RE.match(s)
    if m:
        return (float(m.group(1)), float(m.group(2)),
                (m.group(3).strip() if m.group(3) else None), s or None)
    m = _ONESIDED_RE.match(s)
    if m:
        val = float(m.group(2))
        unit = m.group(3).strip() if m.group(3) else None
        return (None, val, unit, s) if m.group(1) == "<" else (val, None, unit, s)
    return None, None, None, (s or None)


def _parse_single_report(pages: list[list[dict]], collected: str | None,
                         collected_time: str | None = None) -> list[dict]:
    """Parse a single-encounter report (Quest/PWNHealth + specialty single-analyte reports).

    Each analyte is a 'Name … value [flag]' row plus a 'Desired/Reference Range:' line; the
    range sits just BELOW its value row (standard CBC) or ABOVE it (specialty). We anchor on
    the report's own value-column header ('Result' / 'Value') — its x gives the value column
    and its y separates the patient/address METADATA above from the data rows below, so an
    address zip can't masquerade as a value. Each range binds to the nearest data row ABOVE it
    (the standard case), falling back BELOW (specialty). One collection date for the whole
    report.

    Args:
        pages: Per-page lists of pdfplumber word dicts.
        collected: ISO collection date for the whole report, or None.
        collected_time: HH:MM collection time string, or None.

    Returns:
        List of result row dicts in the lab_parse row schema.
    """
    out: list[dict] = []
    for words in pages:
        lines = _lines(words)
        # The value column + the metadata/data divider, from the report's own header.
        valcol, header_y = 430.0, -1e9
        for ln in lines:
            hdr = [w for w in ln if w["text"] in ("Result", "Value")]
            if hdr and len(ln) <= 4:
                valcol, header_y = max(w["x0"] for w in hdr), ln[0]["top"]
                break
        names: list[tuple[float, str]] = []                     # (y, name)
        values: list[tuple[float, str, float | None]] = []      # (y, value_text, value_num) in the value column
        ranges: list[tuple[float, str]] = []                    # (y, range_text)
        for ln in lines:
            if ln[0]["top"] <= header_y:                        # above the header = metadata/address
                continue
            text = " ".join(w["text"] for w in ln)
            rm = _DRANGE_RE.search(text)
            if rm:
                ranges.append((ln[0]["top"], rm.group(1).strip()))
                continue
            if ":" in text:                                     # skip any footer/metadata label line
                continue
            # name = alphabetic tokens LEFT of the value column (so a trailing LOW/HIGH flag,
            # printed further right, is excluded). Name and value can render on slightly
            # different y-lines (a ±1px split), so they're collected SEPARATELY and matched by
            # proximity below — never required on the same line.
            name = " ".join(w["text"] for w in ln if w["x0"] < valcol - 40 and re.search(r"[A-Za-z]", w["text"]))
            if name:
                names.append((ln[0]["top"], name))
            for w in ln:
                if w["x0"] >= valcol - 40 and _value(w["text"]):
                    values.append((w["top"], *_value(w["text"])))
        for ry, rtext in ranges:
            # Bind to the nearest name that HAS a colocated value, preferring above (standard:
            # range sits below its value) then below (specialty: range above its value).
            above = sorted((n for n in names if n[0] < ry), key=lambda n: -n[0])
            below = sorted((n for n in names if n[0] >= ry), key=lambda n: n[0])
            for ny, nm in [*above, *below]:
                vs = [v for v in values if abs(v[0] - ny) < 14]
                if not vs:
                    continue
                lo, hi, unit, rawr = _report_range(rtext)
                out.append({
                    "test_name": nm, "analyte_key": analyte_key(nm),
                    "value_text": vs[0][1], "value_num": vs[0][2], "unit": unit,
                    "ref_low": lo, "ref_high": hi, "ref_text": rawr,
                    "collected_at": collected, "collected_time": collected_time,
                })
                break
    return out


def _backfill(results: list[dict]) -> None:
    """Make each analyte's unit/range consistent across its rows. Units don't vary within an
    analyte, so adopt the modal unit everywhere (fixes per-table wrap quirks like 'U/'). For
    the reference range, only FILL nulls (the range can legitimately change over time, so a
    row that parsed its own range keeps it).

    Magnitude guard (F5): never stamp the modal unit onto a row whose value is orders of
    magnitude off the analyte's median — a raw count (7200) wearing a 'thou/cumm' unit would
    co-plot as a unit-consistent 1000x spike. Such rows keep their own unit and are tagged
    `_magnitude_outlier` so the confidence pass can flag them instead of silently merging."""
    import collections, statistics
    by_key: dict[str, list[dict]] = collections.defaultdict(list)
    for r in results:
        by_key[r["analyte_key"]].append(r)
    for rows in by_key.values():
        units = collections.Counter(r["unit"] for r in rows if r["unit"])
        modal_unit = units.most_common(1)[0][0] if units else None
        for col in ("ref_low", "ref_high"):
            vals = collections.Counter(r[col] for r in rows if r[col] is not None)
            modal = vals.most_common(1)[0][0] if vals else None
            for r in rows:
                if r[col] is None:
                    r[col] = modal
        nums = [r["value_num"] for r in rows if r["value_num"] not in (None, 0)]
        median = statistics.median(nums) if nums else None
        for r in rows:
            v = r["value_num"]
            # >500x separates a 10^3 unit error (e.g. 7200 among 7.x 'thou/cumm' rows) from real
            # biological spread (even platelets 5 -> 900 is only ~180x); only THAT gets guarded.
            outlier = (median is not None and v not in (None, 0)
                       and (v > median * 500 or v < median / 500))
            r["_magnitude_outlier"] = bool(outlier)
            if modal_unit and not outlier:
                r["unit"] = modal_unit


def _score_confidence(results: list[dict]) -> None:
    """Replace the old hard-coded confidence=1.0 with a PER-ROW trust score (F3). A value can
    satisfy the verbatim faithfulness check and still be wrong — bound to the wrong analyte or
    date column, or off by an order of magnitude. We fold the geometry bind margins captured
    during parsing together with a range-plausibility check into {high, medium, low} + reasons,
    so the human reviewer's attention lands on the rows that need it. Pure heuristic; never
    drops a row, only flags it. Margins are in PDF points (lines ~14pt, columns ~50-80px)."""
    rank = {"high": 2, "medium": 1, "low": 0}
    for r in results:
        conf, reasons = "high", []

        def demote(level: str, why: str):
            nonlocal conf
            if rank[level] < rank[conf]:
                conf = level
            reasons.append(why)

        # Geometry bind margins are the RELIABLE signals — they catch the parser's actual
        # failure modes (value bound to the wrong analyte / wrong date column). We deliberately
        # do NOT demote on reference-range plausibility: a sick patient legitimately produces
        # order-of-magnitude-abnormal labs (platelets of 5, nRBC of 60), so range-based flagging
        # would bury exactly the critical values a reviewer most needs to trust. That kind of
        # misread (a dropped decimal) is an OCR-path risk, caught there by the cross-read.
        nm = r.pop("_name_margin", None)      # how much nearer the chosen analyte was than runner-up
        dm = r.pop("_date_margin", None)      # how much nearer the chosen date column was than runner-up
        if nm is not None and nm < 4:
            demote("low", "ambiguous analyte bind")
        elif nm is not None and nm < 10:
            demote("medium", "near analyte bind")
        if dm is not None and dm < 6:
            demote("low", "ambiguous date column")
        elif dm is not None and dm < 12:
            demote("medium", "near date column")
        # The magnitude guard fires only on the unmistakable 10^3 unit-corruption signature
        # (>500x off the analyte median), well clear of biological spread — see _backfill.
        if r.pop("_magnitude_outlier", False):
            demote("low", "likely unit/scale error (far from analyte median)")
        if r.get("collected_at") is None:
            demote("low", "no collection date")
        r["confidence"] = conf
        r["confidence_reasons"] = reasons


def parse_lab_pdf(pdf_bytes: bytes) -> dict:
    """Parse a lab-result PDF into structured rows. Returns
    {doc_type, confidence, results:[…], pages}. doc_type is 'lab_trend_export' when the
    trend-matrix structure is found, else 'unknown' (caller skips it)."""
    import pdfplumber
    results: list[dict] = []
    pages = 0
    full_text = ""
    page_words: list[list[dict]] = []
    with pdfplumber.open(io.BytesIO(pdf_bytes)) as pdf:
        for page in pdf.pages:
            pages += 1
            full_text += (page.extract_text() or "") + "\n"
            try:
                page_words.append(page.extract_words())
            except Exception:  # noqa: BLE001 — a bad page shouldn't sink the whole parse
                page_words.append([])
    # The faithfulness corpus is the VISIBLE words only (P2): extract_text can include hidden /
    # white-on-white text, which an attacker could use to smuggle a value past a verbatim check.
    # extract_words returns laid-out (visible) tokens, so a value matched against THIS cannot be
    # satisfied by invisible injected text.
    visible_text = " ".join(w["text"] for words in page_words for w in words)

    is_trend = "Result Trends" in full_text or "Normal Range:" in full_text
    is_report = ("Desired Range:" in full_text or "Reference Range:" in full_text)
    doc_type = "unknown"
    if is_trend:
        for words in page_words:
            results.extend(_parse_page(words))
        doc_type = "lab_trend_export"
    elif is_report:
        cm = _COLLECTED_RE.search(full_text)
        collected = mdy_to_iso(int(cm.group(1)), int(cm.group(2)), int(cm.group(3))) if cm else None
        ctime = f"{int(cm.group(4)):02d}:{cm.group(5)}" if (cm and cm.group(4)) else None
        results = _parse_single_report(page_words, collected, ctime)
        doc_type = "lab_report"
    _backfill(results)
    _score_confidence(results)
    # De-dupe within the document (the same date column can repeat across overlapping tables).
    seen: set = set()
    uniq: list[dict] = []
    for r in results:
        k = (r["analyte_key"], r["collected_at"], r["value_text"])
        if k in seen:
            continue
        seen.add(k)
        uniq.append(r)
    if not uniq:
        doc_type = "unknown"
    confidence = 1.0 if doc_type != "unknown" else 0.0
    return {"doc_type": doc_type, "confidence": confidence, "results": uniq, "pages": pages,
            "identity": parse_identity(full_text), "visible_text": visible_text}
