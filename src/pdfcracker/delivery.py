"""Package accepted Markdown without depending on a document sidecar."""
from __future__ import annotations

import base64
import hashlib
import html
from html.parser import HTMLParser
import json
from pathlib import Path
import posixpath
from tempfile import TemporaryDirectory
from typing import Callable
from urllib.parse import unquote, urlsplit
import zipfile

from PIL import Image
from pydantic import BaseModel

from pdfcracker.markdown_content import image_references, markdown_parser, resolve_image


class DeliveryResult(BaseModel):
    archive_path: str
    standalone_path: str | None
    image_count: int
    reference_count: int


class _SemanticHTML(HTMLParser):
    """Retain the engine's inline semantic tags; display other raw HTML literally."""
    allowed = {"strong", "em", "u", "span", "br"}

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []

    def handle_starttag(self, tag, attrs) -> None:
        if tag not in self.allowed:
            self.parts.append(html.escape(self.get_starttag_text()))
            return
        attributes = ""
        if tag == "span":
            classes = dict(attrs).get("class") or ""
            attributes = f' class="{html.escape(classes, quote=True)}"'
        self.parts.append(f"<{tag}{attributes}>")

    def handle_endtag(self, tag) -> None:
        self.parts.append(f"</{tag}>" if tag in self.allowed else html.escape(f"</{tag}>"))

    def handle_data(self, data) -> None:
        self.parts.append(html.escape(data))


def _render_html(markdown: str, path: Path, root: Path, *, embed: bool) -> str:
    parser = markdown_parser()

    def raw_html(tokens, index, options, env):
        safe = _SemanticHTML()
        safe.feed(tokens[index].content)
        return "".join(safe.parts)

    def image(tokens, index, options, env):
        token = tokens[index]
        source = token.attrGet("src") or ""
        asset = resolve_image(path, root, source)
        with Image.open(asset) as decoded:
            mime = Image.MIME.get(decoded.format)
            width, height = decoded.size
        if mime not in {"image/png", "image/jpeg", "image/gif", "image/webp"}:
            raise ValueError(f"Provide a browser-compatible PNG/JPEG copy for image: {asset}")
        if embed:
            source = f"data:{mime};base64," + base64.b64encode(asset.read_bytes()).decode("ascii")
        return (f'<img src="{html.escape(source, quote=True)}" '
                f'alt="{html.escape(token.content, quote=True)}" width="{width}" height="{height}" '
                'loading="lazy" decoding="async">')

    parser.renderer.rules.update(image=image, html_inline=raw_html, html_block=raw_html)
    body = parser.render(markdown)
    return ('<!doctype html><html><head><meta charset="utf-8">'
            '<meta name="viewport" content="width=device-width,initial-scale=1">'
            f'<title>{html.escape(path.stem)}</title><style>'
            'body{font:18px/1.8 system-ui,sans-serif;max-width:960px;margin:40px auto;padding:0 20px}'
            'img{display:block;max-width:100%;height:auto;margin:16px 0}'
            'table{display:block;overflow:auto;border-collapse:collapse}'
            'th,td{border:1px solid #ccc;padding:8px}h1,h2,h3{line-height:1.4}'
            '.text-red{color:#a00}.text-blue{color:#1455a0}'
            '</style></head><body>' + body + '</body></html>')


class _HTMLImages(HTMLParser):
    def __init__(self, on_image: Callable[[str], None]) -> None:
        super().__init__()
        self.on_image = on_image

    def handle_starttag(self, tag, attrs) -> None:
        if tag == "img":
            self.on_image(dict(attrs).get("src") or "")


def _verify_zip(path: Path) -> None:
    with zipfile.ZipFile(path) as archive:
        bad = archive.testzip()
        if bad:
            raise ValueError(f"Archive CRC failure: {bad}")
        names = set(archive.namelist())
        for name in names:
            def check(source: str) -> None:
                target = posixpath.normpath(posixpath.join(
                    posixpath.dirname(name), unquote(urlsplit(source).path)))
                if not source or target not in names or target.startswith("../"):
                    raise ValueError(f"Broken archived image reference in {name}: {source}")
            if name.endswith(".md"):
                for reference in image_references(archive.read(name).decode("utf-8")):
                    check(reference.source)
            elif name.endswith(".html"):
                _HTMLImages(check).feed(archive.read(name).decode("utf-8"))


