"""Tests for the optional PaddleOCR fallback and its layout recovery (Phase 9).

PaddleOCR is never installed for the test suite, so the adapter is exercised through a
fake ``PaddleOCR`` class (patched in place of the lazy import) and the geometry helpers
are tested with synthetic line boxes.
"""

from typing import Any

import pytest
from PIL import Image

from app.services import ocr_fallback
from app.services.ocr_fallback import (
    OcrError,
    PaddleOcrEngine,
    clear_ocr_engine_cache,
    get_ocr_engine,
    group_lines_into_rows,
    is_ocr_available,
    normalise_paddle_result,
    ocr_lines_to_text,
    recover_table_rows,
)
from tests.ocr_stubs import make_line


@pytest.fixture()
def require_numpy() -> None:
    """Skip a test when numpy (which ships with PaddlePaddle) is not installed."""

    pytest.importorskip("numpy")


@pytest.fixture()
def image() -> Image.Image:
    """A tiny stand-in for a rendered page."""

    return Image.new("RGB", (24, 24), color=(255, 255, 255))


@pytest.fixture(autouse=True)
def clear_engine_cache() -> Any:
    """Keep the engine/availability caches isolated between tests."""

    clear_ocr_engine_cache()
    yield
    clear_ocr_engine_cache()


# --- layout recovery --------------------------------------------------------------


def test_group_lines_into_rows_orders_columns_left_to_right() -> None:
    lines = [
        make_line("Amount", 300.0, 150.0),
        make_line("Description", 100.0, 150.0),
        make_line("Total", 100.0, 180.0),
    ]

    rows = group_lines_into_rows(lines)

    assert [[line.text for line in row] for row in rows] == [
        ["Description", "Amount"],
        ["Total"],
    ]


def test_recover_table_rows_builds_a_grid_from_line_geometry() -> None:
    lines = [
        make_line("Description", 100.0, 150.0),
        make_line("Amount", 300.0, 150.0),
        make_line("Consulting", 100.0, 180.0),
        make_line("1,000.00", 300.0, 180.0),
        make_line("Total", 100.0, 210.0),
        make_line("1,050.00", 300.0, 210.0),
    ]

    rows = recover_table_rows(lines)

    assert rows == [
        ["Description", "Amount"],
        ["Consulting", "1,000.00"],
        ["Total", "1,050.00"],
    ]


def test_recover_table_rows_keeps_missing_cells_empty() -> None:
    lines = [
        make_line("Invoice No: INV-1", 100.0, 60.0),
        make_line("Description", 100.0, 150.0),
        make_line("Amount", 300.0, 150.0),
        make_line("Consulting", 100.0, 180.0),
        make_line("1,000.00", 300.0, 180.0),
        make_line("Total", 100.0, 210.0),
        make_line("1,050.00", 300.0, 210.0),
    ]

    rows = recover_table_rows(lines)

    assert rows[0] == ["Invoice No: INV-1", ""]
    assert all(len(row) == 2 for row in rows)


def test_recover_table_rows_ignores_running_text() -> None:
    # Four unbroken lines: no column gaps, so this is a paragraph, not a table.
    lines = [
        make_line("Terms and conditions apply to this invoice.", 100.0, 150.0),
        make_line("Payment is due within thirty days of the date.", 100.0, 162.0),
        make_line("Late payment may attract additional charges.", 100.0, 174.0),
        make_line("Please quote the invoice number in any reply.", 100.0, 186.0),
    ]

    assert recover_table_rows(lines) == []


def test_ocr_lines_to_text_keeps_the_column_layout() -> None:
    lines = [
        make_line("Subtotal", 100.0, 150.0),
        make_line("1,000.00", 300.0, 150.0),
        make_line("VAT 5%", 100.0, 180.0),
        make_line("50.00", 300.0, 180.0),
    ]

    assert ocr_lines_to_text(lines) == "Subtotal | 1,000.00\nVAT 5% | 50.00"


def test_ocr_lines_to_text_ignores_blank_lines() -> None:
    lines = [make_line("   ", 100.0, 150.0), make_line("Total", 100.0, 180.0)]

    assert ocr_lines_to_text(lines) == "Total"


# --- PaddleOCR result normalisation -----------------------------------------------


