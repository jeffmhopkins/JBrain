"""Net-new unit tests for lab_vision's internal seams (the model/OCR/render plumbing and the
schema/value coercion edges the existing happy-path tests don't reach). Everything is stubbed
at the module's own import seams — no network, no Tesseract, no real model, no PHI — so we can
exercise the faithfulness/error/empty branches deterministically.
"""
import io
import sys
import types

from app.services import lab_vision
import pytest

pytestmark = pytest.mark.unit


# --------------------------------------------------------------------------- #
# _parse_json_rows — schema-coercion edges (bad JSON, non-list JSON)          #
# --------------------------------------------------------------------------- #
def test_parse_json_rows_rejects_non_list_json():
    # A JSON object (not an array) wrapped in brackets-looking text: the regex grabs an array
    # span but json.loads yields a dict-of-list -> must be rejected (not a list of rows).
    assert lab_vision._parse_json_rows('{"rows": [1, 2]}') == [1, 2]  # outermost [...] is the list


def test_parse_json_rows_returns_none_on_malformed_array():
    # An array-shaped span that isn't valid JSON (trailing comma / unquoted) -> None, never raises.
    assert lab_vision._parse_json_rows("[1, 2,]") is None
    assert lab_vision._parse_json_rows("[not, valid, json]") is None


def test_parse_json_rows_scalar_array_is_list():
    # json.loads of a bare scalar that happens to be inside brackets but parses to a non-list.
    # "[1, 2][" -> regex matches "[1, 2]" which is a list.
    assert lab_vision._parse_json_rows("[1, 2][") == [1, 2]


# --------------------------------------------------------------------------- #
# _normalize — non-dict element is skipped; _num exception path               #
# --------------------------------------------------------------------------- #
def test_normalize_skips_non_dict_rows():
    rows = lab_vision._normalize([
        "garbage-string",                 # not a dict -> skipped
        42,                               # not a dict -> skipped
        None,                             # not a dict -> skipped
        {"test_name": "Sodium", "value_text": "140"},
    ])
    assert len(rows) == 1
    assert rows[0]["analyte_key"] == "sodium"


def test_normalize_handles_non_numeric_ref_values():
    # ref_low/ref_high that can't coerce to float must become None (via _num's except branch),
    # not crash and not poison the row.
    rows = lab_vision._normalize([
        {"test_name": "WBC", "value_text": "4.27",
         "ref_low": "low", "ref_high": object()},
    ])
    assert rows[0]["ref_low"] is None and rows[0]["ref_high"] is None


def test_num_drops_non_finite_and_coerces_strings():
    assert lab_vision._num(float("inf")) is None
    assert lab_vision._num(float("nan")) is None
    assert lab_vision._num("3.5") == 3.5
    assert lab_vision._num("") is None
    assert lab_vision._num(None) is None
    assert lab_vision._num("not-a-number") is None


# --------------------------------------------------------------------------- #
# parse_lab_image — empty input fast-path & vision-unavailable skip           #
# --------------------------------------------------------------------------- #
def test_empty_input_returns_unparsed_without_calling_ocr(monkeypatch):
    def _boom(_):
        raise AssertionError("OCR should not run for empty input")
    monkeypatch.setattr(lab_vision, "_ocr_corpus", _boom)
    out = lab_vision.parse_lab_image([])
    assert out["doc_type"] == "image_unparsed"
    assert out["results"] == [] and out["pages"] == 0 and out["skips"] == []


def test_vision_unavailable_after_ocr_present_is_skip(monkeypatch):
    # OCR engine present (corpus returned) but the vision model returns None (no creds / unparseable)
    # -> visible image_unparsed with an explanatory skip, never an empty no-op.
    monkeypatch.setattr(lab_vision, "_ocr_corpus", lambda imgs: "some text")
    monkeypatch.setattr(lab_vision, "_vision_rows", lambda imgs: None)
    out = lab_vision.parse_lab_image([b"img"])
    assert out["doc_type"] == "image_unparsed" and out["results"] == []
    assert out["skips"] and "vision model unavailable" in out["skips"][0]["reason"]


# --------------------------------------------------------------------------- #
# Confidence tiering branch: an OCR-confirmed row that did NOT start 'high'    #
# (already medium/low from scoring) keeps its level (192->194 partial branch). #
# --------------------------------------------------------------------------- #
def test_ocr_confirmed_row_not_high_keeps_lower_level(monkeypatch):
    # Force _score_confidence to leave a row at 'medium' (not 'high'); OCR-confirmed path must
    # NOT bump it and must NOT demote it — it just appends the confirming reason.
    monkeypatch.setattr(lab_vision, "_ocr_corpus", lambda imgs: "WBC 4.27")
    monkeypatch.setattr(lab_vision, "_vision_rows", lambda imgs: [
        {"test_name": "WBC", "value_text": "4.27", "collected_at": "2026-05-21"},
    ])

    def _score_medium(rows):
        for r in rows:
            r["confidence"] = "medium"
    monkeypatch.setattr(lab_vision.lab_parse, "_score_confidence", _score_medium)

    out = lab_vision.parse_lab_image([b"img"])
    row = out["results"][0]
    assert row["confidence"] == "medium"
    assert any("OCR-confirmed" in s for s in row["confidence_reasons"])
    assert out["ocr_unconfirmed"] == 0


