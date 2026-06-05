"""Deterministic lab-PDF parser tests. The geometry layer is exercised with synthetic word
boxes (no PDF, no PHI) that reproduce the real-world quirks: values mapped to date columns by
x, a wrapped high/unit line, a missing cell, and one analyte printed as two range-variant
sub-rows that must merge by analyte_key."""
from app.services import lab_parse


def W(text, x, top):
    return {"text": text, "x0": x, "top": top}


def line(top, *toks):
    return [W(t, x, top) for t, x in toks]


def _by(rows, key):
    return sorted((r["collected_at"], r["value_text"], r["unit"], r["ref_low"], r["ref_high"])
                  for r in rows if r["analyte_key"] == key)


def test_parse_pure_helpers():
    assert lab_parse.parse_date("Jul 3, 2022") == "2022-07-03"
    assert lab_parse.analyte_key("Auto WBC") == "wbc"
    assert lab_parse.analyte_key("White Blood Cell Count") == "wbc"
    assert lab_parse.analyte_key("Absolute Neutrophils") == "neutrophils_abs"
    assert lab_parse._value("6.20") == ("6.20", 6.2)
    assert lab_parse._value("2942") == ("2942", 2942.0)        # 4-digit count, not just thousands
    assert lab_parse._value("<0.01") == ("<0.01", None)        # inequality keeps text, no number
    assert lab_parse.parse_range("Normal Range: 3.90 - 11.20 thou/cumm")[:3] == (3.9, 11.2, "thou/cumm")


def test_parse_trend_page_geometry():
    """A two-date trend table with: a wrapped high (WBC), a missing first-column cell
    (Platelets), and a split range-variant analyte (Neutrophils Relative)."""
    words = []
    words += line(10, ("Component", 56), ("Jul", 188), ("3,", 204), ("2022", 214),
                  ("Jan", 349), ("5,", 367), ("2025", 378))
    # WBC: range low on the value line; the high ('11.20 thou/cumm') wraps to the line below.
    words += line(30, ("WBC", 56))
    words += line(44, ("Normal", 56), ("Range:", 92), ("3.90", 125), ("-", 146),
                  ("6.20", 190), ("thou/cumm", 214), ("5.00", 351), ("thou/cumm", 375))
    words += line(58, ("11.20", 56), ("thou/cumm", 83))
    # Platelets: a value only in the SECOND date column (first column missing).
    words += line(72, ("Platelets", 56))
    words += line(86, ("Normal", 56), ("Range:", 92), ("140", 125), ("-", 146), ("440", 163),
                  ("250", 351), ("thou/cumm", 375))
    # Neutrophils Relative printed twice (integer- vs decimal-formatted range); the five values
    # are split across the two sub-rows and must reassemble under one analyte_key.
    words += line(100, ("Neutrophils", 56), ("Relative", 92))
    words += line(114, ("Normal", 56), ("Range:", 92), ("40", 125), ("-", 140), ("77", 150), ("%", 160))
    words += line(128, ("Neutrophils", 56), ("Relative", 92))
    words += line(142, ("Normal", 56), ("Range:", 92), ("40.0", 125), ("-", 143), ("77.0", 152),
                  ("61.4", 190), ("%", 210))
    words += line(156, ("88.5", 351), ("%", 371))

    rows = lab_parse._parse_page(words)
    lab_parse._backfill(rows)
    assert _by(rows, "wbc") == [
        ("2022-07-03", "6.20", "thou/cumm", 3.9, 11.2),
        ("2025-01-05", "5.00", "thou/cumm", 3.9, 11.2)]
    assert _by(rows, "platelets") == [("2025-01-05", "250", "thou/cumm", 140.0, 440.0)]
    # Both date values landed under one key despite the two printed sub-rows.
    assert _by(rows, "neutrophils_pct") == [
        ("2022-07-03", "61.4", "%", 40.0, 77.0),
        ("2025-01-05", "88.5", "%", 40.0, 77.0)]


def test_parse_single_report_geometry():
    """A vertical single-encounter report (Quest-style): 'Name … value' then a
    'Desired Range: LOW-HIGH UNIT' line, one collection date."""
    words = []
    words += line(141, ("White", 66), ("Blood", 95), ("Cell", 124), ("Count", 144), ("5.3", 505))
    words += line(153, ("Desired", 66), ("Range:", 93), ("3.8-10.8", 121), ("Thousand/uL", 149))
    words += line(173, ("Absolute", 66), ("Neutrophils", 100), ("2942", 505))
    words += line(185, ("Desired", 66), ("Range:", 93), ("1500-7800", 121), ("cells/uL", 153))

    rows = lab_parse._parse_single_report([words], "2026-05-12")
    got = {r["analyte_key"]: r for r in rows}
    assert got["wbc"]["value_text"] == "5.3" and got["wbc"]["unit"] == "Thousand/uL"
    assert got["wbc"]["ref_low"] == 3.8 and got["wbc"]["ref_high"] == 10.8
    assert got["wbc"]["collected_at"] == "2026-05-12"
    assert got["neutrophils_abs"]["value_text"] == "2942"      # 4-digit value parsed
    assert got["neutrophils_abs"]["ref_high"] == 7800.0


def test_value_above_its_own_name_binds_correctly():
    """A row whose value cells render slightly ABOVE its own label (the BUN/Creatinine Ratio
    case) must bind to THAT analyte, not the one above it — nearest name by distance."""
    words = []
    words += line(10, ("Component", 56), ("Jul", 188), ("3,", 204), ("2022", 214))
    words += line(40, ("Creatinine", 56))
    words += line(54, ("Normal", 56), ("Range:", 92), ("0.70", 125), ("-", 146), ("0.81", 190))
    words += line(68, ("1.20", 56), ("mg/dL", 83))     # creatinine high wrap (no value)
    words += line(82, ("21", 190))                      # ratio value, rendered ABOVE its name
    words += line(88, ("Ratio", 56))
    words += line(102, ("Normal", 56), ("Range:", 92), ("10", 125), ("-", 138), ("20", 145))

    rows = lab_parse._parse_page(words)
    cre = [r["value_text"] for r in rows if r["analyte_key"] == "creatinine"]
    rat = [r["value_text"] for r in rows if r["analyte_key"] == "ratio"]
    assert cre == ["0.81"] and rat == ["21"]            # 21 is NOT mis-bound to creatinine