def test_normalise_paddle_result_reads_the_2x_shape() -> None:
    raw = [
        [
            [[[100, 150], [180, 150], [180, 162], [100, 162]], ("Subtotal", 0.99)],
            [[[300, 150], [360, 150], [360, 162], [300, 162]], ("1,000.00", 0.95)],
        ]
    ]

    lines = normalise_paddle_result(raw)

    assert [(line.text, line.left, line.top, line.confidence) for line in lines] == [
        ("Subtotal", 100.0, 150.0, 0.99),
        ("1,000.00", 300.0, 150.0, 0.95),
    ]


def test_normalise_paddle_result_reads_the_3x_shape() -> None:
    # PaddleOCR 3.x returns one result object per page with parallel arrays.
    raw = [
        {
            "rec_texts": ["Total", "1,050.00"],
            "rec_scores": [0.98, 0.94],
            "dt_polys": [
                [[100, 210], [150, 210], [150, 222], [100, 222]],
                [[300, 210], [380, 210], [380, 222], [300, 222]],
            ],
        }
    ]

    lines = normalise_paddle_result(raw)

    assert [line.text for line in lines] == ["Total", "1,050.00"]
    assert lines[1].right == 380.0


def test_normalise_paddle_result_reads_the_wrapped_payload() -> None:
    raw = [{"res": {"rec_texts": ["Invoice No: INV-1"], "dt_polys": [[[0, 0], [1, 1]]]}}]

    lines = normalise_paddle_result(raw)

    assert [line.text for line in lines] == ["Invoice No: INV-1"]
    # A minimal two-point box is still bounded, and a missing score defaults to 1.0.
    assert (lines[0].left, lines[0].top, lines[0].right, lines[0].bottom) == (
        0.0,
        0.0,
        1.0,
        1.0,
    )
    assert lines[0].confidence == 1.0


def test_normalise_paddle_result_reads_numpy_boxes(require_numpy: None) -> None:
    numpy = pytest.importorskip("numpy")
    raw = [
        {
            "rec_texts": ["Total"],
            "rec_scores": numpy.array([0.9]),
            "dt_polys": numpy.array([[[100, 210], [150, 210], [150, 222], [100, 222]]]),
        }
    ]

    lines = normalise_paddle_result(raw)

    assert [(line.text, line.left, line.bottom) for line in lines] == [
        ("Total", 100.0, 222.0)
    ]


def test_normalise_paddle_result_ignores_unknown_shapes() -> None:
    assert normalise_paddle_result(None) == []
    assert normalise_paddle_result([None, "junk", 42, {}, [[1, 2]]]) == []


# --- engine adapter ---------------------------------------------------------------


class FakePaddleOcr2:
    """A PaddleOCR 2.x style engine (``ocr()`` returning box/text/score triples)."""

    def __init__(self, **kwargs: Any) -> None:
        self.kwargs = kwargs
        self.calls: list[Any] = []

    def ocr(self, array: Any) -> Any:
        self.calls.append(array)
        return [
            [
                [[[100, 150], [180, 150], [180, 162], [100, 162]], ("Subtotal", 0.99)],
                [[[300, 150], [360, 150], [360, 162], [300, 162]], ("1,000.00", 0.4)],
            ]
        ]


class FakePaddleOcr3:
    """A PaddleOCR 3.x style engine (``predict()`` returning result mappings)."""

    def __init__(self, **kwargs: Any) -> None:
        self.kwargs = kwargs

    def predict(self, array: Any) -> Any:
        return [
            {
                "rec_texts": ["Subtotal", "1,000.00"],
                "rec_scores": [0.99, 0.4],
                "dt_polys": [
                    [[100, 150], [180, 150], [180, 162], [100, 162]],
                    [[300, 150], [360, 150], [360, 162], [300, 162]],
                ],
            }
        ]


class StrictPaddleOcr:
    """An engine that only accepts ``lang``, like a minimal older release."""

    def __init__(self, lang: str) -> None:
        self.lang = lang


class FakePaddleOcr37:
    """A PaddleOCR 3.x signature: named OCR options plus the common arguments."""

    def __init__(
        self,
        lang: str | None = None,
        use_textline_orientation: bool | None = None,
        text_rec_score_thresh: float | None = None,
        use_doc_orientation_classify: bool | None = None,
        use_doc_unwarping: bool | None = None,
        **kwargs: Any,
    ) -> None:
        self.kwargs: dict[str, Any] = {
            "lang": lang,
            "use_textline_orientation": use_textline_orientation,
            "text_rec_score_thresh": text_rec_score_thresh,
            "use_doc_orientation_classify": use_doc_orientation_classify,
            "use_doc_unwarping": use_doc_unwarping,
            **kwargs,
        }

    def predict(self, array: Any) -> Any:
        return [
            {
                "rec_texts": ["Subtotal", "1,000.00"],
                "rec_scores": [0.99, 0.4],
                "dt_polys": [
                    [[100, 150], [180, 150], [180, 162], [100, 162]],
                    [[300, 150], [360, 150], [360, 162], [300, 162]],
                ],
            }
        ]


