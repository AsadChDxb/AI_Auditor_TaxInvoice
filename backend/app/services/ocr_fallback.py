"""Optional PaddleOCR fallback for scanned, image-only invoice pages.

The fallback is deliberately isolated from the rest of the application:

* PaddleOCR and its PaddlePaddle runtime are heavy, platform-specific packages, so
  they are imported lazily and never at module import time. When they are missing,
  :func:`get_ocr_engine` returns ``None``, and the caller keeps the ``pdfplumber``
  text (or flags the document as ``needs_ocr``) instead of failing the upload.
* ``pdfplumber`` (and therefore Pillow, which renders the page) is already a runtime
  dependency, so a page can be rendered to an in-memory image and handed to the
  engine without writing anything to disk.
* PaddleOCR's result shape differs between major versions, so every supported shape is
  normalised into :class:`OcrLine` records (text plus a bounding box) by
  :func:`normalise_paddle_result`.
* A scanned invoice has no text layer at all, so the table structure is recovered from
  line geometry: :func:`group_lines_into_rows` clusters lines into visual rows and
  :func:`recover_table_rows` clusters their x-positions into columns.
"""

from __future__ import annotations

import inspect
import re
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from functools import lru_cache
from statistics import median
from typing import Any

from app.config import (
    DEFAULT_OCR_CPU_THREADS,
    DEFAULT_OCR_DET_MODEL,
    DEFAULT_OCR_DEVICE,
    DEFAULT_OCR_ENABLE_MKLDNN,
    DEFAULT_OCR_REC_MODEL,
    settings,
)

PADDLEOCR_LABEL = "paddleocr"
"""Label recorded on pages whose text came from the OCR fallback."""

PADDLEOCR_COMMON_ARG_NAMES = frozenset(
    {
        "device",
        "engine",
        "engine_config",
        "enable_hpi",
        "use_tensorrt",
        "precision",
        "enable_mkldnn",
        "mkldnn_cache_capacity",
        "cpu_threads",
        "enable_cinn",
    }
)
"""The extra keywords a PaddleOCR 3.x wrapper accepts on top of its named parameters.

PaddleOCR 3.x collects them in ``paddleocr._common_args.parse_common_args`` and raises
``ValueError: Unknown argument: <name>`` for anything else - which is exactly what the
2.x flags ``use_gpu`` and ``show_log`` trigger, because 3.x replaced them with ``device``
and removed logging from the constructor. They may therefore only be sent after the
installed signature has been checked.
"""

PADDLEOCR_PORTABLE_ARG_NAMES = PADDLEOCR_COMMON_ARG_NAMES | {"lang"}
"""Keywords safe to offer a wrapper that hides its real parameters behind ``**kwargs``.

``lang`` has been a constructor keyword in every PaddleOCR release, and the common
arguments above are forwarded by 3.x, so a class that accepts ``**kwargs`` is assumed to
understand them. The self-healing retry in :meth:`PaddleOcrEngine._build` removes any of
them that a specific version still rejects.
"""

REJECTED_ARGUMENT_PATTERN = re.compile(
    r"unknown argument:\s*([A-Za-z_][A-Za-z0-9_]*)"
    r"|unexpected keyword argument ['\"]([A-Za-z_][A-Za-z0-9_]*)['\"]",
    re.IGNORECASE,
)
"""Matches the two ways a rejected constructor keyword is reported.

PaddleOCR 3.x raises ``ValueError: Unknown argument: use_gpu`` while Python itself raises
``TypeError: __init__() got an unexpected keyword argument 'device'``, so both the name
and the reason can be recovered and just that keyword dropped.
"""

DEFAULT_ROW_OVERLAP_RATIO = 0.5
"""Fraction of a line's height that must overlap a row band to join that row."""

DEFAULT_COLUMN_GAP_RATIO = 0.6
"""Gap (as a fraction of the row's median line height) that starts a new cell."""

DEFAULT_COLUMN_TOLERANCE_RATIO = 0.5
"""Left-edge difference (as a fraction of the median line height) that still counts
as the same table column."""

