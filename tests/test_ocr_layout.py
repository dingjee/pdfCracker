"""Tests for pdfcracker.ocr_layout — all use fake predictors, no model download."""

from __future__ import annotations

import hashlib
import json
import os
import sys
from pathlib import Path
from typing import Any

import pytest
from PIL import Image

from pdfcracker import ocr_layout
from pdfcracker.ocr_layout import (
    ALLOWED_ROLES,
    ROLE_BY_LABEL,
    detect_layout,
    load_predictor,
)

WIDTH, HEIGHT = 800, 1300

# Mirrors runs/42ece8b2d1c6-xiaomi/pilot-inputs/page-0026-144dpi.layout-raw.json
REAL_SAMPLE_BOXES: list[dict[str, Any]] = [
    {
        "cls_id": 12,
        "label": "header",
        "score": 0.836,
        "coordinate": [621, 103, 742, 124],
        "order": None,
    },
    {
        "cls_id": 22,
        "label": "text",
        "score": 0.962,
        "coordinate": [98, 163, 744, 437],
        "order": 1,
    },
    {
        "cls_id": 22,
        "label": "text",
        "score": 0.967,
        "coordinate": [97, 449, 744, 802],
        "order": 2,
    },
    {
        "cls_id": 22,
        "label": "text",
        "score": 0.958,
        "coordinate": [97, 814, 744, 998],
        "order": 3,
    },
    {
        "cls_id": 17,
        "label": "paragraph_title",
        "score": 0.857,
        "coordinate": [141, 1029, 259, 1057],
        "order": 4,
    },
    {
        "cls_id": 22,
        "label": "text",
        "score": 0.845,
        "coordinate": [138, 1087, 741, 1113],
        "order": 5,
    },
    {
        "cls_id": 10,
        "label": "footnote",
        "score": 0.947,
        "coordinate": [98, 1161, 742, 1235],
        "order": None,
    },
    {
        "cls_id": 2,
        "label": "aside_text",
        "score": 0.785,
        "coordinate": [766, 248, 792, 271],
        "order": None,
    },
    {
        "cls_id": 8,
        "label": "footer",
        "score": 0.330,
        "coordinate": [657, 1278, 685, 1293],
        "order": None,
    },
    {
        "cls_id": 16,
        "label": "number",
        "score": 0.741,
        "coordinate": [718, 1276, 741, 1293],
        "order": 6,
    },
]


class _FakeResult:
    def __init__(self, payload: dict[str, Any]) -> None:
        self._payload = payload

    @property
    def json(self) -> dict[str, Any]:
        return self._payload


class _FakePredictor:
    """Fake paddlex predictor returning {"res": {"boxes": [...]}} like paddlex 3.7."""

    def __init__(self, boxes: list[dict[str, Any]]) -> None:
        self._payload = {"res": {"input_path": "fake.png", "boxes": boxes}}
        self.received_threshold: Any = None

    def predict(self, image: str, threshold: float | None = None, **kwargs: Any):
        self.received_threshold = threshold
        return iter([_FakeResult(self._payload)])


class _StrictPredictor(_FakePredictor):
    def predict(self, image: str):  # no threshold kwarg -> TypeError fallback path
        self.received_threshold = "fallback"
        return iter([_FakeResult(self._payload)])


def _page_png(tmp_path: Path, name: str = "page.png") -> Path:
    path = tmp_path / name
    Image.new("RGB", (WIDTH, HEIGHT), "white").save(path)
    return path


def _run(tmp_path: Path, boxes: list[dict[str, Any]], **kwargs: Any) -> dict[str, Any]:
    return detect_layout(
        _page_png(tmp_path),
        threshold=kwargs.pop("threshold", 0.25),
        predictor=_FakePredictor(boxes),
        **kwargs,
    )


def test_role_map_is_conservative_and_complete() -> None:
    assert set(ROLE_BY_LABEL.values()) <= ALLOWED_ROLES
    expected = {
        "paragraph_title": "heading",
        "doc_title": "heading",
        "text": "paragraph",
        "content": "paragraph",
        "footnote": "footnote",
        "header": "header",
        "footer": "footer",
        "page_number": "page_number",
        "number": "page_number",
        "list": "list_item",
        "key_point": "list_item",
        "table": "table",
        "figure_title": "caption",
        "table_caption": "caption",
    }
    for label, role in expected.items():
        assert ROLE_BY_LABEL[label] == role, label


