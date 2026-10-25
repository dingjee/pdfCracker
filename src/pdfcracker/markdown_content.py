"""Inspect delivered Markdown, independently of the document sidecar."""
from __future__ import annotations

from dataclasses import dataclass
from html.parser import HTMLParser
from pathlib import Path
import re
from urllib.parse import unquote, urlsplit

from markdown_it import MarkdownIt


def markdown_parser() -> MarkdownIt:
    return MarkdownIt("commonmark", {"html": True}).enable("table")


@dataclass(frozen=True)
class ImageReference:
    source: str
    alt: str


def image_references(markdown: str) -> list[ImageReference]:
    return [
        ImageReference(child.attrGet("src") or "", child.content)
        for token in markdown_parser().parse(markdown)
        for child in token.children or []
        if child.type == "image"
    ]


def resolve_image(markdown_path: Path, artifact_root: Path, source: str) -> Path:
    url = urlsplit(source)
    relative = Path(unquote(url.path).replace("\\", "/"))
    if not source or not url.path or url.scheme or url.netloc or relative.is_absolute():
        raise ValueError(f"Expected a nonempty local image reference: {source!r}")
    target = (markdown_path.parent / relative).resolve()
    if not target.is_relative_to(artifact_root.resolve()) or not target.is_file():
        raise ValueError(f"Missing image or path outside artifact root: {source!r}")
    return target


class _HTMLText(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []

    def handle_data(self, data: str) -> None:
        self.parts.append(data)


def normalize_plain_text(text: str) -> str:
    return "".join(text.split())


def list_item_text(text: str) -> str:
    """Remove a source list label that the Markdown list marker replaces."""
    return re.sub(r"^(?:[•●▪◦*-]|\(?\d+[.)]|[A-Za-z][.)])\s+", "", text)


def normalized_markdown_text(markdown: str) -> str:
    parts: list[str] = []
    for token in markdown_parser().parse(markdown):
        if token.type == "html_block":
            parser = _HTMLText()
            parser.feed(token.content)
            parts.extend(parser.parts)
        for child in token.children or []:
            if child.type in {"text", "code_inline"}:
                parts.append(child.content)
            elif child.type == "html_inline":
                parser = _HTMLText()
                parser.feed(child.content)
                parts.extend(parser.parts)
    return normalize_plain_text("".join(parts))
