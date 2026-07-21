from __future__ import annotations

import argparse
import json
import time
from pathlib import Path

import pypdfium2 as pdfium

from pdfcracker.diagnostics import probe_styles
from pdfcracker.extract import extract_document, parse_page_spec
from pdfcracker.markdown import render_markdown
from pdfcracker.render import render_pages
from pdfcracker.vision import run_benchmark


def _page_count(pdf_path: Path) -> int:
    document = pdfium.PdfDocument(pdf_path)
    try:
        return len(document)
    finally:
        document.close()


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="pdfcracker")
    subparsers = parser.add_subparsers(dest="command", required=True)

    extract_parser = subparsers.add_parser("extract", help="Extract a provenance-rich pilot")
    extract_parser.add_argument("pdf", type=Path)
    extract_parser.add_argument("--pages", help="1-based pages, e.g. 3-20,37-38")
    extract_parser.add_argument("--output", type=Path, required=True)
    extract_parser.add_argument("--title")

    render_parser = subparsers.add_parser("render", help="Render selected pages to PNG")
    render_parser.add_argument("pdf", type=Path)
    render_parser.add_argument("--pages", required=True)
    render_parser.add_argument("--dpi", type=int, default=96)
    render_parser.add_argument("--output", type=Path, required=True)

    vision_parser = subparsers.add_parser(
        "vision-benchmark", help="Run structured visual audits through OpenRouter"
    )
    vision_parser.add_argument("images", nargs="+", type=Path)
    vision_parser.add_argument("--model", action="append", required=True)
    vision_parser.add_argument("--output", type=Path, required=True)
    vision_parser.add_argument("--gold", type=Path)

    style_parser = subparsers.add_parser(
        "style-probe", help="Inspect PDF-native fonts, colors, lines, and duplicate glyphs"
    )
    style_parser.add_argument("pdf", type=Path)
    style_parser.add_argument("--pages", required=True)
    style_parser.add_argument("--output", type=Path, required=True)
    return parser


def _run_extract(args: argparse.Namespace) -> int:
    started = time.perf_counter()
    page_count = _page_count(args.pdf)
    pages = parse_page_spec(args.pages, page_count)
    args.output.mkdir(parents=True, exist_ok=True)
    document = extract_document(args.pdf, pages)
    (args.output / "document.json").write_text(
        document.model_dump_json(indent=2), encoding="utf-8"
    )
    (args.output / "book.md").write_text(
        render_markdown(document, title=args.title), encoding="utf-8"
    )
    (args.output / "book.debug.md").write_text(
        render_markdown(document, title=args.title, debug=True), encoding="utf-8"
    )
    report = {
        "source_sha256": document.source_sha256,
        "source_page_count": document.source_page_count,
        "selected_pages": document.selected_pages,
        "body_font_size": document.body_font_size,
        "block_counts": document.metadata["block_counts"],
        "merge_counts": {
            decision: sum(item.decision == decision for item in document.merges)
            for decision in ("merge", "review", "keep_separate")
        },
        "style_counts": {
            "bold_chars": sum(
                len(span.text) if span.bold else 0
                for page in document.pages
                for block in page.blocks
                for line in block.lines
                for span in line.spans
            ),
            "italic_chars": sum(
                len(span.text) if span.italic else 0
                for page in document.pages
                for block in page.blocks
                for line in block.lines
                for span in line.spans
            ),
            "underline_chars": sum(
                len(span.text) if span.underline else 0
                for page in document.pages
                for block in page.blocks
                for line in block.lines
                for span in line.spans
            ),
            "semantic_color_chars": sum(
                len(span.text) if span.semantic_color is not None else 0
                for page in document.pages
                for block in page.blocks
                for line in block.lines
                for span in line.spans
            ),
            "quote_blocks": sum(
                block.type.value == "quote" for page in document.pages for block in page.blocks
            ),
            "toc_entries": sum(
                line.toc_title is not None
                for page in document.pages
                for block in page.blocks
                for line in block.lines
            ),
        },
        "elapsed_seconds": round(time.perf_counter() - started, 3),
    }
    (args.output / "report.json").write_text(
        json.dumps(report, indent=2), encoding="utf-8"
    )
    print(json.dumps(report, indent=2))
    return 0


def _run_render(args: argparse.Namespace) -> int:
    page_count = _page_count(args.pdf)
    pages = parse_page_spec(args.pages, page_count)
    outputs = render_pages(args.pdf, args.output, pages, args.dpi)
    print(json.dumps([str(path) for path in outputs], indent=2))
    return 0


def _run_vision_benchmark(args: argparse.Namespace) -> int:
    gold = json.loads(args.gold.read_text(encoding="utf-8")) if args.gold else None
    rows = run_benchmark(args.images, args.model, args.output, gold)
    summary = {
        "calls": len(rows),
        "successes": sum("error" not in row for row in rows),
        "errors": sum("error" in row for row in rows),
        "total_cost_usd": round(
            sum(float(row.get("usage", {}).get("cost", 0.0)) for row in rows), 8
        ),
    }
    print(json.dumps(summary, indent=2))
    return 0


def _run_style_probe(args: argparse.Namespace) -> int:
    pages = parse_page_spec(args.pages, _page_count(args.pdf))
    rows = probe_styles(args.pdf, pages)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(rows, indent=2), encoding="utf-8")
    summary = {
        "pages": pages,
        "semantic_color_runs": sum(len(row["semantic_color_runs"]) for row in rows),
        "underline_candidates": sum(len(row["underline_evidence"]) for row in rows),
        "duplicate_chars": sum(row["duplicate_char_count"] for row in rows),
    }
    print(json.dumps(summary, indent=2))
    return 0


def main() -> None:
    parser = _build_parser()
    args = parser.parse_args()
    try:
        if args.command == "extract":
            raise SystemExit(_run_extract(args))
        if args.command == "render":
            raise SystemExit(_run_render(args))
        if args.command == "vision-benchmark":
            raise SystemExit(_run_vision_benchmark(args))
        if args.command == "style-probe":
            raise SystemExit(_run_style_probe(args))
    except (FileNotFoundError, ValueError) as error:
        parser.error(str(error))
    raise SystemExit(2)


if __name__ == "__main__":
    main()