DEFAULT_MIN_TABLE_ROWS = 3
"""Rows needed before OCR geometry is treated as a table instead of running text."""

DEFAULT_MIN_TABLE_COLUMNS = 2
"""Columns needed before OCR geometry is treated as a table."""

ROW_SEPARATOR = "\n"
COLUMN_SEPARATOR = " | "
"""Separators used when OCR lines are flattened into the page text."""


class OcrError(RuntimeError):
    """The OCR fallback could not read a page."""


@dataclass(frozen=True)
class OcrLine:
    """One recognised text line and its bounding box, in PDF points."""

    text: str
    left: float
    top: float
    right: float
    bottom: float
    confidence: float = 1.0

    @property
    def width(self) -> float:
        """Horizontal extent of the line."""

        return max(self.right - self.left, 0.0)

    @property
    def height(self) -> float:
        """Vertical extent of the line."""

        return max(self.bottom - self.top, 0.0)

    @property
    def centre_x(self) -> float:
        """Horizontal centre, used to order lines inside a row."""

        return (self.left + self.right) / 2

    @property
    def centre_y(self) -> float:
        """Vertical centre, used to order and cluster lines into rows."""

        return (self.top + self.bottom) / 2


class OcrEngine:
    """Duck-typed OCR engine: hand it a page image, get recognised lines back.

    The class exists to document the seam (and to give type hints); the real
    implementation is :class:`PaddleOcrEngine` and tests inject a stub, so the suite
    never needs PaddleOCR, a model download or a network call.
    """

    label = PADDLEOCR_LABEL

    def recognise(self, image: Any) -> list[OcrLine]:
        """Return the text lines recognised in `image`.

        `image` is an in-memory RGB ``PIL.Image`` rendered by pdfplumber, and an empty
        list means "no text found".
        """

        raise NotImplementedError


def _load_paddleocr() -> Any:
    """Import and return the ``PaddleOCR`` class.

    Kept in its own function so the (slow, optional) import happens once, lazily, and
    so tests can replace it with a fake to exercise the adapter offline.
    """

    from paddleocr import PaddleOCR  # imported here on purpose: optional dependency

    return PaddleOCR


@lru_cache(maxsize=1)
def ocr_availability() -> str | None:
    """Return ``None`` when PaddleOCR can be imported, else why it could not be."""

    try:
        _load_paddleocr()
    except Exception as exc:  # noqa: BLE001 - any import failure means "not installed"
        return f"{type(exc).__name__}: {exc}"
    return None


def is_ocr_available() -> bool:
    """True when the PaddleOCR fallback can be used in this process."""

    return ocr_availability() is None


def _accepted_init_parameters(paddleocr: Any) -> frozenset[str]:
    """Return the constructor keywords the installed PaddleOCR version accepts.

    PaddleOCR 2.x declares every option explicitly, whereas 3.x declares its own options
    and forwards the rest to ``parse_common_args``, which rejects unknown names with
    ``ValueError: Unknown argument: ...``. Reading the signature therefore answers both
    questions that matter here: which legacy names (``use_gpu``, ``use_angle_cls``) may be
    sent, and whether the 3.x common arguments may be sent at all.
    """

    try:
        parameters = inspect.signature(paddleocr.__init__).parameters
    except (TypeError, ValueError):  # pragma: no cover - exotic/compiled callables
        return PADDLEOCR_COMMON_ARG_NAMES

    accepted = {
        name
        for name, parameter in parameters.items()
        if parameter.kind
        not in (inspect.Parameter.VAR_POSITIONAL, inspect.Parameter.VAR_KEYWORD)
    }
    if any(
        parameter.kind is inspect.Parameter.VAR_KEYWORD
        for parameter in parameters.values()
    ):
        accepted |= PADDLEOCR_PORTABLE_ARG_NAMES
    return frozenset(accepted)


def _wants_gpu(device: str) -> bool:
    """Translate a device string into the boolean PaddleOCR 2.x expects."""

    return device.strip().lower() not in ("", DEFAULT_OCR_DEVICE)