def test_real_sample_all_regions_retained_and_ordered(tmp_path: Path) -> None:
    result = _run(tmp_path, REAL_SAMPLE_BOXES)
    regions = result["regions"]
    assert result["region_count"] == len(REAL_SAMPLE_BOXES) == len(regions)
    assert sorted(r["id"] for r in regions) == list(range(len(REAL_SAMPLE_BOXES)))
    assert [r["label"] for r in regions] == [b["label"] for b in REAL_SAMPLE_BOXES]
    roles = {r["label"]: r["role"] for r in regions}
    assert roles["header"] == "header"
    assert roles["text"] == "paragraph"
    assert roles["paragraph_title"] == "heading"
    assert roles["footnote"] == "footnote"
    assert roles["footer"] == "footer"
    assert roles["number"] == "page_number"
    assert roles["aside_text"] == "unknown"
    assert result["model"]["reading_order_source"] == "native_with_band_fallback"
    orders = sorted(r["reading_order"] for r in regions)
    assert orders == list(range(len(REAL_SAMPLE_BOXES)))
    ranks = {r["id"]: r["reading_order"] for r in regions}
    # Native content flow first (order 1..6), furniture appended band-sorted.
    assert [ranks[i] for i in (1, 2, 3, 4, 5, 9)] == [0, 1, 2, 3, 4, 5]
    assert [ranks[i] for i in (0, 7, 6, 8)] == [
        6,
        7,
        8,
        9,
    ]  # header, aside, footnote, footer


def test_bbox_pixel_and_normalized_are_valid(tmp_path: Path) -> None:
    result = _run(tmp_path, REAL_SAMPLE_BOXES)
    for region in result["regions"]:
        x0, y0, x1, y1 = region["bbox"]
        assert 0 <= x0 <= x1 <= WIDTH
        assert 0 <= y0 <= y1 <= HEIGHT
        assert all(isinstance(v, int) for v in region["bbox"])
        nx0, ny0, nx1, ny1 = region["bbox_norm"]
        assert all(0.0 <= v <= 1.0 for v in region["bbox_norm"])
        assert nx0 == pytest.approx(round(x0 / WIDTH, 5))
        assert ny1 == pytest.approx(round(y1 / HEIGHT, 5))
    header = result["regions"][0]
    assert header["bbox"] == [621, 103, 742, 124]
    assert header["bbox_norm"] == [
        round(621 / WIDTH, 5),
        round(103 / HEIGHT, 5),
        round(742 / WIDTH, 5),
        round(124 / HEIGHT, 5),
    ]


def test_pure_heuristic_reading_order_bands(tmp_path: Path) -> None:
    boxes = [
        {"label": "header", "score": 0.9, "coordinate": [100, 10, 700, 40]},
        {"label": "text", "score": 0.9, "coordinate": [50, 200, 350, 400]},
        {"label": "text", "score": 0.9, "coordinate": [400, 200, 750, 400]},
        {"label": "paragraph_title", "score": 0.9, "coordinate": [50, 100, 300, 140]},
        {"label": "footer", "score": 0.9, "coordinate": [100, 1200, 700, 1240]},
    ]
    result = _run(tmp_path, boxes)
    assert result["model"]["reading_order_source"] == "heuristic_band_sort"
    ranks = {r["id"]: r["reading_order"] for r in result["regions"]}
    assert ranks[0] == 0  # header band first
    assert ranks[3] == 1  # title band second
    assert ranks[1] == 2 and ranks[2] == 3  # two-column band, left before right
    assert ranks[4] == 4  # footer band last


