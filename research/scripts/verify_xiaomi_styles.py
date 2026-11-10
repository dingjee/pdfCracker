"""Bounded region OCR to check heading/footnote preservation in a page pilot.

This experiment adds evidence; it never rewrites the full-page transcription.
Run: uv run --extra ocr-layout research/scripts/verify_xiaomi_styles.py DOCUMENT
"""
from __future__ import annotations

import argparse
import hashlib
import io
import json
import re
import unicodedata
from pathlib import Path

import httpx
from PIL import Image

from pdfcracker.ocr import (
    OcrOptions, build_generate_payload, call_generate, prepare_image, render_raw_prompt,
)
from pdfcracker.ocr_layout import detect_layout, load_predictor

TASK = "Extract the text in the image."


def normalize(text: str) -> str:
    return re.sub(r"[\s#*_`]+", "", unicodedata.normalize("NFKC", text))


def verify(document_path: Path, max_calls: int = 32) -> Path:
    root = document_path.parent
    document = json.loads(document_path.read_text())
    if not 1 <= len(document["pages"]) <= 24 or not 1 <= max_calls <= 64:
        raise ValueError("Region experiment requires a pilot of <=24 pages and <=64 calls")
    layouts = root / "layouts"
    evidence = root / "region-checks"
    layouts.mkdir(exist_ok=True)
    evidence.mkdir(exist_ok=True)
    predictor = load_predictor()
    candidates = []
    for page in document["pages"]:
        number = page["page"]
        image = root / page["render_path"]
        layout = detect_layout(image, predictor=predictor)
        (layouts / f"page-{number:04d}.json").write_text(json.dumps(layout, ensure_ascii=False, indent=2))
        for region in layout["regions"]:
            if region["role"] in {"heading", "footnote"} or (
                region["role"] == "header" and region["review_required"]
            ):
                candidates.append((page, region))
    if len(candidates) > max_calls:
        raise ValueError(f"{len(candidates)} region calls exceed configured cap {max_calls}")
    model = document["model"]["name"]
    options = OcrOptions(base_url=document["model"]["base_url"], model=model, max_tokens=1024)
    result = {"schema": "pdfcracker.ocr.style-checks/1", "pilot_approved": False,
              "source_sha256": document["source"]["sha256"],
              "model_digest": document["model"]["digest"], "max_calls": max_calls,
              "prompt": TASK, "regions": [], "external_api_cost_usd": 0}
    target = root / "style-evidence.json"
    with httpx.Client(timeout=300, follow_redirects=False) as client:
        for index, (page, region) in enumerate(candidates):
            number = page["page"]
            key = f"page-{number:04d}-region-{region['id']}"
            with Image.open(root / page["render_path"]) as image:
                x0, y0, x1, y1 = region["bbox"]
                if x1 <= x0 or y1 <= y0:
                    raise ValueError(f"Invalid region {key}")
                # A narrow evidence margin preserves punctuation near the detector edge.
                bbox = [max(0, x0-4), max(0, y0-4), min(image.width, x1+4), min(image.height, y1+4)]
                crop = image.crop(bbox).convert("RGB")
                # Small heading/footnote crops benefit from higher sampling while keeping
                # the source-pixel crop and all resizing parameters in the evidence.
                scale = min(2.0, 1600 / max(crop.size))
                if scale > 1:
                    crop = crop.resize((round(crop.width*scale), round(crop.height*scale)), Image.Resampling.LANCZOS)
                buffer = io.BytesIO()
                crop.save(buffer, format="PNG")
            png = prepare_image(buffer.getvalue())
            (evidence / f"{key}.png").write_bytes(png)
            signature = hashlib.sha256(json.dumps({
                "image": hashlib.sha256(png).hexdigest(), "model": result["model_digest"],
                "prompt": TASK, "options": options.as_provenance(),
            }, sort_keys=True).encode()).hexdigest()
            record_path = evidence / f"{key}.json"
            stored = json.loads(record_path.read_text()) if record_path.exists() else {}
            if stored.get("signature") == signature and stored.get("status") == "ok":
                record = stored
            else:
                outcome = call_generate(client=client, base_url=options.base_url,
                    payload=build_generate_payload(model=model, prompt=render_raw_prompt(TASK),
                                                   image_png=png, options=options), timeout=300)
                (evidence / f"{key}.raw").write_bytes(outcome.body)
                text = outcome.text
                record = {"id": key, "page": number, "region": region, "crop_bbox": bbox,
                          "crop_scale": scale, "signature": signature, "text": text,
                          "status": outcome.status, "usage": outcome.usage,
                          "latency_seconds": outcome.latency_seconds, "error": outcome.error,
                          "raw_response": f"region-checks/{key}.raw", "human_review_required": True}
            found = bool(normalize(record["text"])) and normalize(record["text"]) in normalize(page.get("text", ""))
            record["exact_normalized_match_in_page"] = found
            record["review_reason"] = ("Exact text coverage; role still requires review" if found
                                       else "Region/page OCR disagreement: check scan, omission or recognition error")
            record_path.write_text(json.dumps(record, ensure_ascii=False, indent=2))
            result["regions"].append(record)
            target.write_text(json.dumps(result, ensure_ascii=False, indent=2))
            print(f'{index+1}/{len(candidates)} {key} {region["role"]}: matched={found} {record["text"][:70]}', flush=True)
    return target


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("document", type=Path)
    parser.add_argument("--max-calls", type=int, default=32)
    args = parser.parse_args()
    print(verify(args.document, args.max_calls))