def _engine_init_kwargs(
    paddleocr: Any,
    *,
    language: str,
    device: str,
    min_confidence: float,
    enable_mkldnn: bool,
    cpu_threads: int,
    det_model: str,
    rec_model: str,
) -> dict[str, Any]:
    """Build the PaddleOCR constructor keywords for the installed version.

    Every option is described by all the names it has had across releases, and the first
    name the installed signature accepts wins; an option no name fits is left out. That is
    what keeps the 2.x-only ``use_gpu`` away from PaddleOCR 3.x, which replaced it with
    ``device`` and rejects everything it does not know.

    PaddleOCR's document pre-processing (orientation classification and unwarping) is
    switched off: the fallback OCRs a page we rasterised ourselves, which is already
    upright and flat, and on a CPU-only machine those two extra models dominate the
    runtime of a page. Text-line orientation stays on, because it is cheap and helps with
    a sideways line inside an otherwise upright page.

    ``lang`` and the explicit model names are mutually exclusive - PaddleOCR ignores (and
    warns about) the language once models are named - so the language is only sent when no
    model name could be applied.
    """

    accepted = _accepted_init_parameters(paddleocr)
    kwargs: dict[str, Any] = {}
    for name, value in (
        ("text_detection_model_name", det_model),
        ("text_recognition_model_name", rec_model),
    ):
        if value and name in accepted:
            kwargs[name] = value
    if "lang" in accepted and not kwargs:
        kwargs["lang"] = language
    for name in ("use_textline_orientation", "use_angle_cls"):
        if name in accepted:
            kwargs[name] = True
            break
    for name in ("device", "use_gpu"):
        if name in accepted:
            kwargs[name] = device if name == "device" else _wants_gpu(device)
            break
    if "enable_mkldnn" in accepted:
        kwargs["enable_mkldnn"] = enable_mkldnn
    if "cpu_threads" in accepted:
        kwargs["cpu_threads"] = cpu_threads
    for name in ("use_doc_orientation_classify", "use_doc_unwarping"):
        if name in accepted:
            kwargs[name] = False
    if min_confidence > 0:
        for name in ("text_rec_score_thresh", "drop_score"):
            if name in accepted:
                kwargs[name] = min_confidence
                break
    return kwargs


def _rejected_argument(exc: BaseException) -> str | None:
    """Return the keyword PaddleOCR refused, or None when `exc` is a different problem."""

    match = REJECTED_ARGUMENT_PATTERN.search(str(exc))
    if match is None:
        return None
    return match.group(1) or match.group(2)


def _init_failure(exc: BaseException) -> OcrError:
    """Describe a constructor failure with the hint that actually helps.

    A GPU is never required: the fallback asks for the configured device (``cpu`` by
    default), so the message points at the device setting, the PaddlePaddle runtime, and
    the one-time model download that a first OCR run performs.
    """

    return OcrError(
        "PaddleOCR could not be initialised "
        f"({type(exc).__name__}: {exc}). Check that paddlepaddle is installed, that the "
        "OCR models can be downloaded once (the first run fetches them), and that "
        f"'{DEFAULT_OCR_DEVICE}' is a usable device for this machine "
        "(VAT_AUDIT_OCR_DEVICE); no GPU is required."
    )