class StrictPaddleOcr3:
    """PaddleOCR 3.x keyword handling: anything undeclared raises ValueError.

    This is the failure reported from a laptop: the adapter used to send the removed 2.x
    flag ``use_gpu``, and PaddleOCR 3.x answered
    ``ValueError: Unknown argument: use_gpu``.
    """

    ALLOWED_KEYWORDS = frozenset(
        {
            "lang",
            "use_textline_orientation",
            "text_rec_score_thresh",
            "device",
            "enable_mkldnn",
            "cpu_threads",
        }
    )

    def __init__(self, **kwargs: Any) -> None:
        for name in kwargs:
            if name not in self.ALLOWED_KEYWORDS:
                raise ValueError(f"Unknown argument: {name}")
        self.kwargs = dict(kwargs)

    def predict(self, array: Any) -> Any:
        return [
            {
                "rec_texts": ["Total"],
                "rec_scores": [0.99],
                "dt_polys": [[[1, 2], [3, 2], [3, 4], [1, 4]]],
            }
        ]


class DriftingPaddleOcr:
    """Accepts ``lang`` only and rejects every other keyword, like a future rename."""

    def __init__(self, **kwargs: Any) -> None:
        for name in kwargs:
            if name != "lang":
                raise ValueError(f"Unknown argument: {name}")
        self.kwargs = dict(kwargs)


class TypeErrorPaddleOcr:
    """A permissive signature whose constructor still refuses ``device``."""

    def __init__(self, lang: str = "en", **kwargs: Any) -> None:
        if "device" in kwargs:
            raise TypeError("__init__() got an unexpected keyword argument 'device'")
        self.kwargs = {"lang": lang, **kwargs}


class FakePaddleOcr2Signature:
    """A PaddleOCR 2.x signature, which takes ``use_gpu`` and has no ``device``."""

    def __init__(
        self,
        lang: str = "en",
        use_gpu: bool = False,
        use_angle_cls: bool = False,
        drop_score: float = 0.5,
    ) -> None:
        self.kwargs: dict[str, Any] = {
            "lang": lang,
            "use_gpu": use_gpu,
            "use_angle_cls": use_angle_cls,
            "drop_score": drop_score,
        }

    def ocr(self, array: Any) -> Any:
        return [[[[[100, 150], [180, 150], [180, 162], [100, 162]], ("Total", 0.9)]]]


class BothPredictionMethods:
    """Exposes both APIs, as PaddleOCR 3.x does (its ``ocr`` is a deprecated wrapper)."""

    def __init__(self, **kwargs: Any) -> None:
        self.called: list[str] = []

    def predict(self, array: Any) -> Any:
        self.called.append("predict")
        return [
            {
                "rec_texts": ["Total"],
                "rec_scores": [0.99],
                "dt_polys": [[[1, 2], [3, 2], [3, 4], [1, 4]]],
            }
        ]

    def ocr(self, array: Any) -> Any:
        self.called.append("ocr")
        return [[[[[1, 2], [3, 2], [3, 4], [1, 4]], ("Total", 0.9)]]]


class FailingPredictPaddleOcr:
    """``predict`` is broken, so the adapter has to fall back to ``ocr``."""

    def __init__(self, **kwargs: Any) -> None:
        pass

    def predict(self, array: Any) -> Any:
        raise ValueError("the cls argument was removed")

    def ocr(self, array: Any) -> Any:
        box = [[100, 150], [180, 150], [180, 162], [100, 162]]
        return [[[box, ("Subtotal", 0.99)]]]


def _install(monkeypatch: pytest.MonkeyPatch, engine_class: Any) -> None:
    """Patch the lazy PaddleOCR import so the adapter uses `engine_class`."""

    monkeypatch.setattr(ocr_fallback, "_load_paddleocr", lambda: engine_class)
    clear_ocr_engine_cache()


