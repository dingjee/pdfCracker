"""Audit a scan chapter's page coverage, OCR provenance and delivered images."""
from __future__ import annotations

import argparse
from collections import Counter
from hashlib import sha256
import json
from pathlib import Path
import re

from pdfcracker.markdown_content import image_references


def audit(root: Path, expected_pages: list[int] | None) -> dict:
    document = json.loads((root / "document.json").read_text(encoding="utf-8"))
    markdown = (root / "chapter.md").read_text(encoding="utf-8")
    pages = document["pages"]
    numbers = [row["page"] for row in pages]
    errors = []
    if not numbers or numbers != (expected_pages or list(range(numbers[0], numbers[-1] + 1))):
        errors.append("Selected PDF pages are incomplete, unordered or repeated.")
    if not re.fullmatch(r"[0-9a-f]{64}", document["source_sha256"]):
        errors.append("Source PDF hash is missing or invalid.")
    if sha256(markdown.encode("utf-8")).hexdigest() != document["markdown_sha256"]:
        errors.append("Markdown hash differs from the sidecar.")

    rendered = Counter()
    for paragraph in document["paragraphs"]:
        start, end = paragraph["output_char_start"], paragraph["output_char_end"]
        if markdown[start:end] != paragraph["text"]:
            errors.append(f"Markdown differs from paragraph record {paragraph['index']}.")
        rendered.update(paragraph["source_block_ids"])

    page_labels = {row["page"]: row["printed_page"] for row in pages}
    for row in pages:
        accounted = Counter(
            source
            for block in row["structured_blocks"]
            if rendered[block["id"]] == 1
            for source in block["ocr_source_ids"]
        )
        for tombstone in row["tombstones"]:
            accounted.update(tombstone["ocr_source_ids"])
        sources = Counter(row["source_block_ids"])
        if (
            row["status"] != "ok" or not sources or max(sources.values()) != 1
            or accounted != sources or accounted != Counter(row["accounted_block_ids"])
        ):
            errors.append(f"PDF page {row['page']} has missing, repeated or unaccounted OCR blocks.")

    image_dir = root / "images"
    if image_dir.is_symlink():
        errors.append("Image directory is a symlink.")
    figure_paths = Counter()
    figure_numbers = Counter()
    for figure in document["figures"]:
        number = figure["number"]
        figure_numbers[number] += 1
        expected_path = f"images/p{figure['printed_page']:04d}-fig-{number}.png"
        path = figure["asset_path"]
        figure_paths[path] += 1
        if (
            not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]*", number)
            or path != expected_path
            or page_labels.get(figure["page"]) != figure["printed_page"]
        ):
            errors.append(f"Figure {number!r} has an invalid path or page mapping.")
            continue
        asset = root / path
        if image_dir.is_symlink() or asset.is_symlink() or not asset.is_file():
            errors.append(f"Image missing or unsafe: {path}")
        elif sha256(asset.read_bytes()).hexdigest() != figure["sha256"]:
            errors.append(f"Image hash differs from the sidecar: {path}")
    delivered_paths = Counter(ref.source for ref in image_references(markdown))
    if delivered_paths != figure_paths or any(count != 1 for count in figure_numbers.values()):
        errors.append("Markdown image links and unique manifest figures do not match.")
    if Counter(f"images/{asset.name}" for asset in image_dir.glob("*.png")) != figure_paths:
        errors.append("The images directory differs from the figure manifest.")

    return {
        "status": "failed" if errors else "passed",
        "pdf_pages": numbers,
        "ocr_blocks": sum(len(page["source_block_ids"]) for page in pages),
        "images": len(figure_paths),
        "errors": errors,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("root", type=Path)
    parser.add_argument("--pages", metavar="FIRST-LAST")
    args = parser.parse_args()
    expected = None
    if args.pages is not None:
        match = re.fullmatch(r"([1-9]\d*)-([1-9]\d*)", args.pages)
        if not match or int(match[1]) > int(match[2]):
            parser.error("--pages requires a positive ascending range")
        expected = list(range(int(match[1]), int(match[2]) + 1))
    try:
        result = audit(args.root, expected)
    except (OSError, ValueError, TypeError, KeyError) as error:
        result = {"status": "failed", "errors": [str(error)]}
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return int(result["status"] != "passed")


if __name__ == "__main__":
    raise SystemExit(main())
