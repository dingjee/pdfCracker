"""Structural layout-role detection for scanned pages via PP-DocLayoutV3.

Optional heavy-dependency module (group ``ocr-layout``). Importing is always
safe; ``paddlex`` is imported lazily in :func:`load_predictor`.

Scope: segment one scanned page image into layout regions and map native
PP-DocLayoutV3 labels to conservative roles. This module never runs OCR,
deletes content, infers fonts, or fabricates text: raw labels and detector
JSON are preserved verbatim. Scores are confidence telemetry, not measured
accuracy -- ambiguous regions (footnote vs footer, generic ``number``,
unmapped labels, low scores) are flagged ``review_required``.

Reading order: PP-DocLayoutV3 natively orders content regions (``order``
field); page furniture gets ``null``. Native-ordered regions come first,
remaining regions are appended in a deterministic top-to-bottom /
left-to-right band order (policy recorded in ``model.reading_order_source``).

Usage (reuse one predictor across a pilot run)::

    from pdfcracker.ocr_layout import detect_layout, load_predictor

    predictor = load_predictor()          # or load_predictor(model_dir)
    page = detect_layout(image_path, predictor=predictor)
"""

from __future__ import annotations

import hashlib
import json
import os
import time
from functools import lru_cache
from pathlib import Path
from typing import Any

from PIL import Image

MODEL_NAME = "PP-DocLayoutV3"
DEVICE = "cpu"
LOW_SCORE_REVIEW = 0.5
MODEL_ARTIFACTS = ("inference.json", "inference.pdiparams", "inference.yml")
_INSTALL_HINT = (
    "layout detection requires the optional 'ocr-layout' dependency group; "
    "install it with `uv sync --extra ocr-layout`"
)

# Conservative PP-DocLayout label -> structural role map. Labels absent from
# this table map to role "unknown" (review flagged); the raw label string is
# always preserved verbatim on the region.
_ROLES_BY_GROUP: dict[str, tuple[str, ...]] = {
    "heading": ("paragraph_title", "doc_title", "chapter_title", "title"),
    "paragraph": ("text", "text_block", "content", "body_text", "abstract"),
    "footnote": ("footnote",),
    "header": ("header", "running_header"),
    "footer": ("footer", "running_footer"),
    "page_number": ("page_number", "page_num", "folio", "number"),
    "list_item": ("list", "list_item", "key_point"),
    "table": ("table",),
    "caption": ("figure_title", "figure_caption", "table_caption", "caption"),
}
ROLE_BY_LABEL: dict[str, str] = {
    label: role for role, labels in _ROLES_BY_GROUP.items() for label in labels
}
ALLOWED_ROLES = frozenset(ROLE_BY_LABEL.values()) | {"unknown"}


def load_predictor(model_dir: Path | str | None = None) -> Any:
    """Create a reusable PP-DocLayoutV3 CPU predictor.

    With ``model_dir`` (explicitly local runtime), paddlex's online
    model-source connectivity check is disabled; with ``None``, network
    model download from the official source stays permitted.
    """
    try:
        from paddlex import create_model
    except ImportError as exc:
        raise RuntimeError(_INSTALL_HINT) from exc
    kwargs: dict[str, Any] = {"model_name": MODEL_NAME, "device": DEVICE}
    if model_dir is not None:
        os.environ.setdefault("PADDLE_PDX_DISABLE_MODEL_SOURCE_CHECK", "True")
        kwargs["model_dir"] = str(model_dir)
    try:
        return create_model(**kwargs)
    except TypeError:
        kwargs.pop("device", None)
        return create_model(**kwargs)