def test_engine_reads_a_2x_result(
    monkeypatch: pytest.MonkeyPatch, image: Image.Image, require_numpy: None
) -> None:
    _install(monkeypatch, FakePaddleOcr2)
    engine = PaddleOcrEngine(language="en")

    lines = engine.recognise(image)

    assert [line.text for line in lines] == ["Subtotal", "1,000.00"]
    raw = engine._resolve_engine()
    assert raw.calls, "the page image must reach the OCR engine"
    assert raw.calls[0].shape[2] == 3  # BGR image array


def test_engine_reads_a_3x_result(
    monkeypatch: pytest.MonkeyPatch, image: Image.Image, require_numpy: None
) -> None:
    _install(monkeypatch, FakePaddleOcr3)
    engine = PaddleOcrEngine(language="en")

    assert [line.text for line in engine.recognise(image)] == ["Subtotal", "1,000.00"]


def test_engine_drops_lines_below_the_confidence_floor(
    monkeypatch: pytest.MonkeyPatch, image: Image.Image, require_numpy: None
) -> None:
    _install(monkeypatch, FakePaddleOcr3)
    engine = PaddleOcrEngine(language="en", min_confidence=0.9)

    assert [line.text for line in engine.recognise(image)] == ["Subtotal"]


def test_engine_falls_back_to_a_supported_constructor(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install(monkeypatch, StrictPaddleOcr)

    raw = PaddleOcrEngine(language="ar")._resolve_engine()

    assert isinstance(raw, StrictPaddleOcr)
    assert raw.lang == "ar"


def test_engine_never_sends_the_removed_use_gpu_flag(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """PaddleOCR 3.x replaced `use_gpu` with `device` and dropped `show_log`."""

    _install(monkeypatch, FakePaddleOcr37)

    raw = PaddleOcrEngine(language="en", device="cpu")._resolve_engine()

    assert raw.kwargs["lang"] == "en"
    assert raw.kwargs["device"] == "cpu"
    assert raw.kwargs["use_textline_orientation"] is True
    assert "use_gpu" not in raw.kwargs
    assert "show_log" not in raw.kwargs


def test_engine_requests_portable_cpu_settings(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """oneDNN stays off and the document pre-processors stay out of the pipeline.

    PaddlePaddle 3.x raises NotImplementedError inside oneDNN on some CPU builds, and the
    orientation/unwarping models only add CPU time to a page we rasterised ourselves.
    """

    _install(monkeypatch, FakePaddleOcr37)

    raw = PaddleOcrEngine(language="en", device="cpu")._resolve_engine()

    assert raw.kwargs["enable_mkldnn"] is False
    assert raw.kwargs["cpu_threads"] == 4
    assert raw.kwargs["use_doc_orientation_classify"] is False
    assert raw.kwargs["use_doc_unwarping"] is False

    enabled = PaddleOcrEngine(language="en", enable_mkldnn=True)._resolve_engine()
    assert enabled.kwargs["enable_mkldnn"] is True


def test_engine_works_with_a_3x_paddleocr_that_rejects_unknown_arguments(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The reported laptop failure: `Unknown argument: use_gpu` must not be raised."""

    _install(monkeypatch, StrictPaddleOcr3)

    raw = PaddleOcrEngine(language="en", device="cpu")._resolve_engine()

    assert raw.kwargs == {
        "lang": "en",
        "device": "cpu",
        "enable_mkldnn": False,
        "cpu_threads": 4,
    }


def test_engine_drops_only_the_keyword_a_version_rejects(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install(monkeypatch, DriftingPaddleOcr)

    raw = PaddleOcrEngine(language="ar", device="cpu")._resolve_engine()

    assert raw.kwargs == {"lang": "ar"}, "only the refused keyword is dropped"


def test_engine_drops_a_keyword_python_itself_rejects(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install(monkeypatch, TypeErrorPaddleOcr)

    raw = PaddleOcrEngine(language="en", device="cpu")._resolve_engine()

    assert raw.kwargs == {"lang": "en", "enable_mkldnn": False, "cpu_threads": 4}


def test_engine_translates_the_device_setting_for_a_2x_install(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install(monkeypatch, FakePaddleOcr2Signature)

    gpu = PaddleOcrEngine(language="en", device="gpu:0")._resolve_engine()
    assert gpu.kwargs["use_gpu"] is True
    assert "device" not in gpu.kwargs

    cpu = PaddleOcrEngine(language="en", device="cpu")._resolve_engine()
    assert cpu.kwargs["use_gpu"] is False
    assert cpu.kwargs["use_angle_cls"] is True


def test_engine_sends_the_confidence_floor_under_its_3x_name(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install(monkeypatch, FakePaddleOcr37)

    floored = PaddleOcrEngine(language="en", min_confidence=0.6)._resolve_engine()
    assert floored.kwargs["text_rec_score_thresh"] == 0.6

    default = PaddleOcrEngine(language="en")._resolve_engine()
    assert default.kwargs["text_rec_score_thresh"] is None, (
        "the library default is kept when no floor is configured"
    )


def test_engine_prefers_predict_over_the_deprecated_ocr_wrapper(
    monkeypatch: pytest.MonkeyPatch, image: Image.Image, require_numpy: None
) -> None:
    _install(monkeypatch, BothPredictionMethods)

    engine = PaddleOcrEngine(language="en")

    assert [line.text for line in engine.recognise(image)] == ["Total"]
    assert engine._resolve_engine().called == ["predict"]


def test_engine_falls_through_to_the_next_prediction_method(
    monkeypatch: pytest.MonkeyPatch, image: Image.Image, require_numpy: None
) -> None:
    _install(monkeypatch, FailingPredictPaddleOcr)

    lines = PaddleOcrEngine(language="en").recognise(image)

    assert [line.text for line in lines] == ["Subtotal"]


def test_get_ocr_engine_uses_the_configured_device(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install(monkeypatch, FakePaddleOcr37)
    monkeypatch.setattr(ocr_fallback.settings, "ocr_device", "gpu:0")
    monkeypatch.setattr(ocr_fallback.settings, "ocr_enable_mkldnn", True)

    engine = get_ocr_engine(language="en")

    assert engine is not None
    assert engine.device == "gpu:0"
    assert engine.enable_mkldnn is True
    assert engine._resolve_engine().kwargs["device"] == "gpu:0"
    assert engine._resolve_engine().kwargs["enable_mkldnn"] is True
    assert get_ocr_engine(language="en") is engine, "the engine must be built once"
    assert get_ocr_engine(language="en", device="cpu").device == "cpu"
    assert (
        get_ocr_engine(language="en", enable_mkldnn=False).enable_mkldnn is False
    )


def test_engine_reports_constructor_failures(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class Broken:
        def __init__(self, **kwargs: Any) -> None:
            raise RuntimeError("no paddlepaddle runtime")

    _install(monkeypatch, Broken)

    with pytest.raises(OcrError) as excinfo:
        PaddleOcrEngine()._resolve_engine()

    assert "could not be initialised" in str(excinfo.value)


def test_engine_reports_prediction_failures(
    monkeypatch: pytest.MonkeyPatch, image: Image.Image, require_numpy: None
) -> None:
    class Failing:
        def __init__(self, **kwargs: Any) -> None:
            pass

        def ocr(self, array: Any) -> Any:
            raise ValueError("model file is missing")

    _install(monkeypatch, Failing)

    with pytest.raises(OcrError) as excinfo:
        PaddleOcrEngine().recognise(image)

    assert "model file is missing" in str(excinfo.value)


def test_engine_requires_a_supported_prediction_method(
    monkeypatch: pytest.MonkeyPatch, image: Image.Image, require_numpy: None
) -> None:
    class NoMethod:
        def __init__(self, **kwargs: Any) -> None:
            pass

    _install(monkeypatch, NoMethod)

    with pytest.raises(OcrError) as excinfo:
        PaddleOcrEngine().recognise(image)

    assert "no supported prediction method" in str(excinfo.value)


# --- availability -----------------------------------------------------------------


def _make_paddleocr_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    """Pretend PaddleOCR is not installed."""

    def missing() -> Any:
        raise ImportError("No module named 'paddleocr'")

    monkeypatch.setattr(ocr_fallback, "_load_paddleocr", missing)
    clear_ocr_engine_cache()


def test_ocr_is_reported_unavailable_when_paddleocr_is_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _make_paddleocr_missing(monkeypatch)

    assert is_ocr_available() is False
    # None is a supported deployment state, not an error.
    assert get_ocr_engine() is None
    assert "ImportError" in (ocr_fallback.ocr_availability() or "")


def test_get_ocr_engine_builds_the_configured_engine(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install(monkeypatch, FakePaddleOcr2)

    engine = get_ocr_engine(language="ar")

    assert isinstance(engine, PaddleOcrEngine)
    assert engine.language == "ar"
    assert get_ocr_engine(language="ar") is engine, "the engine must be built once"