class PaddleOcrEngine(OcrEngine):
    """Version-tolerant adapter around PaddleOCR (2.x and 3.x).

    The engine object is created lazily and cached by :func:`get_ocr_engine`, because
    constructing PaddleOCR loads (and possibly downloads) its models.
    """

    label = PADDLEOCR_LABEL

    def __init__(
        self,
        *,
        language: str = "en",
        min_confidence: float = 0.0,
        device: str = DEFAULT_OCR_DEVICE,
        enable_mkldnn: bool = DEFAULT_OCR_ENABLE_MKLDNN,
        cpu_threads: int = DEFAULT_OCR_CPU_THREADS,
        det_model: str = DEFAULT_OCR_DET_MODEL,
        rec_model: str = DEFAULT_OCR_REC_MODEL,
    ) -> None:
        """Create the adapter.

        Args:
            language: PaddleOCR language code (``en`` for Latin-script invoices); ignored
                when model names are configured.
            min_confidence: Lines below this recognition score are dropped; raise it
                to suppress noise from poor scans.
            device: Inference device for PaddleOCR 3.x (``cpu``, ``gpu:0``, ``npu``,
                ...). It defaults to ``cpu`` because a GPU is optional; on a PaddleOCR
                2.x install the same setting is translated into ``use_gpu``.
            enable_mkldnn: Allow PaddleOCR's oneDNN acceleration. It is off by default
                because PaddlePaddle 3.x raises ``NotImplementedError`` inside oneDNN on
                some CPU builds, which would fail every page.
            cpu_threads: Threads for the CPU kernels (4 by default; PaddleOCR's own
                default of 10 oversubscribes a laptop CPU).
            det_model: Text-detection model name; empty means "let PaddleOCR choose".
            rec_model: Text-recognition model name; empty means "let PaddleOCR choose".
                It is language-specific, so it pairs with ``language``.
        """

        self.language = language
        self.min_confidence = min_confidence
        self.device = device
        self.enable_mkldnn = enable_mkldnn
        self.cpu_threads = cpu_threads
        self.det_model = det_model
        self.rec_model = rec_model
        self._engine: Any | None = None

    def _resolve_engine(self) -> Any:
        """Build the underlying PaddleOCR object once."""

        if self._engine is None:
            paddleocr = _load_paddleocr()
            self._engine = self._build(paddleocr)
        return self._engine

    def _build(self, paddleocr: Any) -> Any:
        """Instantiate PaddleOCR with keywords this version actually accepts.

        PaddleOCR changed its constructor between majors: 3.x replaced ``use_gpu`` with
        ``device``, removed ``show_log``, and rejects every keyword it does not know with
        ``ValueError: Unknown argument: <name>``. The keywords are therefore derived from
        the installed signature (see :func:`_engine_init_kwargs`), and if a version still
        refuses one - for example after a future rename - only that keyword is dropped and
        the constructor retried, so a version difference degrades into a missing option
        instead of a failed upload.
        """

        kwargs = _engine_init_kwargs(
            paddleocr,
            language=self.language,
            device=self.device,
            min_confidence=self.min_confidence,
            enable_mkldnn=self.enable_mkldnn,
            cpu_threads=self.cpu_threads,
            det_model=self.det_model,
            rec_model=self.rec_model,
        )
        # Each attempt either succeeds, raises the real error, or drops one keyword, so
        # the loop is bounded by the number of keywords plus the final empty attempt.
        for _attempt in range(len(kwargs) + 1):
            try:
                return paddleocr(**kwargs)
            except Exception as exc:  # re-raised through _init_failure below
                rejected = _rejected_argument(exc)
                if rejected is None or rejected not in kwargs:
                    raise _init_failure(exc) from exc
                kwargs.pop(rejected)
        raise OcrError(  # pragma: no cover - unreachable: the loop always returns or raises
            "PaddleOCR could not be initialised: no supported constructor signature."
        )

    def recognise(self, image: Any) -> list[OcrLine]:
        """Run OCR on one rendered page image and return the recognised lines."""

        engine = self._resolve_engine()
        result = self._invoke(engine, self._to_array(image))
        return [
            line
            for line in normalise_paddle_result(result)
            if line.text.strip() and line.confidence >= self.min_confidence
        ]

    @staticmethod
    def _to_array(image: Any) -> Any:
        """Convert a PIL image into the contiguous BGR array PaddleOCR expects."""

        try:
            import numpy as np
        except Exception as exc:  # pragma: no cover - numpy ships with paddlepaddle
            raise OcrError(
                "numpy is required by the PaddleOCR fallback and is not installed."
            ) from exc

        pixels = np.asarray(image.convert("RGB"))
        # PaddleOCR feeds the array to OpenCV, which expects BGR and contiguous data.
        return np.ascontiguousarray(pixels[:, :, ::-1])

    @staticmethod
    def _invoke(engine: Any, array: Any) -> Any:
        """Call whichever prediction method this PaddleOCR version exposes.

        ``predict`` is tried first because it is PaddleOCR 3.x's native API - 3.x keeps
        ``ocr`` only as a deprecated 2.x-compatibility wrapper - while a 2.x install
        exposes ``ocr`` alone. A method that exists but fails is skipped in favour of the
        next one, and the last failure is reported when none of them works.
        """

        last_error: Exception | None = None
        for name in ("predict", "ocr", "recognize"):
            method = getattr(engine, name, None)
            if not callable(method):
                continue
            try:
                return method(array)
            except Exception as exc:  # noqa: BLE001 - re-raised as OcrError below
                last_error = exc
        if last_error is not None:
            raise OcrError(
                f"PaddleOCR failed ({type(last_error).__name__}: {last_error})."
            ) from last_error
        raise OcrError(
            "PaddleOCR exposes no supported prediction method (ocr/predict/recognize)."
        )


