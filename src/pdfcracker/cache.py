from __future__ import annotations

import gzip
import json
import shutil
from importlib.metadata import version
from pathlib import Path
from typing import Any

from pdftext.extraction import dictionary_output


CACHE_SCHEMA = "raw-pages-v1"


def _entry_dir(cache_root: Path, source_sha256: str) -> Path:
    parser_version = version("pdftext")
    return cache_root / source_sha256 / f"pdftext-{parser_version}-{CACHE_SCHEMA}"


def _stable_pages(pages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Keep only extraction fields consumed by PDFCracker, excluding parser-private objects."""
    output: list[dict[str, Any]] = []
    for page in pages:
        blocks: list[dict[str, Any]] = []
        for block in page.get("blocks", []):
            lines: list[dict[str, Any]] = []
            for line in block.get("lines", []):
                spans: list[dict[str, Any]] = []
                for span in line.get("spans", []):
                    font = span.get("font") or {}
                    spans.append(
                        {
                            "text": str(span.get("text", "")),
                            "bbox": list(span["bbox"]),
                            "font": {
                                "name": str(font.get("name", "")),
                                "size": float(font.get("size", 0.0)),
                                "weight": int(font.get("weight", -1)),
                                "flags": int(font.get("flags", 0)),
                            },
                            "rotation": float(span.get("rotation", 0.0)),
                            "url": str(span.get("url", "")),
                            "superscript": bool(span.get("superscript", False)),
                            "subscript": bool(span.get("subscript", False)),
                        }
                    )
                lines.append({"bbox": list(line["bbox"]), "spans": spans})
            blocks.append({"bbox": list(block["bbox"]), "lines": lines})
        output.append(
            {
                "page": int(page["page"]),
                "width": float(page["width"]),
                "height": float(page["height"]),
                "rotation": int(page.get("rotation", 0)),
                "blocks": blocks,
            }
        )
    return output


def load_pdftext_pages(
    pdf_path: Path,
    source_sha256: str,
    cache_root: Path | None = None,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    if cache_root is None:
        pages = _stable_pages(dictionary_output(str(pdf_path), sort=True, keep_chars=False))
        return pages, {"enabled": False, "hit": False}

    entry = _entry_dir(cache_root, source_sha256)
    cache_file = entry / "raw-pages.json.gz"
    if cache_file.exists():
        with gzip.open(cache_file, "rt", encoding="utf-8") as handle:
            pages = json.load(handle)
        return pages, {
            "enabled": True,
            "hit": True,
            "key": str(entry.relative_to(cache_root)),
            "path": str(cache_file),
        }

    pages = _stable_pages(dictionary_output(str(pdf_path), sort=True, keep_chars=False))
    entry.mkdir(parents=True, exist_ok=True)
    temporary = cache_file.with_suffix(".tmp")
    try:
        with gzip.open(temporary, "wt", encoding="utf-8") as handle:
            json.dump(pages, handle, ensure_ascii=False, separators=(",", ":"))
    except Exception:
        temporary.unlink(missing_ok=True)
        raise
    temporary.replace(cache_file)
    return pages, {
        "enabled": True,
        "hit": False,
        "key": str(entry.relative_to(cache_root)),
        "path": str(cache_file),
    }


def cleanup_source_cache(cache_root: Path, source_sha256: str) -> bool:
    root = cache_root.resolve()
    target = (cache_root / source_sha256).resolve()
    if target.parent != root:
        raise ValueError(f"Unsafe cache cleanup target: {target}")
    removed = False
    if target.exists():
        shutil.rmtree(target)
        removed = True
    if root.exists() and not any(root.iterdir()):
        root.rmdir()
    cache_parent = root.parent
    if cache_parent.name == ".cache" and cache_parent.exists() and not any(cache_parent.iterdir()):
        cache_parent.rmdir()
    return removed
