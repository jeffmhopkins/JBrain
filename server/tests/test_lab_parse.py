"""Deterministic lab-PDF parser tests. The geometry layer is exercised with synthetic word
boxes (no PDF, no PHI) that reproduce the real-world quirks: values mapped to date columns by
x, a wrapped high/unit line, a missing cell, and one analyte printed as two range-variant
sub-rows that must merge by analyte_key."""
from app.services import lab_parse
import pytest

pytestmark = pytest.mark.unit


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


def test_table_caption_index_is_not_a_value():
    """Each table is introduced by a caption like '… (Table 2 of 7)'. Its index digit sits in
    the first date column's x-band and must NOT be read as a value for the top analyte (the
    real-world 'Auto WBC' picked up 2/3/5/6 from 'Table N of 7' captions)."""
    words = []
    # Table 1 (its header sets the column anchors that are still active when the caption below
    # it is reached — exactly as in the real export).
    words += line(10, ("Component", 56), ("Jul", 188), ("3,", 204), ("2022", 214))
    words += line(24, ("WBC", 56))
    words += line(38, ("Normal", 56), ("Range:", 92), ("3.90", 125), ("-", 146), ("6.20", 190),
                  ("thou/cumm", 214))
    words += line(52, ("11.20", 56), ("thou/cumm", 83))
    # Caption introducing table 2; '2' of '(Table 2 of 7)' sits in table-1's first column band.
    words += line(80, ("Feb", 56), ("3,", 80), ("2026", 95), ("-", 130), ("(Table", 150),
                  ("2", 190), ("of", 210), ("7)", 225))
    # Table 2.
    words += line(100, ("Component", 56), ("Feb", 188), ("3,", 204), ("2026", 214))
    words += line(114, ("WBC", 56))
    words += line(128, ("Normal", 56), ("Range:", 92), ("3.90", 125), ("-", 146), ("7.40", 190),
                  ("thou/cumm", 214))
    words += line(142, ("11.20", 56), ("thou/cumm", 83))

    rows = lab_parse._parse_page(words)
    wbc = sorted(r["value_text"] for r in rows if r["analyte_key"] == "wbc")
    assert wbc == ["6.20", "7.40"]     # the caption '2' did NOT leak in as a third WBC value


def test_confidence_flags_ambiguous_bind_not_biology():
    """Per-row confidence (F3) must fire on the parser's real failure modes (a value bound to
    an analyte by a near-tie) but NOT on genuinely extreme-but-real values — a critically low
    platelet count is the most important row to trust, never to bury."""
    words = []
    words += line(10, ("Component", 56), ("Jul", 188), ("3,", 204), ("2022", 214))
    # Platelets with a critically low real value (5) far below its reference low (140).
    words += line(40, ("Platelets", 56))
    words += line(54, ("Normal", 56), ("Range:", 92), ("140", 125), ("-", 146), ("5", 190),
                  ("thou/cumm", 214))
    words += line(68, ("440", 56), ("thou/cumm", 83))
    rows = lab_parse._parse_page(words)
    lab_parse._backfill(rows)
    lab_parse._score_confidence(rows)
    plt = [r for r in rows if r["analyte_key"] == "platelets"]
    assert plt and plt[0]["confidence"] == "high"      # 5 vs ref-low 140 is real, NOT demoted

    # A value sitting exactly between two analytes (near-tie name bind) IS demoted.
    w2 = []
    w2 += line(10, ("Component", 56), ("Jul", 188), ("3,", 204), ("2022", 214))
    w2 += line(40, ("Sodium", 56))
    w2 += line(54, ("Normal", 56), ("Range:", 92), ("135", 125), ("-", 146), ("145", 190))
    w2 += line(68, ("9.2", 190))                        # value midway between the two names (tops 40/96)
    w2 += line(96, ("Potassium", 56))
    w2 += line(110, ("Normal", 56), ("Range:", 92), ("3.5", 125), ("-", 146), ("5.1", 190))
    r2 = lab_parse._parse_page(w2)
    lab_parse._backfill(r2)
    lab_parse._score_confidence(r2)
    mid = [r for r in r2 if r["value_text"] == "9.2"]
    assert mid and mid[0]["confidence"] in ("low", "medium")   # ambiguous bind is surfaced


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


def test_date_validation_rejects_impossible_calendar_dates():
    """mdy_to_iso must reject day-for-month, not just day<=31 — '2025-02-30' would be silently
    shifted to '2025-03-02' by SQLite date() while string-sorting as Feb 30 (review finding)."""
    assert lab_parse.mdy_to_iso(2, 30, 2025) is None      # Feb 30 — impossible
    assert lab_parse.mdy_to_iso(2, 29, 2024) == "2024-02-29"   # leap day — valid
    assert lab_parse.mdy_to_iso(2, 29, 2025) is None      # not a leap year
    assert lab_parse.mdy_to_iso(13, 6, 2025) == "2025-06-13"   # DD/MM (first field >12)
    assert lab_parse.parse_date("Feb 30, 2025") is None