def _verify_standalone(path: Path, expected_assets: list[Path]) -> None:
    expected = iter(expected_assets)
    count = 0

    def check(source: str) -> None:
        nonlocal count
        asset = next(expected, None)
        if asset is None or not source.startswith("data:image/") or ";base64," not in source:
            raise ValueError("Standalone HTML has an unexpected external or missing image")
        decoded = base64.b64decode(source.split(",", 1)[1], validate=True)
        if hashlib.sha256(decoded).digest() != hashlib.sha256(asset.read_bytes()).digest():
            raise ValueError(f"Standalone image bytes differ: {asset}")
        count += 1

    parser = _HTMLImages(check)
    with path.open(encoding="utf-8") as stream:
        while chunk := stream.read(1024 * 1024):
            parser.feed(chunk)
    parser.close()
    if count != len(expected_assets):
        raise ValueError("Standalone HTML lost images")


def package_markdown(
    markdown_paths: list[Path], artifact_root: Path, output_dir: Path, *,
    self_contained: bool = False, extra_files: list[Path] | None = None,
) -> DeliveryResult:
    """Preserve the input layout in a ZIP and optionally embed images in one HTML.

    Images resolve from each Markdown file's directory. Only files inside the
    declared artifact root may enter the package. No source files are changed.
    """
    root = artifact_root.resolve()
    output_dir = output_dir.resolve()
    if output_dir.exists():
        raise ValueError(f"Output already exists: {output_dir}")
    if not markdown_paths:
        raise ValueError("At least one Markdown file is required")
    sources = [path.resolve() for path in markdown_paths]
    members: dict[str, Path] = {}
    texts: dict[Path, str] = {}
    assets: set[Path] = set()
    reference_count = 0
    for source in sources + [p.resolve() for p in extra_files or []]:
        if not source.is_relative_to(root) or not source.is_file():
            raise ValueError(f"Input is missing or outside artifact root: {source}")
        members[source.relative_to(root).as_posix()] = source
    for source in sources:
        if source.suffix.casefold() != ".md":
            raise ValueError(f"Expected a Markdown file: {source}")
        texts[source] = source.read_text(encoding="utf-8")
        for reference in image_references(texts[source]):
            asset = resolve_image(source, root, reference.source)
            with Image.open(asset) as decoded:
                decoded.verify()
            assets.add(asset)
            members[asset.relative_to(root).as_posix()] = asset
            reference_count += 1
    first = sources[0]
    html_name = first.relative_to(root).with_suffix(".html").as_posix()
    if html_name in members or "PDFCRACKER-README.txt" in members:
        raise ValueError("Input collides with a generated delivery file")
    linked = _render_html(texts[first], first, root, embed=False)
    output_dir.parent.mkdir(parents=True, exist_ok=True)
    with TemporaryDirectory(prefix=".package-", dir=output_dir.parent) as temporary:
        staging = Path(temporary) / "delivery"
        staging.mkdir()
        archive_path = staging / "portable.zip"
        with zipfile.ZipFile(archive_path, "w", zipfile.ZIP_DEFLATED) as archive:
            for name, file in sorted(members.items()):
                archive.write(file, name)
            archive.writestr(html_name, linked)
            archive.writestr("PDFCRACKER-README.txt",
                             f"Extract the entire archive, then open {html_name}.\n"
                             "Keep the extracted folders together when moving this document.\n")
        _verify_zip(archive_path)
        standalone_name = "standalone.html" if self_contained else None
        if standalone_name:
            (staging / standalone_name).write_text(
                _render_html(texts[first], first, root, embed=True), encoding="utf-8")
            _verify_standalone(staging / standalone_name, [
                resolve_image(first, root, ref.source) for ref in image_references(texts[first])
            ])
        manifest = {
            "markdown_files": [p.relative_to(root).as_posix() for p in sources],
            "image_count": len(assets), "reference_count": reference_count,
            "asset_sha256": {p.relative_to(root).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
                             for p in sorted(assets)},
            "archive_crc_and_image_references": "passed",
            "standalone_image_hashes": "passed" if standalone_name else "not_requested",
        }
        (staging / "manifest.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
        staging.rename(output_dir)
    return DeliveryResult(
        archive_path=str(output_dir / "portable.zip"),
        standalone_path=str(output_dir / standalone_name) if standalone_name else None,
        image_count=len(assets), reference_count=reference_count,
    )