def detect_layout(
    image_path: Path | str,
    model_dir: Path | str | None = None,
    threshold: float = 0.25,
    *,
    predictor: Any | None = None,
) -> dict[str, Any]:
    """Detect structural layout regions for one page image.

    Returns a JSON-serializable dict: ``width``/``height``, ``model`` info,
    ``latency_ms``, one region per detector box (id, label, role, pixel
    ``bbox``, ``bbox_norm``, confidence, evidence, ``reading_order``,
    ``review_required``) and the detector JSON under ``raw``. No detector
    box is ever dropped: unparseable boxes are retained with a placeholder
    bbox and a review flag.

    Because a ``model_dir=None`` run may download a mutable remote detector,
    ``model`` also records ``resolved_model_dir`` (the directory actually
    used, e.g. the paddlex official-model cache) plus ``artifacts_sha256``
    for the inference artifacts, so every output is pinned to the exact
    bytes that produced it. ``threshold_applied`` is ``false`` when the
    predictor rejected the ``threshold`` runtime argument.
    """
    started = time.perf_counter()
    width, height = _image_size(Path(image_path))
    api = "caller-supplied"
    if predictor is None:
        predictor = load_predictor(model_dir)
        api = "paddlex.create_model"
    data, threshold_applied = _predict_json(predictor, str(image_path), threshold)
    regions: list[dict[str, Any]] = []
    native: list[int | None] = []
    for idx, box in enumerate(_boxes_from(data)):
        region, hint = _build_region(
            idx, box if isinstance(box, dict) else {}, width, height, threshold
        )
        regions.append(region)
        native.append(hint)
    _assign_reading_order(regions, native)
    has_native = any(hint is not None for hint in native)
    resolved_dir = _resolve_model_dir(model_dir, predictor)
    return {
        "image_path": str(image_path),
        "width": width,
        "height": height,
        "model": {
            "name": MODEL_NAME,
            "runtime": "paddlex",
            "device": DEVICE,
            "api": api,
            "model_dir": str(model_dir) if model_dir is not None else None,
            "resolved_model_dir": (
                str(resolved_dir) if resolved_dir is not None else None
            ),
            "artifacts_sha256": {
                name: _artifact_hash(resolved_dir, name) for name in MODEL_ARTIFACTS
            },
            "threshold": threshold,
            "threshold_applied": threshold_applied,
            "reading_order_source": (
                "native_with_band_fallback" if has_native else "heuristic_band_sort"
            ),
            "score_semantics": "detector confidence, not measured accuracy",
        },
        "latency_ms": round((time.perf_counter() - started) * 1000, 2),
        "region_count": len(regions),
        "regions": regions,
        "raw": _serializable_raw(data),
    }


def _image_size(path: Path) -> tuple[int, int]:
    with Image.open(path) as image:
        return int(image.width), int(image.height)


def _resolve_model_dir(model_dir: Path | str | None, predictor: Any) -> Path | None:
    """Best-effort directory actually backing the detector (metadata only).

    Priority: explicit ``model_dir``, then the predictor's own ``model_dir``
    attribute (set by paddlex runners), then the paddlex official-model
    cache (honoring ``PADDLE_PDX_CACHE_HOME``). Never imports paddlex.
    """
    if model_dir is not None:
        return Path(model_dir).expanduser().resolve()
    predictor_dir = getattr(predictor, "model_dir", None)
    if predictor_dir:
        return Path(predictor_dir).expanduser().resolve()
    cache_home = os.environ.get("PADDLE_PDX_CACHE_HOME")
    base = Path(cache_home).expanduser() if cache_home else Path.home() / ".paddlex"
    candidate = (base / "official_models" / MODEL_NAME).resolve()
    return candidate if candidate.is_dir() else None


@lru_cache(maxsize=64)
def _artifact_sha256(
    resolved_dir: str, name: str, mtime_ns: int, size: int
) -> str | None:
    """SHA256 of one inference artifact; keyed by dir+mtime+size (cached)."""
    digest = hashlib.sha256()
    with (Path(resolved_dir) / name).open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _artifact_hash(model_dir: Path | None, name: str) -> str | None:
    if model_dir is None:
        return None
    path = model_dir / name
    if not path.is_file():
        return None
    stat = path.stat()
    return _artifact_sha256(
        str(model_dir.resolve()), name, stat.st_mtime_ns, stat.st_size
    )


def _predict_json(
    predictor: Any, image_path: str, threshold: float
) -> tuple[dict[str, Any] | None, bool]:
    """Run predict, returning (result JSON, threshold_applied).

    Only an "unexpected keyword argument 'threshold'" TypeError falls back
    to a threshold-less call; any other TypeError (bad input data, API
    mismatch) propagates untouched.
    """
    try:
        outputs = list(predictor.predict(image_path, threshold=threshold))
        return _first_output_json(outputs), True
    except TypeError as exc:
        message = str(exc)
        if "unexpected keyword" not in message or "threshold" not in message:
            raise
        outputs = list(predictor.predict(image_path))
        return _first_output_json(outputs), False


def _first_output_json(outputs: list[Any]) -> dict[str, Any] | None:
    if not outputs:
        return None
    result = outputs[0]
    if isinstance(result, dict):
        return result
    for attr in ("json", "to_json"):
        value = getattr(result, attr, None)
        if value is None:
            continue
        value = value() if callable(value) else value
        if isinstance(value, dict):
            return value
    return None


def _boxes_from(data: dict[str, Any] | None) -> list[Any]:
    if not isinstance(data, dict):
        return []
    containers = [data]
    for key in ("res", "result"):
        nested = data.get(key)
        if isinstance(nested, dict):
            containers.append(nested)
    for container in containers:
        boxes = container.get("boxes")
        if isinstance(boxes, list):
            return boxes
    return []


def _serializable_raw(data: dict[str, Any] | None) -> dict[str, Any]:
    if data is None:
        return {"note": "detector returned no JSON view"}
    try:
        json.dumps(data, ensure_ascii=False, allow_nan=False)
    except (TypeError, ValueError) as exc:
        return {
            "note": f"detector JSON view not serializable ({exc.__class__.__name__})"
        }
    return data


