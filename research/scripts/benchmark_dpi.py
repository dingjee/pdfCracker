from __future__ import annotations

import json
import math
import re
import sys
import time
import unicodedata
from collections import Counter
from pathlib import Path

import pdfplumber
from PIL import Image
from rapidfuzz.distance import Levenshtein
from rapidocr_onnxruntime import RapidOCR


def normalize(text: str) -> str:
    text = unicodedata.normalize("NFKC", text).casefold()
    text = text.translate(str.maketrans({"’": "'", "‘": "'", "“": '"', "”": '"', "–": "-", "—": "-"}))
    return re.sub(r"\s+", " ", text).strip()


def multiset_prf(reference: str, hypothesis: str) -> tuple[float, float, float]:
    ref = Counter(re.findall(r"\w+(?:['-]\w+)*", reference))
    hyp = Counter(re.findall(r"\w+(?:['-]\w+)*", hypothesis))
    matched = sum((ref & hyp).values())
    precision = matched / sum(hyp.values()) if hyp else 0.0
    recall = matched / sum(ref.values()) if ref else 0.0
    f1 = 2 * precision * recall / (precision + recall) if precision + recall else 0.0
    return precision, recall, f1


def main(pdf_path: Path, page_number: int, image_dir: Path, out_path: Path) -> None:
    with pdfplumber.open(pdf_path) as pdf:
        reference = normalize(pdf.pages[page_number - 1].extract_text(x_tolerance=2, y_tolerance=2) or "")

    engine = RapidOCR()
    rows = []
    for path in sorted(image_dir.glob(f"page{page_number:03d}_*dpi.png"), key=lambda p: int(re.search(r"_(\d+)dpi", p.name).group(1))):
        dpi = int(re.search(r"_(\d+)dpi", path.name).group(1))
        start = time.perf_counter()
        result, elapsed = engine(str(path))
        wall_seconds = time.perf_counter() - start
        result = result or []
        hypothesis = normalize("\n".join(item[1] for item in result))
        precision, recall, f1 = multiset_prf(reference, hypothesis)
        with Image.open(path) as image:
            width, height = image.size
        rows.append(
            {
                "dpi": dpi,
                "width_px": width,
                "height_px": height,
                "file_bytes": path.stat().st_size,
                "gpt_5_6_original_patch_units": math.ceil(width / 32) * math.ceil(height / 32),
                "ocr_lines": len(result),
                "mean_confidence": round(sum(float(item[2]) for item in result) / len(result), 5) if result else 0.0,
                "character_similarity_including_spaces": round(Levenshtein.normalized_similarity(reference, hypothesis), 5),
                "word_precision_multiset": round(precision, 5),
                "word_recall_multiset": round(recall, 5),
                "word_f1_multiset": round(f1, 5),
                "ocr_wall_seconds": round(wall_seconds, 3),
                "engine_elapsed": elapsed,
            }
        )

    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(
        json.dumps(
            {
                "pdf": str(pdf_path),
                "page": page_number,
                "reference_chars": len(reference),
                "reference_words": len(reference.split()),
                "rows": rows,
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )


if __name__ == "__main__":
    main(Path(sys.argv[1]), int(sys.argv[2]), Path(sys.argv[3]), Path(sys.argv[4]))
