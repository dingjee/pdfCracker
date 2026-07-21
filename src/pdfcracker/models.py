from __future__ import annotations

from enum import StrEnum
from typing import Any, Literal

from pydantic import BaseModel, Field


class BlockType(StrEnum):
    UNKNOWN = "unknown"
    PARAGRAPH = "paragraph"
    HEADING = "heading"
    TOC_ENTRY = "toc_entry"
    LIST_ITEM = "list_item"
    CAPTION = "caption"
    HEADER = "header"
    FOOTER = "footer"
    PAGE_NUMBER = "page_number"
    QUOTE = "quote"


class PageKind(StrEnum):
    BODY = "body"
    TOC = "toc"
    FRONT_MATTER = "front_matter"


class BBox(BaseModel):
    x0: float
    y0: float
    x1: float
    y1: float

    @classmethod
    def from_sequence(cls, values: list[float] | tuple[float, ...]) -> BBox:
        return cls(x0=values[0], y0=values[1], x1=values[2], y1=values[3])

    def normalized(self, width: float, height: float) -> BBox:
        return BBox(
            x0=self.x0 / width,
            y0=self.y0 / height,
            x1=self.x1 / width,
            y1=self.y1 / height,
        )


class FontInfo(BaseModel):
    name: str = ""
    size: float = 0.0
    weight: int = -1
    flags: int = 0


class Span(BaseModel):
    text: str
    bbox: BBox
    font: FontInfo
    rotation: float = 0.0
    url: str = ""
    superscript: bool = False
    subscript: bool = False
    bold: bool = False
    italic: bool = False
    underline: bool = False
    semantic_color: str | None = None
    color_rgb: tuple[float, float, float] | None = None
    style_evidence: list[str] = Field(default_factory=list)


class Line(BaseModel):
    text: str
    bbox: BBox
    spans: list[Span] = Field(default_factory=list)
    role: str | None = None
    toc_level: int | None = None
    toc_title: str | None = None
    toc_page_label: str | None = None


class Block(BaseModel):
    id: str
    page: int
    source_block_index: int
    bbox: BBox
    bbox_norm: BBox
    raw_text: str
    text: str
    lines: list[Line] = Field(default_factory=list)
    type: BlockType = BlockType.UNKNOWN
    confidence: float = 0.0
    evidence: list[str] = Field(default_factory=list)
    heading_level: int | None = None
    removed_as: str | None = None
    source_parser: str = "pdftext"
    quote_kind: str | None = None

    @property
    def primary_font(self) -> FontInfo:
        candidates = [
            span.font
            for line in self.lines
            for span in line.spans
            if span.text.strip() and span.font.size > 0
        ]
        if not candidates:
            return FontInfo()
        return max(candidates, key=lambda font: font.size)


class Page(BaseModel):
    number: int
    source_index: int
    width: float
    height: float
    rotation: int = 0
    kind: PageKind = PageKind.BODY
    blocks: list[Block] = Field(default_factory=list)


class MergeDecision(BaseModel):
    from_block: str
    to_block: str
    from_page: int
    to_page: int
    decision: Literal["merge", "review", "keep_separate"]
    score: float
    reasons: list[str] = Field(default_factory=list)


class Document(BaseModel):
    schema_version: str = "0.1.0"
    source_path: str
    source_sha256: str
    source_page_count: int
    selected_pages: list[int]
    pages: list[Page]
    body_font_size: float
    merges: list[MergeDecision] = Field(default_factory=list)
    metadata: dict[str, Any] = Field(default_factory=dict)