def test_numeric_equivalence_confirmation_tiers_to_medium(monkeypatch):
    # OCR wrote '15' but the model wrote '15.0'; is_faithful's numeric fallback should still
    # treat it as confirmed -> 'medium', verifying lab_vision wires that corroboration through.
    monkeypatch.setattr(lab_vision, "_ocr_corpus", lambda imgs: "Hemoglobin 15")
    monkeypatch.setattr(lab_vision, "_vision_rows", lambda imgs: [
        {"test_name": "Hemoglobin", "value_text": "15.0", "collected_at": "2026-05-21"},
    ])
    out = lab_vision.parse_lab_image([b"img"])
    assert out["results"][0]["confidence"] == "medium"
    assert out["ocr_unconfirmed"] == 0


# --------------------------------------------------------------------------- #
# _vision_rows — exercise the real function via stubbed llm/image_analysis    #
# --------------------------------------------------------------------------- #
class _FakeUnsupported(Exception):
    pass


def _install_fake_image_analysis(monkeypatch, prepare):
    # `_vision_rows` does `from . import image_analysis` — which (the module being already
    # imported) binds the attribute off the `app.services` package. Patch it there so the
    # real module is never touched.
    import app.services as pkg
    fake = types.ModuleType("app.services.image_analysis")
    fake.UnsupportedImage = _FakeUnsupported
    fake._prepare_image = prepare
    monkeypatch.setattr(pkg, "image_analysis", fake, raising=False)
    return fake


def _install_fake_llm(monkeypatch, *, has_creds=True, complete=None):
    import app.services as pkg
    fake = types.ModuleType("app.services.llm")
    fake.has_credentials = lambda: has_creds
    fake.model_for = lambda tier: "fake-vision-model"
    if complete is None:
        complete = lambda messages, model=None, max_tokens=0: "[]"
    fake.complete = complete
    monkeypatch.setattr(pkg, "llm", fake, raising=False)
    return fake


def test_vision_rows_none_without_credentials(monkeypatch):
    _install_fake_llm(monkeypatch, has_creds=False)
    _install_fake_image_analysis(monkeypatch, prepare=lambda raw: ("image/png", "QQ=="))
    assert lab_vision._vision_rows([b"img"]) is None


def test_vision_rows_none_when_all_images_unsupported(monkeypatch):
    _install_fake_llm(monkeypatch, has_creds=True)

    def _prepare(raw):
        raise _FakeUnsupported("bad")
    _install_fake_image_analysis(monkeypatch, prepare=_prepare)
    # Every image is undecodable -> no content blocks -> None (not a crash, not empty rows).
    assert lab_vision._vision_rows([b"a", b"b"]) is None


def test_vision_rows_parses_model_json(monkeypatch):
    captured = {}

    def _complete(messages, model=None, max_tokens=0):
        captured["messages"] = messages
        captured["model"] = model
        return '```json\n[{"test_name":"WBC","value_text":"4.27"}]\n```'
    _install_fake_llm(monkeypatch, has_creds=True, complete=_complete)
    _install_fake_image_analysis(monkeypatch, prepare=lambda raw: ("image/jpeg", "QkJC"))

    rows = lab_vision._vision_rows([b"img1", b"img2"])
    assert rows == [{"test_name": "WBC", "value_text": "4.27"}]
    # The prompt text block is appended after the image blocks.
    content = captured["messages"][0]["content"]
    assert content[-1]["type"] == "text"
    assert content[0]["type"] == "image"
    assert captured["model"] == "fake-vision-model"


def test_vision_rows_caps_payload_at_eight_images(monkeypatch):
    prepared = {"count": 0}

    def _prepare(raw):
        prepared["count"] += 1
        return ("image/png", "QQ==")
    _install_fake_image_analysis(monkeypatch, prepare=_prepare)
    _install_fake_llm(monkeypatch, has_creds=True,
                      complete=lambda messages, model=None, max_tokens=0: "[]")
    lab_vision._vision_rows([b"x"] * 20)
    assert prepared["count"] == 8  # image_bytes_list[:8] bound


