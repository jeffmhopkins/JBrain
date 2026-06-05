"""Phase 1 vision-intake tests. The model call and OCR engine are stubbed (no network, no
Tesseract, no PHI) so we exercise the FAITHFULNESS plumbing: JSON parsing, schema coercion,
and — the crux — that a value reaches the staged results only if the independent OCR corpus
also contains it."""
from app.services import lab_vision


def test_parse_json_rows_tolerates_fences_and_prose():
    assert lab_vision._parse_json_rows('```json\n[{"a":1}]\n```') == [{"a": 1}]
    assert lab_vision._parse_json_rows("here you go: [{\"x\":2}] done") == [{"x": 2}]
    assert lab_vision._parse_json_rows("no array here") is None
    assert lab_vision._parse_json_rows("") is None


def test_normalize_coerces_to_lab_parse_schema():
    rows = lab_vision._normalize([
        {"test_name": "Auto WBC", "value_text": "4.27", "unit": "thou/cumm",
         "ref_low": 3.9, "ref_high": 11.2, "collected_at": "2026-05-21"},
        {"test_name": "NRBC Absolute", "value_text": "<0.01", "unit": "thou/cumm", "collected_at": "2026-05-21"},
        {"test_name": "", "value_text": "9"},           # no name -> dropped
        {"test_name": "Glucose", "value_text": ""},     # no value -> dropped
        {"test_name": "Sodium", "value_text": "140", "collected_at": "not-a-date"},  # bad date -> None
    ])
    by = {r["analyte_key"]: r for r in rows}
    assert by["wbc"]["value_num"] == 4.27 and by["wbc"]["collected_at"] == "2026-05-21"
    assert by["nrbc_abs"]["value_text"] == "<0.01" and by["nrbc_abs"]["value_num"] is None
    assert by["sodium"]["collected_at"] is None
    assert "glucose" not in by and len(rows) == 3


def test_ocr_cross_read_gates_unconfirmed_values(monkeypatch):
    """The model proposes three values; OCR independently 'sees' only two. The unconfirmed one
    must be dropped into skips with a reason, never staged."""
    monkeypatch.setattr(lab_vision, "_ocr_corpus", lambda imgs: "Auto WBC 4.27 thou/cumm  Platelets 291")
    monkeypatch.setattr(lab_vision, "_vision_rows", lambda imgs: [
        {"test_name": "Auto WBC", "value_text": "4.27", "collected_at": "2026-05-21"},
        {"test_name": "Platelets", "value_text": "291", "collected_at": "2026-05-21"},
        {"test_name": "Hemoglobin", "value_text": "15.0", "collected_at": "2026-05-21"},  # NOT in OCR
    ])
    out = lab_vision.parse_lab_image([b"fake-image-bytes"])
    assert out["doc_type"] == "lab_image"
    kept = {r["analyte_key"]: r for r in out["results"]}
    assert set(kept) == {"wbc", "platelets"}            # hemoglobin dropped: OCR didn't confirm it
    assert all(r["confidence"] == "medium" for r in out["results"])   # model-derived, never auto-trusted
    reasons = [s["reason"] for s in out["skips"]]
    assert any("OCR" in r for r in reasons)


def test_empty_model_result_is_not_a_lab_not_unreadable(monkeypatch):
    """A photo the model reads as having NO lab values is 'not a lab' (silent unknown), not the
    visible 'image_unparsed' state — the latter is reserved for inputs we recognised but couldn't
    verify, so a random photo on a medical note doesn't nag the reviewer."""
    monkeypatch.setattr(lab_vision, "_ocr_corpus", lambda imgs: "some unrelated text")
    monkeypatch.setattr(lab_vision, "_vision_rows", lambda imgs: [])
    out = lab_vision.parse_lab_image([b"fake"])
    assert out["doc_type"] == "unknown" and out["results"] == []


def test_no_ocr_engine_refuses_to_trust_vision(monkeypatch):
    """With no independent reader we must NOT stage ungated model output — and must say so
    visibly (image_unparsed), never silently return empty."""
    monkeypatch.setattr(lab_vision, "_ocr_corpus", lambda imgs: None)
    out = lab_vision.parse_lab_image([b"fake"])
    assert out["doc_type"] == "image_unparsed" and out["results"] == []
    assert out["skips"] and "OCR" in out["skips"][0]["reason"]
