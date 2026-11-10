#!/usr/bin/env python3
"""check_scan_chapter.py — local QA gate for a scanned-chapter delivery artifact.

Validates one chapter artifact directory against the delivery contract using
only the Python standard library. It never inspects or prints document/OCR
text: only file names, block ids, counts, page numbers, paths, and hashes are
read and reported.

Expected artifact layout
    <artifact-root>/
        chapter.md
        document.json
        images/

document.json contract
    {
      "pages":   [{"page": int, "printed_page": int, "status": "ok",
                   "source_block_ids": [str], "accounted_block_ids": [str]}],
      "figures": [{"page": int, "printed_page": int, "number": str,
                   "asset_path": str, "sha256": str}],
      "source_sha256": str
    }

Checks performed
    * page numbers are unique and contiguous; --pages START-END (inclusive,
      e.g. 21-44, or a single page number) pins the exact expected page set:
      pages outside the range and gaps inside the range are both failures
    * per page, source_block_ids and accounted_block_ids correspond
      one-to-one (equal counts, equal id sets, no duplicate ids in a list)
    * markdown image links and manifest figures correspond one-to-one
      (inline ![alt](path), reference-style ![alt][label] + [label]: path,
      and <img src="..."> forms are recognised)
    * every image path (manifest and markdown) is relative and stays under
      images/
    * every referenced image file exists; the manifest sha256 matches the file
    * image file name matches p{printed_page:04d}-fig-{number}.png
    * duplicate figure numbers, duplicate asset paths, and bad links are
      rejected

Output
    A single machine-readable JSON object on stdout. "ok" is false and the
    process exits non-zero when any check fails. Errors/warnings carry only
    metadata (codes, ids, paths, counts, hashes) — never document content.

Exit codes
    0  all checks passed
    1  QA failures (see "errors" in the JSON report)
    2  usage/environment error (bad arguments, artifact root not a directory)

Self test
    python scripts/check_scan_chapter.py --self-test
    Builds a synthetic fixture in a temporary directory, corrupts it in a set
    of controlled ways, and asserts the checker's exit codes and error codes.
    Requires no real book data.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import posixpath
import re
import shutil
import subprocess
import sys
import tempfile
from collections import Counter
from copy import deepcopy
from pathlib import Path
from typing import Any

TOOL_NAME = "check_scan_chapter"
TOOL_VERSION = "1.0.0"

EXIT_OK = 0
EXIT_QA_FAILURE = 1
EXIT_USAGE = 2

IMAGES_DIR = "images"
PAGE_STATUS_OK = "ok"
SELF_TEST_OCR_MARKER = "SYNTHETIC-OCR-MARKER-DO-NOT-LEAK"

SHA256_RE = re.compile(r"^[0-9a-fA-F]{64}$")
FIGURE_NUMBER_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
PAGE_RANGE_FULL_RE = re.compile(r"^(\d+)\s*-\s*(\d+)$")
PAGE_RANGE_SINGLE_RE = re.compile(r"^(\d+)$")
URL_SCHEME_RE = re.compile(r"^[A-Za-z][A-Za-z0-9+.\-]*:")
WINDOWS_DRIVE_RE = re.compile(r"^[A-Za-z]:")

MD_REF_DEF_RE = re.compile(
    r"^[ \t]{0,3}\[([^\[\]]+)\]:[ \t]*(?:<([^<>\n]*)>|(\S+))"
    r"(?:[ \t]+(?:\"[^\"]*\"|'[^']*'|\([^)]*\)))?[ \t]*$"
)
MD_INLINE_IMAGE_RE = re.compile(
    r"!\[[^\]\n]*\]\(\s*(?:<([^<>\n]*)>|([^\s)]+))"
    r"(?:\s+(?:\"[^\"]*\"|'[^']*'|\([^)]*\)))?\s*\)"
)
MD_REF_IMAGE_RE = re.compile(r"!\[([^\]\n]*)\]\[([^\]\n]*)\]")
MD_HTML_IMAGE_RE = re.compile(r"<img\b[^>]*?\bsrc\s*=\s*[\"']([^\"']+)[\"']", re.IGNORECASE)


def is_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def parse_pages_spec(spec: str) -> tuple[int, int]:
    """Parse an inclusive page range: '21-44' or a single '22'."""
    text = spec.strip()
    match = PAGE_RANGE_FULL_RE.match(text)
    if match:
        start, end = int(match.group(1)), int(match.group(2))
        if start < 1 or end < 1:
            raise ValueError("page numbers must be >= 1")
        if start > end:
            raise ValueError("range start must not exceed range end")
        return start, end
    match = PAGE_RANGE_SINGLE_RE.match(text)
    if match:
        start = int(match.group(1))
        if start < 1:
            raise ValueError("page numbers must be >= 1")
        return start, start
    raise ValueError("expected START-END (e.g. 21-44) or a single page number")


def sha256_file(path: Path) -> str | None:
    digest = hashlib.sha256()
    try:
        with path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1 << 20), b""):
                digest.update(chunk)
    except OSError:
        return None
    return digest.hexdigest()


class Report:
    def __init__(self, artifact_root: str) -> None:
        self.artifact_root = artifact_root
        self.errors: list[dict[str, Any]] = []
        self.warnings: list[dict[str, Any]] = []
        self.stats: dict[str, Any] = {}
        self.expected_pages: dict[str, int] | None = None
        self.source_sha256: str | None = None

    def error(self, code: str, **detail: Any) -> None:
        self.errors.append({"code": code, **detail})

    def warning(self, code: str, **detail: Any) -> None:
        self.warnings.append({"code": code, **detail})

    def as_dict(self) -> dict[str, Any]:
        return {
            "tool": TOOL_NAME,
            "version": TOOL_VERSION,
            "ok": not self.errors,
            "artifact_root": self.artifact_root,
            "expected_pages": self.expected_pages,
            "source_sha256": self.source_sha256,
            "stats": self.stats,
            "errors": self.errors,
            "warnings": self.warnings,
        }


def check_image_rel_path(report: Report, raw: Any, origin: str, detail: dict[str, Any]) -> str | None:
    """Require a local, relative, POSIX-style path whose first component is images/.

    Returns the normalized relative path, or None after reporting an error.
    """
    context = dict(detail)
    context["origin"] = origin
    if not isinstance(raw, str) or not raw.strip():
        report.error("image_path_invalid", path=raw if isinstance(raw, str) else None, **context)
        return None
    path = raw.strip()
    context["path"] = path
    if URL_SCHEME_RE.match(path):
        report.error("image_path_not_local", **context)
        return None
    if "\\" in path or path.startswith("/") or WINDOWS_DRIVE_RE.match(path):
        report.error("image_path_invalid", **context)
        return None
    if any(segment == ".." for segment in path.split("/")):
        report.error("image_path_escapes_root", **context)
        return None
    normalized = posixpath.normpath(path)
    if normalized in ("", ".", "/") or normalized == ".." or normalized.startswith("../") or normalized.startswith("/"):
        report.error("image_path_escapes_root", **context)
        return None
    if normalized.split("/", 1)[0] != IMAGES_DIR:
        report.error("image_path_outside_images_dir", **context)
        return None
    return normalized


def collect_markdown_image_targets(text: str, report: Report) -> list[tuple[str, int, str]]:
    """Extract image link targets from markdown text (no text is ever reported).

    Returns a list of (raw_target, line_number, syntax) triples in order.
    """
    lines = text.splitlines()
    definitions: dict[str, tuple[str, int]] = {}
    targets: list[tuple[str, int, str]] = []

    for lineno, line in enumerate(lines, start=1):
        match = MD_REF_DEF_RE.match(line)
        if match:
            label = match.group(1).strip().lower()
            target = (match.group(2) if match.group(2) is not None else match.group(3)).strip()
            if label in definitions:
                report.warning("markdown_reference_redefined", label=label, line=lineno)
            else:
                definitions[label] = (target, lineno)

    for lineno, line in enumerate(lines, start=1):
        for match in MD_INLINE_IMAGE_RE.finditer(line):
            target = match.group(1) if match.group(1) is not None else match.group(2)
            targets.append((target.strip(), lineno, "inline"))
        for match in MD_REF_IMAGE_RE.finditer(line):
            label = (match.group(2) or match.group(1)).strip().lower()
            if label not in definitions:
                report.error("markdown_reference_undefined", label=label, line=lineno)
                continue
            target, _def_line = definitions[label]
            targets.append((target, lineno, "reference"))
        for match in MD_HTML_IMAGE_RE.finditer(line):
            targets.append((match.group(1).strip(), lineno, "html"))

    return targets


def check_artifact(root: Path, pages_spec: str | None) -> tuple[Report, int]:
    report = Report(str(root))

    if not root.is_dir():
        report.error("artifact_root_not_directory", path=str(root))
        return report, EXIT_USAGE

    if pages_spec is not None:
        try:
            start, end = parse_pages_spec(pages_spec)
        except ValueError as exc:
            report.error("pages_spec_invalid", spec=pages_spec, reason=str(exc))
            return report, EXIT_USAGE
        report.expected_pages = {"start": start, "end": end}

    document_path = root / "document.json"
    chapter_path = root / "chapter.md"
    images_dir = root / IMAGES_DIR

    if not document_path.is_file():
        report.error("document_json_missing")
    if not chapter_path.is_file():
        report.error("chapter_md_missing")
    if not images_dir.is_dir():
        report.error("images_dir_missing")

    document: Any = None
    if document_path.is_file():
        try:
            document = json.loads(document_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            report.error("document_json_unreadable", reason=exc.__class__.__name__)

    if not isinstance(document, dict):
        if document_path.is_file():
            report.error("document_json_invalid", reason="top_level_value_not_object")
        report.stats = {
            "pages": 0,
            "page_entries": 0,
            "figures": 0,
            "distinct_manifest_asset_paths": 0,
            "distinct_markdown_image_paths": 0,
            "markdown_image_links": 0,
        }
        return report, EXIT_QA_FAILURE

    pages_raw = document.get("pages")
    figures_raw = document.get("figures")
    source_sha256 = document.get("source_sha256")

    if not isinstance(pages_raw, list):
        report.error("contract_field_invalid", field="pages", expected="list")
        pages_raw = []
    if not isinstance(figures_raw, list):
        report.error("contract_field_invalid", field="figures", expected="list")
        figures_raw = []
    if not isinstance(source_sha256, str) or not source_sha256.strip():
        report.error("contract_field_invalid", field="source_sha256", expected="non-empty string")
    else:
        report.source_sha256 = source_sha256

    # ------------------------------------------------------------------ pages
    pages_by_number: dict[int, dict[str, Any]] = {}
    printed_by_page: dict[int, int] = {}
    printed_owner: dict[int, int] = {}
    accounted_owner: dict[str, int] = {}

    for index, entry in enumerate(pages_raw):
        if not isinstance(entry, dict):
            report.error("contract_entry_invalid", section="pages", index=index)
            continue
        page = entry.get("page")
        printed_page = entry.get("printed_page")
        status = entry.get("status")
        source_ids = entry.get("source_block_ids")
        accounted_ids = entry.get("accounted_block_ids")

        entry_ok = True
        if not is_int(page) or page < 1:
            report.error("page_invalid", index=index, page=page if is_int(page) else None)
            entry_ok = False
        if not is_int(printed_page) or printed_page < 0:
            report.error("printed_page_invalid", index=index, page=page if is_int(page) else None)
            entry_ok = False
        if status != PAGE_STATUS_OK:
            report.error(
                "page_status_not_ok",
                page=page if is_int(page) else None,
                status=status if isinstance(status, str) else None,
            )
        for field, ids in (("source_block_ids", source_ids), ("accounted_block_ids", accounted_ids)):
            if not isinstance(ids, list) or not all(isinstance(block_id, str) for block_id in ids):
                report.error("block_id_list_invalid", field=field, page=page if is_int(page) else None)
                entry_ok = False
        if not entry_ok:
            continue

        page_number = int(page)
        if page_number in pages_by_number:
            report.error("duplicate_page", page=page_number)
        else:
            pages_by_number[page_number] = entry

        printed_number = int(printed_page)
        if printed_number in printed_owner:
            report.error(
                "duplicate_printed_page",
                printed_page=printed_number,
                pages=sorted([printed_owner[printed_number], page_number]),
            )
        else:
            printed_owner[printed_number] = page_number
            printed_by_page[page_number] = printed_number

        for field, ids in (("source_block_ids", source_ids), ("accounted_block_ids", accounted_ids)):
            duplicates = sorted(block_id for block_id, count in Counter(ids).items() if count > 1)
            if duplicates:
                report.error("duplicate_block_id_in_list", page=page_number, field=field, block_ids=duplicates)

        source_set, accounted_set = set(source_ids), set(accounted_ids)
        if len(source_ids) != len(accounted_ids):
            report.error(
                "page_block_count_mismatch",
                page=page_number,
                source_count=len(source_ids),
                accounted_count=len(accounted_ids),
            )
        sourced_not_accounted = sorted(source_set - accounted_set)
        accounted_not_sourced = sorted(accounted_set - source_set)
        if sourced_not_accounted or accounted_not_sourced:
            report.error(
                "page_block_unmatched",
                page=page_number,
                sourced_not_accounted=sourced_not_accounted,
                accounted_not_sourced=accounted_not_sourced,
            )
        if not source_ids and not accounted_ids:
            report.warning("page_has_no_blocks", page=page_number)

        for block_id in accounted_ids:
            first_page = accounted_owner.get(block_id)
            if first_page is not None and first_page != page_number:
                report.warning(
                    "block_accounted_on_multiple_pages",
                    block_id=block_id,
                    pages=sorted({first_page, page_number}),
                )
            else:
                accounted_owner[block_id] = page_number

    page_numbers = sorted(pages_by_number)
    if report.expected_pages is not None:
        start, end = report.expected_pages["start"], report.expected_pages["end"]
        outside = [number for number in page_numbers if number < start or number > end]
        if outside:
            report.error("page_outside_range", pages=outside, expected_start=start, expected_end=end)
        missing = sorted(set(range(start, end + 1)) - set(page_numbers))
        if missing:
            report.error("page_gap", missing_pages=missing, expected_start=start, expected_end=end)
    elif page_numbers:
        missing = sorted(set(range(page_numbers[0], page_numbers[-1] + 1)) - set(page_numbers))
        if missing:
            report.error(
                "page_gap",
                missing_pages=missing,
                first_page=page_numbers[0],
                last_page=page_numbers[-1],
            )

    # ---------------------------------------------------------------- figures
    figure_numbers: dict[str, list[int]] = {}
    asset_pages: dict[str, list[int | None]] = {}
    manifest_paths: Counter[str] = Counter()

    for index, entry in enumerate(figures_raw):
        if not isinstance(entry, dict):
            report.error("contract_entry_invalid", section="figures", index=index)
            continue
        page = entry.get("page")
        printed_page = entry.get("printed_page")
        number = entry.get("number")
        asset_path = entry.get("asset_path")
        declared_sha = entry.get("sha256")

        if not is_int(page) or page < 1:
            report.error("figure_page_invalid", index=index, page=page if is_int(page) else None)
            page = None
        if not is_int(printed_page) or printed_page < 0:
            report.error("figure_printed_page_invalid", index=index, page=page if is_int(page) else None)
            printed_page = None
        if not isinstance(number, str) or not FIGURE_NUMBER_RE.fullmatch(number):
            report.error(
                "figure_number_invalid",
                index=index,
                number=number if isinstance(number, str) else None,
            )
            number = None
        if not isinstance(declared_sha, str) or not SHA256_RE.fullmatch(declared_sha):
            report.error("sha256_format_invalid", index=index, page=page, number=number)
            declared_sha = None

        detail = {"index": index, "page": page, "printed_page": printed_page, "number": number}

        if page is not None:
            if page not in pages_by_number:
                report.error("figure_page_unknown", **detail)
            elif printed_page is not None and printed_by_page.get(page) != printed_page:
                report.error(
                    "figure_printed_page_mismatch",
                    page=page,
                    figure_printed_page=printed_page,
                    page_entry_printed_page=printed_by_page.get(page),
                )

        if page is not None and number is not None:
            figure_numbers.setdefault(number, []).append(page)

        normalized = None
        if isinstance(asset_path, str):
            normalized = check_image_rel_path(report, asset_path, origin="manifest", detail=detail)

        if normalized is None:
            continue

        if printed_page is not None and number is not None:
            expected_name = f"p{printed_page:04d}-fig-{number}.png"
            actual_name = posixpath.basename(normalized)
            if actual_name != expected_name:
                report.error(
                    "figure_filename_mismatch",
                    asset_path=normalized,
                    expected_filename=expected_name,
                    actual_filename=actual_name,
                    page=page,
                    printed_page=printed_page,
                    number=number,
                )

        occurrences = asset_pages.setdefault(normalized, [])
        occurrences.append(page if is_int(page) else None)
        if len(occurrences) > 1:
            report.error(
                "duplicate_asset_path",
                asset_path=normalized,
                pages=[entry_page for entry_page in occurrences if entry_page is not None],
            )
        manifest_paths[normalized] += 1

        file_path = root.joinpath(*normalized.split("/"))
        if not file_path.is_file():
            report.error("missing_image_file", origin="manifest", asset_path=normalized, page=page, number=number)
            continue
        computed_sha = sha256_file(file_path)
        if computed_sha is None:
            report.error("image_file_unreadable", asset_path=normalized, page=page, number=number)
        elif declared_sha is not None and computed_sha.lower() != declared_sha.lower():
            report.error(
                "sha256_mismatch",
                asset_path=normalized,
                page=page,
                number=number,
                declared_sha256=declared_sha.lower(),
                computed_sha256=computed_sha,
            )

    for number, pages in sorted(figure_numbers.items()):
        if len(pages) > 1:
            report.error("duplicate_figure_number", number=number, pages=sorted(pages))

    # -------------------------------------------------------------- markdown
    md_targets: list[tuple[str, int, str]] = []
    markdown_paths: Counter[str] = Counter()
    if chapter_path.is_file():
        try:
            chapter_text = chapter_path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            report.error("chapter_md_unreadable")
            chapter_text = ""
        md_targets = collect_markdown_image_targets(chapter_text, report)

    for raw_target, lineno, syntax in md_targets:
        normalized = check_image_rel_path(
            report, raw_target, origin="markdown", detail={"line": lineno, "syntax": syntax}
        )
        if normalized is None:
            continue
        markdown_paths[normalized] += 1
        file_path = root.joinpath(*normalized.split("/"))
        if not file_path.is_file():
            report.error("missing_image_file", origin="markdown", asset_path=normalized, line=lineno)

    orphan_paths = markdown_paths - manifest_paths
    unlinked_paths = manifest_paths - markdown_paths
    for path in sorted(orphan_paths):
        report.error(
            "orphan_markdown_image",
            asset_path=path,
            markdown_count=orphan_paths[path],
            manifest_count=manifest_paths.get(path, 0),
        )
    for path in sorted(unlinked_paths):
        report.error(
            "unlinked_manifest_figure",
            asset_path=path,
            manifest_count=unlinked_paths[path],
            markdown_count=markdown_paths.get(path, 0),
        )

    report.stats = {
        "pages": len(pages_by_number),
        "page_entries": len(pages_raw),
        "figures": len(figures_raw),
        "distinct_manifest_asset_paths": len(manifest_paths),
        "distinct_markdown_image_paths": len(markdown_paths),
        "markdown_image_links": len(md_targets),
    }

    return report, (EXIT_OK if not report.errors else EXIT_QA_FAILURE)


# ----------------------------------------------------------------- self test


def _load_doc(root: Path) -> dict[str, Any]:
    return json.loads((root / "document.json").read_text(encoding="utf-8"))


def _save_doc(root: Path, doc: dict[str, Any]) -> None:
    (root / "document.json").write_text(json.dumps(doc, indent=2), encoding="utf-8")


def _replace_md(root: Path, old: str, new: str) -> None:
    md_path = root / "chapter.md"
    md_path.write_text(md_path.read_text(encoding="utf-8").replace(old, new), encoding="utf-8")


def _write_fixture(root: Path) -> None:
    (root / IMAGES_DIR).mkdir(parents=True)

    image_payloads = {
        "p0019-fig-1.png": b"\x89PNG\r\n\x1a\n" + bytes(range(64)),
        "p0021-fig-2.png": b"\x89PNG\r\n\x1a\n" + bytes(range(64, 128)),
    }
    for name, payload in image_payloads.items():
        (root / IMAGES_DIR / name).write_bytes(payload)

    pages = [
        {
            "page": page,
            "printed_page": page - 2,
            "status": "ok",
            "source_block_ids": [f"blk-{page:04d}-a", f"blk-{page:04d}-b"],
            "accounted_block_ids": [f"blk-{page:04d}-a", f"blk-{page:04d}-b"],
        }
        for page in range(21, 25)
    ]
    figures = [
        {
            "page": 21,
            "printed_page": 19,
            "number": "1",
            "asset_path": f"{IMAGES_DIR}/p0019-fig-1.png",
            "sha256": hashlib.sha256(image_payloads["p0019-fig-1.png"]).hexdigest(),
        },
        {
            "page": 23,
            "printed_page": 21,
            "number": "2",
            "asset_path": f"{IMAGES_DIR}/p0021-fig-2.png",
            "sha256": hashlib.sha256(image_payloads["p0021-fig-2.png"]).hexdigest(),
        },
    ]
    chapter_lines = [
        "# Chapter 3 - Synthetic Fixture",
        "",
        SELF_TEST_OCR_MARKER,
        "",
        "![Figure 1](images/p0019-fig-1.png)",
        "",
        "![Figure 2][fig-two]",
        "",
        '[fig-two]: images/p0021-fig-2.png "Figure 2"',
        "",
    ]
    document = {
        "pages": pages,
        "figures": figures,
        "source_sha256": hashlib.sha256(b"synthetic-source").hexdigest(),
    }

    (root / "chapter.md").write_text("\n".join(chapter_lines), encoding="utf-8")
    (root / "document.json").write_text(json.dumps(document, indent=2), encoding="utf-8")


def _mut_duplicate_page(root: Path) -> None:
    doc = _load_doc(root)
    doc["pages"].append(deepcopy(doc["pages"][1]))
    _save_doc(root, doc)


def _mut_remove_page(root: Path) -> None:
    doc = _load_doc(root)
    doc["pages"].pop(2)  # drops page 23; figure 2 then references an unknown page
    _save_doc(root, doc)


def _mut_block_mismatch(root: Path) -> None:
    doc = _load_doc(root)
    doc["pages"][0]["accounted_block_ids"] = doc["pages"][0]["accounted_block_ids"][:1]
    _save_doc(root, doc)


def _mut_duplicate_figure_number(root: Path) -> None:
    doc = _load_doc(root)
    payload = b"synthetic-extra-image-bytes"
    name = "p0020-fig-1.png"
    (root / IMAGES_DIR / name).write_bytes(payload)
    doc["figures"].append(
        {
            "page": 22,
            "printed_page": 20,
            "number": "1",
            "asset_path": f"{IMAGES_DIR}/{name}",
            "sha256": hashlib.sha256(payload).hexdigest(),
        }
    )
    _save_doc(root, doc)
    md_path = root / "chapter.md"
    md_path.write_text(
        md_path.read_text(encoding="utf-8") + "\n![Duplicate figure](images/p0020-fig-1.png)\n",
        encoding="utf-8",
    )


def _mut_sha256_mismatch(root: Path) -> None:
    doc = _load_doc(root)
    doc["figures"][0]["sha256"] = "0" * 64
    _save_doc(root, doc)


def _mut_missing_image(root: Path) -> None:
    (root / IMAGES_DIR / "p0021-fig-2.png").unlink()


def _mut_filename_mismatch(root: Path) -> None:
    (root / IMAGES_DIR / "p0019-fig-1.png").rename(root / IMAGES_DIR / "p0019-fig-9.png")
    doc = _load_doc(root)
    doc["figures"][0]["asset_path"] = f"{IMAGES_DIR}/p0019-fig-9.png"
    _save_doc(root, doc)
    _replace_md(root, "images/p0019-fig-1.png", "images/p0019-fig-9.png")


def _mut_asset_outside_images(root: Path) -> None:
    (root / "assets").mkdir()
    payload = b"\x89PNG\r\n\x1a\n" + bytes(range(64))
    (root / "assets" / "p0019-fig-1.png").write_bytes(payload)
    doc = _load_doc(root)
    doc["figures"][0]["asset_path"] = "assets/p0019-fig-1.png"
    _save_doc(root, doc)
    _replace_md(root, "images/p0019-fig-1.png", "assets/p0019-fig-1.png")


def _mut_asset_escapes_root(root: Path) -> None:
    (root / IMAGES_DIR / "p0019-fig-1.png").rename(root / "p0019-fig-1.png")
    doc = _load_doc(root)
    doc["figures"][0]["asset_path"] = "images/../p0019-fig-1.png"
    _save_doc(root, doc)
    _replace_md(root, "images/p0019-fig-1.png", "images/../p0019-fig-1.png")


def _mut_duplicate_asset(root: Path) -> None:
    doc = _load_doc(root)
    first, second = doc["figures"][0], doc["figures"][1]
    second["asset_path"] = first["asset_path"]
    second["sha256"] = first["sha256"]
    _save_doc(root, doc)
    _replace_md(root, "![Figure 2][fig-two]", "![Figure 2 alias](images/p0019-fig-1.png)")


def _mut_broken_links(root: Path) -> None:
    md_path = root / "chapter.md"
    md_text = md_path.read_text(encoding="utf-8")
    md_text = md_text.replace("![Figure 2][fig-two]\n", "")
    md_text = md_text.rstrip("\n") + "\n\n![Ghost figure](images/p0019-fig-ghost.png)\n"
    md_path.write_text(md_text, encoding="utf-8")


def _mut_page_status(root: Path) -> None:
    doc = _load_doc(root)
    doc["pages"][1]["status"] = "review"
    _save_doc(root, doc)


def _mut_drop_document(root: Path) -> None:
    (root / "document.json").unlink()


def _mut_drop_images_dir(root: Path) -> None:
    shutil.rmtree(root / IMAGES_DIR)


def _case(name: str, mutate=None, pages: str | None = None, want_exit: int = EXIT_QA_FAILURE, codes=()) -> dict[str, Any]:
    return {"name": name, "mutate": mutate, "pages": pages, "want_exit": want_exit, "codes": set(codes)}


SELF_TEST_CASES = [
    _case("base_artifact_passes", want_exit=EXIT_OK),
    _case("full_explicit_range_passes", pages="21-24", want_exit=EXIT_OK),
    _case("range_above_manifest_reports_gap", pages="21-26", codes={"page_gap"}),
    _case("range_below_manifest_reports_outside", pages="21-23", codes={"page_outside_range"}),
    _case("single_page_spec_reports_outside", pages="22", codes={"page_outside_range"}),
    _case("invalid_range_spec_usage_error", pages="abc", want_exit=EXIT_USAGE),
    _case("duplicate_page_rejected", _mut_duplicate_page, codes={"duplicate_page", "duplicate_printed_page"}),
    _case("removed_page_reports_gap_and_unknown_figure", _mut_remove_page, codes={"page_gap", "figure_page_unknown"}),
    _case("block_accounting_mismatch_rejected", _mut_block_mismatch, codes={"page_block_count_mismatch", "page_block_unmatched"}),
    _case("duplicate_figure_number_rejected", _mut_duplicate_figure_number, codes={"duplicate_figure_number"}),
    _case("sha256_mismatch_rejected", _mut_sha256_mismatch, codes={"sha256_mismatch"}),
    _case("missing_image_file_rejected", _mut_missing_image, codes={"missing_image_file"}),
    _case("figure_filename_mismatch_rejected", _mut_filename_mismatch, codes={"figure_filename_mismatch"}),
    _case("asset_outside_images_dir_rejected", _mut_asset_outside_images, codes={"image_path_outside_images_dir"}),
    _case("asset_path_escape_rejected", _mut_asset_escapes_root, codes={"image_path_escapes_root"}),
    _case("duplicate_asset_path_rejected", _mut_duplicate_asset, codes={"duplicate_asset_path", "figure_filename_mismatch"}),
    _case("orphan_and_unlinked_links_rejected", _mut_broken_links, codes={"orphan_markdown_image", "unlinked_manifest_figure", "missing_image_file"}),
    _case("page_status_not_ok_rejected", _mut_page_status, codes={"page_status_not_ok"}),
    _case("missing_document_json_rejected", _mut_drop_document, codes={"document_json_missing"}),
    _case("missing_images_dir_rejected", _mut_drop_images_dir, codes={"images_dir_missing"}),
]


def run_self_test() -> int:
    script_path = Path(__file__).resolve()
    results: list[dict[str, Any]] = []
    failed = 0

    for case in SELF_TEST_CASES:
        with tempfile.TemporaryDirectory(prefix="check_scan_chapter_selftest_") as tmp:
            root = Path(tmp) / "chapter"
            _write_fixture(root)
            if case["mutate"] is not None:
                case["mutate"](root)
            command = [sys.executable, str(script_path), str(root)]
            if case["pages"]:
                command += ["--pages", case["pages"]]
            proc = subprocess.run(command, capture_output=True, text=True)

        parsed: Any = None
        parse_problem = None
        try:
            parsed = json.loads(proc.stdout)
        except json.JSONDecodeError as exc:
            parse_problem = f"stdout is not JSON: {exc}"

        actual_codes: set[str] = set()
        if isinstance(parsed, dict):
            actual_codes = {
                str(entry.get("code"))
                for entry in parsed.get("errors", [])
                if isinstance(entry, dict) and entry.get("code")
            }

        problems: list[str] = []
        if proc.returncode != case["want_exit"]:
            problems.append(f"exit {proc.returncode} != expected {case['want_exit']}")
        if parse_problem is not None:
            problems.append(parse_problem)
        missing_codes = case["codes"] - actual_codes
        if missing_codes:
            problems.append(f"missing error codes: {sorted(missing_codes)}")
        if case["want_exit"] == EXIT_OK and isinstance(parsed, dict) and parsed.get("ok") is not True:
            problems.append("expected ok=true in report")
        if SELF_TEST_OCR_MARKER in proc.stdout:
            problems.append("report leaked synthetic OCR marker")

        ok = not problems
        failed += 0 if ok else 1
        results.append(
            {
                "name": case["name"],
                "ok": ok,
                "expected_exit": case["want_exit"],
                "actual_exit": proc.returncode,
                "required_codes": sorted(case["codes"]),
                "actual_codes": sorted(actual_codes),
                "problems": problems,
            }
        )

    summary = {
        "tool": TOOL_NAME,
        "self_test": {
            "passed": len(results) - failed,
            "failed": failed,
            "total": len(results),
            "cases": results,
        },
    }
    print(json.dumps(summary, indent=2, sort_keys=True))
    return EXIT_OK if failed == 0 else EXIT_QA_FAILURE


# ---------------------------------------------------------------------- main


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog=TOOL_NAME,
        description=(
            "Local QA gate for a scanned-chapter delivery artifact "
            "(chapter.md + document.json + images/). Prints a machine-readable "
            "JSON report; exit 0 = pass, 1 = QA failure, 2 = usage error."
        ),
    )
    parser.add_argument(
        "artifact_root",
        nargs="?",
        default=None,
        help="chapter artifact directory containing chapter.md, document.json, images/",
    )
    parser.add_argument(
        "--pages",
        default=None,
        metavar="START-END",
        help="expected inclusive page range, e.g. 21-44, or a single page number",
    )
    parser.add_argument(
        "--self-test",
        action="store_true",
        help="build a synthetic fixture in a temp dir, verify the checker, print a JSON self-test report",
    )
    parser.add_argument("--version", action="version", version=f"{TOOL_NAME} {TOOL_VERSION}")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.self_test:
        return run_self_test()
    if not args.artifact_root:
        build_parser().error("artifact_root is required unless --self-test is given")
    report, code = check_artifact(Path(args.artifact_root).expanduser().resolve(), args.pages)
    print(json.dumps(report.as_dict(), indent=2, sort_keys=True))
    return code


if __name__ == "__main__":
    sys.exit(main())
