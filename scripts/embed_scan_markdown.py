"""Embed standalone scan image links in Markdown without rewriting the prose."""

from __future__ import annotations

import argparse
import base64
from io import BytesIO
import json
import os
from pathlib import Path
import re
from tempfile import NamedTemporaryFile

from PIL import Image

from pdfcracker.markdown_content import image_references, markdown_parser, resolve_image


IMAGE_LINE = re.compile(r" {0,3}!\[.*\]\((?P<target>[^\s()<>]+)\)[ \t]*")
MODES = ("gray-jpeg", "gray-png", "bilevel-png")


def encode_scan(asset: Path, mode: str, quality: int) -> tuple[str, int]:
    with Image.open(asset) as source:
        if getattr(source, "is_animated", False):
            raise ValueError(f"Expected a still image: {asset}")
        if "A" in source.getbands() or "transparency" in source.info:
            rgba = source.convert("RGBA")
            canvas = Image.new("RGBA", rgba.size, "white")
            gray = Image.alpha_composite(canvas, rgba).convert("L")
        else:
            gray = source.convert("L")
    encoded = BytesIO()
    if mode == "gray-jpeg":
        mime = "image/jpeg"
        gray.save(encoded, "JPEG", quality=quality, optimize=True)
    else:
        mime = "image/png"
        if mode == "bilevel-png":
            gray = gray.point(lambda value: 255 if value >= 128 else 0, mode="1")
        gray.save(encoded, "PNG", optimize=True)
    payload = encoded.getvalue()
    return f"data:{mime};base64," + base64.b64encode(payload).decode("ascii"), len(payload)


def embed_markdown(
    source: Path, output: Path, root: Path, *, mode: str = "gray-jpeg", quality: int = 90,
) -> dict:
    """Rewrite image destinations in the standalone link format PDFCracker emits."""
    if output.exists() or output.is_symlink():
        raise FileExistsError(f"Output already exists: {output}")
    if mode not in MODES or not 1 <= quality <= 95:
        raise ValueError("Choose a supported mode and JPEG quality from 1 to 95.")

    original = source.read_bytes()
    markdown = original.decode("utf-8")
    lines = markdown.split("\n")
    assets: dict[Path, tuple[str, int]] = {}
    expected = []
    for token in markdown_parser().parse(markdown):
        images = [child for child in token.children or [] if child.type == "image"]
        if not images:
            continue
        if token.map is None or token.map[1] != token.map[0] + 1 or len(token.children) != 1:
            raise ValueError("Only standalone Markdown image links on one line are supported.")
        index = token.map[0]
        line = lines[index]
        match = IMAGE_LINE.fullmatch(line.removesuffix("\r"))
        if not match:
            raise ValueError(f"Expected ![alt](path) on line {index + 1}.")
        asset = resolve_image(source, root, images[0].attrGet("src") or "")
        if asset not in assets:
            assets[asset] = encode_scan(asset, mode, quality)
        uri, _ = assets[asset]
        start, end = match.span("target")
        lines[index] = line[:start] + uri + line[end:]
        expected.append((uri, images[0].content))
    if not expected:
        raise ValueError("No Markdown images found.")

    embedded = "\n".join(lines)
    if [(ref.source, ref.alt) for ref in image_references(embedded)] != expected:
        raise ValueError("Embedded images did not round-trip through the Markdown parser.")
    payload = embedded.encode("utf-8")
    with NamedTemporaryFile(dir=output.parent) as temporary:
        temporary.write(payload)
        temporary.flush()
        os.link(temporary.name, output)
    return {
        "output": str(output),
        "mode": mode,
        "image_links": len(expected),
        "unique_images": len(assets),
        "source_markdown_bytes": len(original),
        "source_image_bytes": sum(asset.stat().st_size for asset in assets),
        "encoded_image_bytes": sum(size for _, size in assets.values()),
        "embedded_markdown_bytes": len(payload),
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source", type=Path)
    parser.add_argument("output", type=Path)
    parser.add_argument("--artifact-root", type=Path, help="Defaults to the source directory.")
    parser.add_argument("--mode", choices=MODES, default="gray-jpeg")
    parser.add_argument("--quality", type=int, default=90, help="JPEG quality, 1–95.")
    args = parser.parse_args()
    try:
        report = embed_markdown(
            args.source, args.output, args.artifact_root or args.source.parent,
            mode=args.mode, quality=args.quality,
        )
    except (OSError, ValueError) as error:
        parser.error(str(error))
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
