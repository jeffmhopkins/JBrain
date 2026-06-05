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
    """Normalize a printed analyte name to a stable trend-grouping slug. Known CBC names
    map to a canonical key; anything else falls back to a conservative slug so trends still
    group exact-name repeats."""
    n = re.sub(r"\s+", " ", (name or "").strip().lower())
    n = re.sub(r"^auto\s+", "", n)
    n = re.sub(r"\s+count$", "", n)                   # "white blood cell count" -> "… cell"
    if n in _ANALYTE_MAP:
        return _ANALYTE_MAP[n]
    return re.sub(r"[^a-z0-9]+", "_", n).strip("_")


def parse_date(text: str) -> str | None:
    """'Jul 3, 2022' -> '2022-07-03' (ISO date), or None."""
    m = _DATE_RE.search(text or "")
    if not m:
        return None
    mon = _MONTHS.get(m.group(1))
    if not mon:
        return None
    return f"{int(m.group(3)):04d}-{mon:02d}-{int(m.group(2)):02d}"


def parse_range(text: str) -> tuple[float | None, float | None, str | None, str | None]:
    """Parse a 'Normal Range: …' left-column string into (low, high, unit, raw_text).

    Handles '3.90 - 11.20 thou/cumm', '0 - 13 %', '0.0 - 0.2 /100 WBC', and one-sided
    'Less than <=0.40 thou/cumm' (low=None, high=0.40)."""
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
    """A unit token following a value ('mg/dL', 'thou/cumm', '%', 'U/L', '/100')."""
    if not tok or len(tok) > 12:
        return False
    if _VALUE_RE.match(tok):
        return False
    return bool(re.search(r"[A-Za-z%]", tok)) or tok == "/100"


def _value(tok: str) -> tuple[str, float | None] | None:
    """A numeric value cell -> (value_text, value_num). Inequalities ('<0.01') keep the
    text but have no numeric value (so trend math ignores them rather than guessing)."""
    m = _VALUE_RE.match(tok)
    if not m:
        return None
    if m.group(1):
        return tok, None
    return tok, float(m.group(2))


def _lines(words: list[dict]) -> list[list[dict]]:
    """Group words into visual lines by rounded baseline, each sorted left-to-right."""
    rows: dict[int, list[dict]] = {}
    for w in words:
        rows.setdefault(round(w["top"] / 2.0), []).append(w)
    return [sorted(rows[k], key=lambda w: w["x0"]) for k in sorted(rows)]


def _parse_page(words: list[dict]) -> list[dict]:
    """Extract result rows from one page's words. A page may hold several stacked tables,
    each re-introduced by a 'Component <date> <date> …' header that resets the columns.

    Two passes: (1) downward, record each analyte ENTRY (anchored on its 'Normal Range:'
    line, named from the left-column line just above it) and every VALUE cell (mapped to a
    date by nearest column x); (2) assign each value to the nearest analyte name above it.
    This is robust to wrapped unit lines and to one analyte printed as two range-variant
    sub-rows (both share a name -> merge by analyte_key later)."""
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
            j = min(range(len(anchors)), key=lambda k: abs(w["x0"] - anchors[k]))
            if abs(w["x0"] - anchors[j]) > 60:                # outside any column band
                continue
            unit = None
            if idx + 1 < len(ln) and _is_unit(ln[idx + 1]["text"]):
                unit = ln[idx + 1]["text"]
                if unit == "/100" and idx + 2 < len(ln):
                    unit = "/100 " + ln[idx + 2]["text"]
            cells.append({"top": w["top"], "date": dates[j], "vt": v[0], "vn": v[1], "unit": unit})

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
        out.append({
            "test_name": e["name"], "analyte_key": analyte_key(e["name"]),
            "value_text": c["vt"], "value_num": c["vn"], "unit": c["unit"] or e["unit"],
            "ref_low": e["low"], "ref_high": e["high"], "ref_text": e["ref_text"],
            "collected_at": c["date"],
        })
    return out


_COLLECTED_RE = re.compile(r"Collected Date:\s*(\d{1,2})/(\d{1,2})/(\d{4})")
_DRANGE_RE = re.compile(r"(?:Desired|Reference)\s+Range:\s*(.*)$")
_SPAN_RE = re.compile(r"([\d.]+)\s*-\s*([\d.]+)\s*(\S.*)?$")


def _parse_single_report(pages: list[list[dict]], collected: str | None) -> list[dict]:
    """Single-encounter report (e.g. Quest/PWNHealth): a vertical list where each analyte is
    a 'Name … value' line followed by a 'Desired Range: LOW-HIGH UNIT' line, all sharing one
    collection date. Name+value are split by x (value is the far-right column)."""
    out: list[dict] = []
    for words in pages:
        names: list[tuple[float, str]] = []                  # (y, analyte name)
        values: list[tuple[float, str, float | None]] = []   # (y, value_text, value_num)
        for ln in _lines(words):
            text = " ".join(w["text"] for w in ln)
            if _DRANGE_RE.search(text):
                # The reference range anchors an analyte: name = nearest name line above,
                # value = the value token nearest that name (robust to ±1px line splits).
                m = _DRANGE_RE.search(text)
                y = ln[0]["top"]
                above = [n for n in names if n[0] < y]
                if not above:
                    continue
                ny, nm = above[-1]
                vs = [v for v in values if abs(v[0] - ny) < 14]
                vt, vn = (vs[0][1], vs[0][2]) if vs else (None, None)
                sm = _SPAN_RE.match(m.group(1).strip())
                out.append({
                    "test_name": nm, "analyte_key": analyte_key(nm),
                    "value_text": vt, "value_num": vn,
                    "unit": (sm.group(3).strip() if sm and sm.group(3) else None),
                    "ref_low": float(sm.group(1)) if sm else None,
                    "ref_high": float(sm.group(2)) if sm else None,
                    "ref_text": m.group(1).strip() or None, "collected_at": collected,
                })
                continue
            nm = " ".join(w["text"] for w in ln if w["x0"] <= 430 and re.search(r"[A-Za-z]", w["text"]))
            if nm and "Desired" not in nm and "Reference" not in nm:
                names.append((ln[0]["top"], nm))
            for w in ln:
                if w["x0"] > 430 and _value(w["text"]):
                    values.append((w["top"], *_value(w["text"])))
    return out


def _backfill(results: list[dict]) -> None:
    """Make each analyte's unit/range consistent across its rows. Units don't vary within an
    analyte, so adopt the modal unit everywhere (fixes per-table wrap quirks like 'U/'). For
    the reference range, only FILL nulls (the range can legitimately change over time, so a
    row that parsed its own range keeps it)."""
    import collections
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
        if modal_unit:
            for r in rows:
                r["unit"] = modal_unit


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

    is_trend = "Result Trends" in full_text or "Normal Range:" in full_text
    is_report = ("Desired Range:" in full_text or "Reference Range:" in full_text)
    doc_type = "unknown"
    if is_trend:
        for words in page_words:
            results.extend(_parse_page(words))
        doc_type = "lab_trend_export"
    elif is_report:
        cm = _COLLECTED_RE.search(full_text)
        collected = f"{int(cm.group(3)):04d}-{int(cm.group(1)):02d}-{int(cm.group(2)):02d}" if cm else None
        results = _parse_single_report(page_words, collected)
        doc_type = "lab_report"
    _backfill(results)
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
    return {"doc_type": doc_type, "confidence": confidence, "results": uniq, "pages": pages}