def _mapping_get(value: Any, key: str) -> Any:
    """Read `key` from a dict-like or attribute-like result object, else None."""

    if isinstance(value, dict):
        return value.get(key)
    try:
        return value[key]
    except Exception:  # noqa: BLE001 - not subscriptable, so try the attribute form
        return getattr(value, key, None)


def _as_point(point: Any) -> tuple[float, float] | None:
    """Return one (x, y) pair from a box corner, or None when it is not a point."""

    if hasattr(point, "tolist"):  # numpy row
        try:
            point = point.tolist()
        except Exception:  # noqa: BLE001 - a broken array wrapper is not a point
            return None
    if not isinstance(point, (list, tuple)) or len(point) < 2:
        return None
    try:
        return float(point[0]), float(point[1])
    except (TypeError, ValueError):
        return None


def _iter_points(box: Any) -> list[tuple[float, float]]:
    """Normalise any box representation into a list of (x, y) float pairs."""

    if box is None:
        return []
    if hasattr(box, "tolist"):  # numpy arrays (PaddleOCR 3.x detector output)
        try:
            box = box.tolist()
        except Exception:  # noqa: BLE001 - an unusable array yields no points
            return []
    if not isinstance(box, (list, tuple)) or not box:
        return []
    if all(isinstance(value, (int, float)) for value in box) and len(box) >= 4:
        flat = [float(value) for value in box[:4]]
        return [(flat[0], flat[1]), (flat[2], flat[3])]
    points: list[tuple[float, float]] = []
    for point in box:
        pair = _as_point(point)
        if pair is not None:
            points.append(pair)
    return points


def _box_bounds(box: Any) -> tuple[float, float, float, float] | None:
    """Return (left, top, right, bottom) for a box, or None when unusable."""

    points = _iter_points(box)
    if not points:
        return None
    xs = [point[0] for point in points]
    ys = [point[1] for point in points]
    return min(xs), min(ys), max(xs), max(ys)


def _line(text: Any, box: Any, confidence: Any = 1.0) -> list[OcrLine]:
    """Build one :class:`OcrLine` from a text/box/score triple (empty when unusable)."""

    bounds = _box_bounds(box)
    if bounds is None:
        return []
    clean = " ".join(str(text).split())
    if not clean:
        return []
    try:
        score = float(confidence)
    except (TypeError, ValueError):
        score = 1.0
    left, top, right, bottom = bounds
    return [
        OcrLine(
            text=clean,
            left=left,
            top=top,
            right=right,
            bottom=bottom,
            confidence=score,
        )
    ]