def test_review_flags_for_ambiguity(tmp_path: Path) -> None:
    result = _run(tmp_path, REAL_SAMPLE_BOXES)
    by_label = {r["label"]: r for r in result["regions"]}
    assert by_label["footnote"]["review_required"] is True
    assert "footnote" in by_label["footnote"]["evidence"]
    assert by_label["number"]["review_required"] is True
    assert by_label["aside_text"]["review_required"] is True
    assert by_label["footer"]["review_required"] is True  # score 0.33 < 0.5
    assert by_label["header"]["review_required"] is False
    assert by_label["text"]["review_required"] is False
    assert by_label["paragraph_title"]["review_required"] is False


def test_unknown_and_malformed_boxes_never_dropped(tmp_path: Path) -> None:
    boxes = [
        {
            "cls_id": 2,
            "label": "aside_text",
            "score": 0.8,
            "coordinate": [700, 50, 760, 90],
            "order": None,
        },
        {
            "cls_id": 9,
            "label": "seal",
            "score": 0.7,
            "coordinate": [300, 900, 400, 1000],
            "order": None,
        },
        {"label": "mystery_label", "score": 0.6},
        {"label": "bad_coord", "score": 0.9, "coordinate": ["oops"]},
        {
            "label": "text",
            "score": 0.95,
            "polygon_points": [[10, 10], [110, 10], [110, 50], [10, 50]],
        },
    ]
    result = _run(tmp_path, boxes)
    regions = result["regions"]
    assert len(regions) == len(boxes)  # nothing dropped
    assert {r["label"] for r in regions} == {b["label"] for b in boxes}
    unknown = [r for r in regions if r["role"] == "unknown"]
    assert {r["label"] for r in unknown} == {
        "aside_text",
        "seal",
        "mystery_label",
        "bad_coord",
    }
    assert all(r["review_required"] for r in unknown)
    malformed = next(r for r in regions if r["label"] == "bad_coord")
    assert malformed["bbox"] == [0, 0, 0, 0]
    assert "malformed coordinate" in malformed["evidence"]
    polygon = next(
        r for r in regions if r["label"] == "text" and r["bbox"] == [10, 10, 110, 50]
    )
    assert polygon["bbox"] == [10, 10, 110, 50]
    assert polygon["role"] == "paragraph"
    assert polygon["review_required"] is False


def test_result_serializable_with_raw_and_latency(tmp_path: Path) -> None:
    result = _run(tmp_path, REAL_SAMPLE_BOXES)
    json.dumps(result, ensure_ascii=False)  # must not raise
    assert result["raw"]["res"]["boxes"] == REAL_SAMPLE_BOXES
    assert result["latency_ms"] >= 0
    assert result["width"] == WIDTH and result["height"] == HEIGHT
    model = result["model"]
    assert model["name"] == "PP-DocLayoutV3"
    assert model["device"] == "cpu"
    assert model["api"] == "caller-supplied"
    assert model["score_semantics"].startswith("detector confidence")
    for region in result["regions"]:
        assert isinstance(region["evidence"], str) and region["evidence"]
        assert 0.0 <= region["confidence"] <= 1.0


def test_threshold_is_forwarded_and_fallback_works(tmp_path: Path) -> None:
    path = _page_png(tmp_path)
    accepting = _FakePredictor(REAL_SAMPLE_BOXES)
    result = detect_layout(path, threshold=0.4, predictor=accepting)
    assert accepting.received_threshold == 0.4
    assert result["model"]["threshold_applied"] is True
    strict = _StrictPredictor(REAL_SAMPLE_BOXES)
    result = detect_layout(path, threshold=0.4, predictor=strict)
    assert strict.received_threshold == "fallback"
    assert result["model"]["threshold_applied"] is False
    assert len(result["regions"]) == len(REAL_SAMPLE_BOXES)


def test_data_typeerror_propagates_not_swallowed(tmp_path: Path) -> None:
    class _DataErrorPredictor:
        def predict(self, image: str, threshold: float | None = None, **kw: Any):
            raise TypeError("bad image data: expected ndarray, got list")

    path = _page_png(tmp_path, "data-error.png")
    with pytest.raises(TypeError, match="bad image data"):
        detect_layout(path, predictor=_DataErrorPredictor())