def _build_region(
    idx: int, box: dict[str, Any], width: int, height: int, threshold: float
) -> tuple[dict[str, Any], int | None]:
    label_raw = box.get("label", box.get("cls_name", box.get("category")))
    label = str(label_raw).strip().lower() if label_raw is not None else ""
    score = _as_float(box.get("score", box.get("confidence")), 0.0)
    raw_coord = box.get("coordinate", box.get("bbox", box.get("box")))
    if raw_coord is None:
        raw_coord = box.get("polygon_points")
    bbox, malformed = _pixel_bbox(raw_coord, width, height)
    role = ROLE_BY_LABEL.get(label, "unknown")
    reasons = [
        (role == "unknown", "label outside conservative map; kept unknown"),
        (role == "footnote", "footnote/footer ambiguity; score is not accuracy"),
        (label == "number", "generic 'number' label; page-number role is tentative"),
        (label == "key_point", "'key_point' list-item role is tentative"),
        (score < threshold, f"score below threshold {threshold:.2f}"),
        (threshold <= score < LOW_SCORE_REVIEW, f"low score (<{LOW_SCORE_REVIEW:.2f})"),
        (malformed, "malformed coordinate; placeholder bbox"),
    ]
    notes = [note for hit, note in reasons if hit]
    review = bool(notes)
    native_order = _native_order(box)
    evidence = f"label={label!r}; detector score {score:.3f}"
    if native_order is not None:
        evidence += f"; native order {native_order}"
    else:
        evidence += "; no native order; band placement"
    if notes:
        evidence += "; " + "; ".join(notes)
    region: dict[str, Any] = {
        "id": idx,
        "label": str(label_raw) if label_raw is not None else "",
        "role": role,
        "bbox": bbox,
        "bbox_norm": _norm_bbox(bbox, width, height),
        "confidence": round(score, 4),
        "evidence": evidence,
        "reading_order": -1,
        "review_required": review,
    }
    return region, native_order


def _native_order(box: dict[str, Any]) -> int | None:
    value = box.get("order", box.get("reading_order"))
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return int(value)


def _as_float(value: Any, default: float) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _pixel_bbox(raw: Any, width: int, height: int) -> tuple[list[int], bool]:
    nums: list[float] = []
    if isinstance(raw, (list, tuple)):
        flat = [
            v
            for item in raw
            for v in (item if isinstance(item, (list, tuple)) else (item,))
        ]
        try:
            nums = [float(v) for v in flat]
        except (TypeError, ValueError):
            nums = []
    if len(nums) == 4:
        x1, y1, x2, y2 = nums
    elif len(nums) >= 8 and len(nums) % 2 == 0:
        xs, ys = nums[0::2], nums[1::2]
        x1, x2, y1, y2 = min(xs), max(xs), min(ys), max(ys)
    else:
        return [0, 0, 0, 0], True
    x_lo = int(round(min(max(x1, 0.0), float(width))))
    x_hi = int(round(min(max(x2, 0.0), float(width))))
    y_lo = int(round(min(max(y1, 0.0), float(height))))
    y_hi = int(round(min(max(y2, 0.0), float(height))))
    return [min(x_lo, x_hi), min(y_lo, y_hi), max(x_lo, x_hi), max(y_lo, y_hi)], False


def _norm_bbox(bbox: list[int], width: int, height: int) -> list[float]:
    if width <= 0 or height <= 0:
        return [0.0, 0.0, 0.0, 0.0]
    x0, y0, x1, y1 = bbox
    return [
        round(min(v / s, 1.0), 5)
        for v, s in ((x0, width), (y0, height), (x1, width), (y1, height))
    ]


def _band_order(boxes: list[list[int]]) -> list[int]:
    """Deterministic reading order: row bands top-to-bottom, left-to-right."""
    by_y = sorted(range(len(boxes)), key=lambda i: (boxes[i][1], boxes[i][0]))
    bands: list[list[Any]] = []
    for i in by_y:
        y0, y1 = boxes[i][1], boxes[i][3]
        if bands and y0 < bands[-1][0]:
            bands[-1][1].append(i)
            bands[-1][0] = max(bands[-1][0], y1)
        else:
            bands.append([y1, [i]])
    order: list[int] = []
    for _, members in bands:
        order.extend(sorted(members, key=lambda i: (boxes[i][0], boxes[i][1])))
    return order


def _assign_reading_order(
    regions: list[dict[str, Any]], native: list[int | None]
) -> None:
    if not regions:
        return
    ordered = sorted(
        (i for i, hint in enumerate(native) if hint is not None),
        key=lambda i: (int(native[i]), i),
    )
    rest = [i for i, hint in enumerate(native) if hint is None]
    rest = [rest[k] for k in _band_order([regions[i]["bbox"] for i in rest])]
    for rank, i in enumerate(ordered + rest):
        regions[i]["reading_order"] = rank