def _lines_from_result(result: Any) -> list[OcrLine]:
    """Normalise one PaddleOCR result element, whatever shape it has."""

    if result is None:
        return []

    # PaddleOCR 3.x: a dict-like result (or object) carrying parallel arrays.
    texts = _mapping_get(result, "rec_texts")
    if texts is not None:
        scores = _mapping_get(result, "rec_scores") or []
        boxes = _mapping_get(result, "dt_polys")
        if boxes is None:
            boxes = _mapping_get(result, "rec_polys") or []
        lines: list[OcrLine] = []
        for index, text in enumerate(texts):
            box = boxes[index] if index < len(boxes) else None
            score = scores[index] if index < len(scores) else 1.0
            lines.extend(_line(text, box, score))
        return lines

    # PaddleOCR 3.x may wrap the payload under "res".
    nested = _mapping_get(result, "res")
    if nested is not None and nested is not result:
        return _lines_from_result(nested)

    if isinstance(result, (list, tuple)):
        # PaddleOCR 2.x: [box, (text, score)] per line.
        if (
            len(result) == 2
            and _box_bounds(result[0]) is not None
            and isinstance(result[1], (list, tuple))
            and len(result[1]) >= 2
        ):
            text, score = result[1][0], result[1][1]
            return _line(text, result[0], score)
        # Otherwise recurse (a page-level list of lines, or nested result objects).
        lines = []
        for entry in result:
            lines.extend(_lines_from_result(entry))
        return lines

    return []


def normalise_paddle_result(result: Any) -> list[OcrLine]:
    """Convert any supported PaddleOCR output into :class:`OcrLine` records.

    Handles the 2.x layout (``[[box, (text, score)], ...]``), the 3.x
    ``rec_texts``/``rec_scores``/``dt_polys`` arrays (optionally nested under ``res``)
    and objects exposing those attributes. Unknown shapes are skipped rather than
    raising, because a single odd element must not lose the whole page.
    """

    if result is None:
        return []
    if isinstance(result, (list, tuple)):
        lines: list[OcrLine] = []
        for entry in result:
            lines.extend(_lines_from_result(entry))
        return lines
    return _lines_from_result(result)


@lru_cache(maxsize=4)
def _cached_engine(
    language: str,
    device: str,
    enable_mkldnn: bool,
    cpu_threads: int,
    det_model: str,
    rec_model: str,
) -> PaddleOcrEngine | None:
    """Build (once) the PaddleOCR engine, or None when it is not installed."""

    if not is_ocr_available():
        return None
    try:
        return PaddleOcrEngine(
            language=language,
            device=device,
            enable_mkldnn=enable_mkldnn,
            cpu_threads=cpu_threads,
            det_model=det_model,
            rec_model=rec_model,
        )
    except OcrError:
        return None


def get_ocr_engine(
    language: str | None = None,
    device: str | None = None,
    enable_mkldnn: bool | None = None,
    cpu_threads: int | None = None,
    det_model: str | None = None,
    rec_model: str | None = None,
) -> OcrEngine | None:
    """Return the configured PaddleOCR fallback engine, or None when unavailable.

    ``None`` is a normal outcome, not an error: deployments without the optional OCR
    dependency keep working, and scanned pages are simply reported as needing OCR. Every
    argument defaults to the configured setting, so a machine without a GPU runs the
    portable CPU profile: ``cpu`` device, oneDNN off, four threads and the mobile models
    (see the ``VAT_AUDIT_OCR_*`` settings).
    """

    return _cached_engine(
        language or settings.ocr_language,
        device or settings.ocr_device,
        settings.ocr_enable_mkldnn if enable_mkldnn is None else enable_mkldnn,
        cpu_threads or settings.ocr_cpu_threads,
        settings.ocr_det_model if det_model is None else det_model,
        settings.ocr_rec_model if rec_model is None else rec_model,
    )


def clear_ocr_engine_cache() -> None:
    """Drop the cached engine and availability probe (used by tests and installs)."""

    _cached_engine.cache_clear()
    ocr_availability.cache_clear()


def _cluster_positions(values: Iterable[float], tolerance: float) -> list[float]:
    """Fold sorted positions into cluster centres that are within `tolerance`."""

    centres: list[float] = []
    counts: list[int] = []
    for value in sorted(values):
        if centres and value - centres[-1] <= tolerance:
            centres[-1] = (centres[-1] * counts[-1] + value) / (counts[-1] + 1)
            counts[-1] += 1
            continue
        centres.append(value)
        counts.append(1)
    return centres


def _nearest_index(values: Sequence[float], target: float) -> int:
    """Index of the value closest to `target` (0 for an empty sequence)."""

    if not values:
        return 0
    return min(range(len(values)), key=lambda index: abs(values[index] - target))