def test_normalize_dob_to_iso():
    """Owner/document DOBs are compared on a common ISO footing — format drift must not read as
    a different person."""
    for raw in ("07/04/1990", "1990-07-04", "Jul 4, 1990"):
        assert lab_parse.normalize_dob(raw) == "1990-07-04"
    assert lab_parse.normalize_dob("13/40/1990") is None       # garbage -> None, never raw
    assert lab_parse.normalize_dob("") is None


def test_is_faithful_numeric_equality_without_accepting_a_different_number():
    assert lab_parse.is_faithful("15.0", "wbc 15 thou") is True       # trailing .0 tolerated
    assert lab_parse.is_faithful("1234", "plt 1,234 x") is True       # thousands separator
    assert lab_parse.is_faithful("12", "plt 120 x") is False          # NOT a substring of 120
    assert lab_parse.is_faithful("5", "x 50 y") is False
    assert lab_parse.is_faithful("<0.01", "nrbc <0.01") is True


def test_parse_identity_name_and_dob():
    """Patient identity (P1) is extracted from both layouts so a wrong-patient upload can be
    flagged before it merges into the single owner's trends."""
    cbc = lab_parse.parse_identity("Result Trends\nJane Doe Date of Birth: Jul 4, 1990\n...")
    assert cbc["name"] == "Jane Doe" and cbc["dob"] == "1990-07-04"
    quest = lab_parse.parse_identity("DOE,JANE\nDOB: 07/04/1990 | Sex: F | Collected Date: 05/12/2026")
    assert quest["name"] == "DOE,JANE" and quest["dob"] == "1990-07-04"
    assert lab_parse.parse_identity("no identity here") == {}


def test_specialty_report_range_above_value_and_onesided():
    """A specialty single-analyte report (Quest ADAMTS13 form): a 'Value' column header, the
    'Reference Range:' line ABOVE the value row, a one-sided '<0.4 BEU' range, and the value's
    flag printed to its right. The analyte must bind to its own row below the range, the flag
    and the page-header address must stay out, and the one-sided range -> high only."""
    words = []
    words += line(20, ("ANYTOWN,", 200), ("ST", 290), ("00000", 320))                 # address (metadata, above header)
    words += line(60, ("Analyte", 57), ("Value", 335))                                # value-column header
    words += line(90, ("Reference", 368), ("Range:", 400), ("<0.4", 423), ("BEU", 438))
    words += line(110, ("ADAMTS13", 68), ("Inhibitor", 113), ("1.2", 337), ("H", 350))
    rows = lab_parse._parse_single_report([words], "2025-01-15", "09:00")
    assert len(rows) == 1
    r = rows[0]
    assert r["analyte_key"] == "adamts13_inhibitor"      # flag 'H' and the address are excluded
    assert r["value_text"] == "1.2" and r["ref_low"] is None and r["ref_high"] == 0.4
    assert r["unit"] == "BEU" and r["collected_at"] == "2025-01-15" and r["collected_time"] == "09:00"


def test_report_range_onesided_and_twosided():
    assert lab_parse._report_range("3.8 - 10.8 Thousand/uL")[:3] == (3.8, 10.8, "Thousand/uL")
    assert lab_parse._report_range("<0.4 BEU") == (None, 0.4, "BEU", "<0.4 BEU")
    assert lab_parse._report_range(">5 x")[:3] == (5.0, None, "x")


def test_collected_regex_accepts_both_label_forms():
    assert lab_parse._COLLECTED_RE.search("Collected: 02/06/2026 13:42").group(1) == "02"
    assert lab_parse._COLLECTED_RE.search("Collected Date: 05/12/2026 11:48").group(4) == "11"


def test_single_report_captures_collection_time():
    """P4: a report header 'Collected Date: 05/12/2026 11:48' yields collected_time for
    intra-day ordering; rows still carry the date-only collected_at for the trend x-axis."""
    words = []
    words += line(141, ("White", 66), ("Blood", 95), ("Cell", 124), ("Count", 144), ("5.3", 505))
    words += line(153, ("Desired", 66), ("Range:", 93), ("3.8-10.8", 121), ("Thousand/uL", 149))
    rows = lab_parse._parse_single_report([words], "2026-05-12", "11:48")
    assert rows and rows[0]["collected_at"] == "2026-05-12" and rows[0]["collected_time"] == "11:48"
    # The header regex pulls the optional time.
    m = lab_parse._COLLECTED_RE.search("Collected Date: 05/12/2026 11:48 | Status: FINAL")
    assert m.group(4) == "11" and m.group(5) == "48"
    assert lab_parse._COLLECTED_RE.search("Collected Date: 05/12/2026").group(4) is None


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