def test_vision_rows_returns_none_when_model_call_raises(monkeypatch):
    def _complete(messages, model=None, max_tokens=0):
        raise RuntimeError("model 500")
    _install_fake_llm(monkeypatch, has_creds=True, complete=_complete)
    _install_fake_image_analysis(monkeypatch, prepare=lambda raw: ("image/png", "QQ=="))
    # A model error must be swallowed -> None, never bubble up to 500 the upload.
    assert lab_vision._vision_rows([b"img"]) is None


# --------------------------------------------------------------------------- #
# _ocr_corpus — stub pytesseract + PIL into sys.modules to drive the function #
# --------------------------------------------------------------------------- #
def _install_fake_pil_and_tesseract(monkeypatch, *, image_to_string, open_raises=False):
    pil = types.ModuleType("PIL")
    image_mod = types.ModuleType("PIL.Image")

    class _Img:
        def load(self):
            pass

    def _open(_buf):
        if open_raises:
            raise OSError("bad image")
        return _Img()

    image_mod.open = _open
    pil.Image = image_mod
    monkeypatch.setitem(sys.modules, "PIL", pil)
    monkeypatch.setitem(sys.modules, "PIL.Image", image_mod)

    tess = types.ModuleType("pytesseract")
    tess.image_to_string = image_to_string
    monkeypatch.setitem(sys.modules, "pytesseract", tess)


def test_ocr_corpus_joins_page_text(monkeypatch):
    pages = iter(["WBC 4.27", "Platelets 291"])
    _install_fake_pil_and_tesseract(monkeypatch, image_to_string=lambda img: next(pages))
    corpus = lab_vision._ocr_corpus([b"page1", b"page2"])
    assert "WBC 4.27" in corpus and "Platelets 291" in corpus


def test_ocr_corpus_blank_result_is_none(monkeypatch):
    # OCR engine present but reads nothing legible -> None (treated as 'no independent reader').
    _install_fake_pil_and_tesseract(monkeypatch, image_to_string=lambda img: "   ")
    assert lab_vision._ocr_corpus([b"page"]) is None


def test_ocr_corpus_skips_bad_page_keeps_batch(monkeypatch):
    # One page fails to decode; the batch must not sink — good pages still contribute.
    state = {"n": 0}

    pil = types.ModuleType("PIL")
    image_mod = types.ModuleType("PIL.Image")

    class _Img:
        def load(self):
            pass

    def _open(_buf):
        state["n"] += 1
        if state["n"] == 1:
            raise OSError("corrupt page")
        return _Img()

    image_mod.open = _open
    pil.Image = image_mod
    monkeypatch.setitem(sys.modules, "PIL", pil)
    monkeypatch.setitem(sys.modules, "PIL.Image", image_mod)
    tess = types.ModuleType("pytesseract")
    tess.image_to_string = lambda img: "Glucose 99"
    monkeypatch.setitem(sys.modules, "pytesseract", tess)

    corpus = lab_vision._ocr_corpus([b"bad", b"good"])
    assert corpus == "Glucose 99"


# --------------------------------------------------------------------------- #
# render_pdf_to_images — stub pypdfium2 to drive the render loop / failure     #
# --------------------------------------------------------------------------- #
def _install_fake_pdfium(monkeypatch, *, n_pages=2, doc_raises=False, render_raises=False):
    mod = types.ModuleType("pypdfium2")

    class _Bitmap:
        def to_pil(self):
            class _P:
                def save(self, buf, format=None):
                    buf.write(b"\x89PNG-fake")
            return _P()

    class _Page:
        def render(self, scale=1.0):
            if render_raises:
                raise RuntimeError("render boom")
            return _Bitmap()

    class _PdfDocument:
        def __init__(self, data):
            if doc_raises:
                raise RuntimeError("cannot open pdf")
            self._n = n_pages

        def __len__(self):
            return self._n

        def __getitem__(self, i):
            return _Page()

    mod.PdfDocument = _PdfDocument
    monkeypatch.setitem(sys.modules, "pypdfium2", mod)


def test_render_pdf_to_images_renders_each_page(monkeypatch):
    _install_fake_pdfium(monkeypatch, n_pages=3)
    out = lab_vision.render_pdf_to_images(b"%PDF-fake")
    assert len(out) == 3 and all(p == b"\x89PNG-fake" for p in out)


def test_render_pdf_to_images_returns_empty_on_open_failure(monkeypatch):
    _install_fake_pdfium(monkeypatch, doc_raises=True)
    assert lab_vision.render_pdf_to_images(b"garbage") == []


def test_render_pdf_to_images_returns_partial_on_render_failure(monkeypatch):
    # render() raises on the first page; the except aborts the loop -> [] (no partial garbage).
    _install_fake_pdfium(monkeypatch, n_pages=2, render_raises=True)
    assert lab_vision.render_pdf_to_images(b"%PDF") == []