def group_lines_into_rows(
    lines: Sequence[OcrLine], *, overlap_ratio: float = DEFAULT_ROW_OVERLAP_RATIO
) -> list[list[OcrLine]]:
    """Cluster OCR lines into visual rows and sort each row left to right.

    A line joins the current row when its vertical extent overlaps the row band by at
    least `overlap_ratio` of the smaller height. This is what turns the flat list of
    OCR boxes of a scanned table into readable lines.
    """

    ordered = sorted(lines, key=lambda line: (line.centre_y, line.left))
    rows: list[list[OcrLine]] = []
    for line in ordered:
        if rows:
            current = rows[-1]
            band_top = min(item.top for item in current)
            band_bottom = max(item.bottom for item in current)
            reference = max(min(item.height for item in current), line.height, 1.0)
            overlap = min(band_bottom, line.bottom) - max(band_top, line.top)
            if overlap >= overlap_ratio * reference:
                current.append(line)
                continue
        rows.append([line])
    return [sorted(row, key=lambda line: line.left) for row in rows]


def _split_row_into_cells(
    row: Sequence[OcrLine],
    *,
    gap_ratio: float = DEFAULT_COLUMN_GAP_RATIO,
) -> list[tuple[float, str]]:
    """Split one visual row into cells at horizontal gaps, as (left, text) pairs."""

    heights = [line.height for line in row if line.height > 0]
    reference = median(heights) if heights else 10.0
    gap_threshold = gap_ratio * reference

    cells: list[list[Any]] = []  # [left, right, text]
    for line in row:
        if cells and line.left - cells[-1][1] > gap_threshold:
            cells.append([line.left, line.right, line.text])
            continue
        if cells:
            cells[-1][1] = max(cells[-1][1], line.right)
            cells[-1][2] = f"{cells[-1][2]} {line.text}".strip()
        else:
            cells.append([line.left, line.right, line.text])
    return [(float(left), str(text)) for left, _right, text in cells]


def recover_table_rows(
    lines: Sequence[OcrLine],
    *,
    min_rows: int = DEFAULT_MIN_TABLE_ROWS,
    min_columns: int = DEFAULT_MIN_TABLE_COLUMNS,
    gap_ratio: float = DEFAULT_COLUMN_GAP_RATIO,
    column_tolerance_ratio: float = DEFAULT_COLUMN_TOLERANCE_RATIO,
) -> list[list[str]]:
    """Recover a table grid from OCR line geometry.

    Columns are found by clustering the left edge of every cell, so a page that is
    simply running text (few cell splits, or fewer than `min_rows` tabular rows)
    returns ``[]`` and is left as plain text. Missing cells become empty strings, so
    every returned row has the same width.
    """

    if not lines:
        return []

    cell_rows = [
        _split_row_into_cells(row, gap_ratio=gap_ratio)
        for row in group_lines_into_rows(lines)
    ]
    tabular_rows = [cells for cells in cell_rows if len(cells) >= min_columns]
    if len(tabular_rows) < min_rows:
        return []

    heights = [line.height for line in lines if line.height > 0]
    reference = median(heights) if heights else 10.0
    boundaries = _cluster_positions(
        (left for cells in cell_rows for left, _text in cells),
        tolerance=column_tolerance_ratio * reference,
    )
    if len(boundaries) < min_columns:
        return []

    grid: list[list[str]] = []
    for cells in cell_rows:
        row_cells = [""] * len(boundaries)
        for left, text in cells:
            index = _nearest_index(boundaries, left)
            row_cells[index] = f"{row_cells[index]} {text}".strip()
        grid.append(row_cells)
    return grid


def ocr_lines_to_text(lines: Sequence[OcrLine]) -> str:
    """Flatten recognised lines into page text, one visual row per line.

    Columns inside a row are joined with ``" | "`` so the table layout survives into
    the text that the LLM field extraction reads.
    """

    rows = group_lines_into_rows(lines)
    rendered: list[str] = []
    for row in rows:
        text = COLUMN_SEPARATOR.join(line.text for line in row if line.text.strip())
        if text:
            rendered.append(text)
    return ROW_SEPARATOR.join(rendered)