def test_unrelated_threshold_typeerror_propagates(tmp_path: Path) -> None:
    class _BrokenPredictor:
        def predict(self, image: str, threshold: float | None = None, **kw: Any):
            raise TypeError("unsupported operand type(s) for +: 'int' and 'str'")

    path = _page_png(tmp_path, "broken.png")
    with pytest.raises(TypeError, match="unsupported operand"):
        detect_layout(path, predictor=_BrokenPredictor())


def _write_model_artifacts(model_root: Path) -> dict[str, bytes]:
    model_root.mkdir(parents=True, exist_ok=True)
    contents = {
        "inference.json": b'{"Arch": {"name": "PP-DocLayoutV3"}}',
        "inference.pdiparams": b"\x00\x01\x02fake-weights",
        "inference.yml": b"Global:\n  model_name: PP-DocLayoutV3\n",
    }
    for name, data in contents.items():
        (model_root / name).write_bytes(data)
    return contents


def test_resolved_cache_dir_and_artifact_sha256(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    model_root = tmp_path / "official_models" / "PP-DocLayoutV3"
    contents = _write_model_artifacts(model_root)
    monkeypatch.setenv("PADDLE_PDX_CACHE_HOME", str(tmp_path))
    result = _run(tmp_path, REAL_SAMPLE_BOXES)
    model = result["model"]
    assert model["resolved_model_dir"] == str(model_root.resolve())
    for name, data in contents.items():
        assert model["artifacts_sha256"][name] == hashlib.sha256(data).hexdigest()
    # Missing artifact records None instead of a fabricated digest.
    (model_root / "inference.yml").unlink()
    result = _run(tmp_path, REAL_SAMPLE_BOXES)
    assert result["model"]["artifacts_sha256"]["inference.yml"] is None
    assert result["model"]["artifacts_sha256"]["inference.json"] is not None
    # Empty cache -> no resolved dir, no fabricated hashes.
    monkeypatch.setenv("PADDLE_PDX_CACHE_HOME", str(tmp_path / "absent"))
    result = _run(tmp_path, REAL_SAMPLE_BOXES)
    assert result["model"]["resolved_model_dir"] is None
    assert all(v is None for v in result["model"]["artifacts_sha256"].values())


def test_explicit_model_dir_recorded_and_hashed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    model_root = tmp_path / "anywhere" / "detector"
    contents = _write_model_artifacts(model_root)
    monkeypatch.delenv("PADDLE_PDX_CACHE_HOME", raising=False)
    result = detect_layout(
        _page_png(tmp_path, "explicit.png"),
        model_dir=model_root,
        threshold=0.25,
        predictor=_FakePredictor(REAL_SAMPLE_BOXES),
    )
    assert result["model"]["resolved_model_dir"] == str(model_root.resolve())
    assert result["model"]["artifacts_sha256"]["inference.pdiparams"] == (
        hashlib.sha256(contents["inference.pdiparams"]).hexdigest()
    )


def test_artifact_hashes_cached_until_file_changes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    model_root = tmp_path / "official_models" / "PP-DocLayoutV3"
    _write_model_artifacts(model_root)
    monkeypatch.setenv("PADDLE_PDX_CACHE_HOME", str(tmp_path))
    ocr_layout._artifact_sha256.cache_clear()
    _run(tmp_path, REAL_SAMPLE_BOXES)
    assert ocr_layout._artifact_sha256.cache_info().misses == 3  # one per artifact
    assert ocr_layout._artifact_sha256.cache_info().hits == 0
    _run(tmp_path, REAL_SAMPLE_BOXES)  # unchanged files -> served from cache
    info = ocr_layout._artifact_sha256.cache_info()
    assert info.hits == 3 and info.misses == 3
    os.utime(model_root / "inference.json", (1_700_000_000, 1_700_000_000))
    _run(tmp_path, REAL_SAMPLE_BOXES)  # changed mtime -> re-hashed
    info = ocr_layout._artifact_sha256.cache_info()
    assert info.misses == 4 and info.hits == 5


def test_missing_dependency_raises_install_hint(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setitem(sys.modules, "paddlex", None)
    with pytest.raises(RuntimeError, match="ocr-layout"):
        load_predictor()
    with pytest.raises(RuntimeError, match="ocr-layout"):
        detect_layout(_page_png(tmp_path, "dep.png"))
