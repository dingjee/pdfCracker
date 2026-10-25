from __future__ import annotations

import re
from pathlib import Path

import pytest

from pdfcracker.extract import extract_document, parse_page_spec
from pdfcracker.markdown import render_markdown
from pdfcracker.models import BlockType


SAMPLE_NAME = "Breaking Comedy's DNA_v1.4.4-[US]Jerry Corley.pdf"
SAMPLE_ROOT = Path(__file__).parents[1] / "inputs"
SAMPLE_PDF = next(
    (path for path in (SAMPLE_ROOT / SAMPLE_NAME, SAMPLE_ROOT / "achieved" / SAMPLE_NAME)
     if path.is_file()),
    None,
)


def test_parse_page_spec() -> None:
    assert parse_page_spec("3-5,8,10-11", 12) == [3, 4, 5, 8, 10, 11]


def test_parse_page_spec_rejects_invalid_page() -> None:
    with pytest.raises(ValueError):
        parse_page_spec("0,3", 12)


@pytest.fixture(scope="module")
def sample_document():
    if SAMPLE_PDF is None:
        pytest.skip("local PDF regression sample is not present")
    return extract_document(SAMPLE_PDF, [3, 4, 6, 7, 15, 37, 38])


def test_toc_boundary_is_not_merged(sample_document) -> None:
    decision = next(
        item
        for item in sample_document.merges
        if item.from_page == 3 and item.to_page == 4
    )
    assert decision.decision == "keep_separate"
    assert decision.reasons == ["non_paragraph_boundary"]


def test_body_continuation_is_merged(sample_document) -> None:
    decision = next(
        item
        for item in sample_document.merges
        if item.from_page == 37 and item.to_page == 38
    )
    assert decision.decision == "merge"
    assert decision.score >= 0.75


def test_tightly_split_body_blocks_are_merged(sample_document) -> None:
    decision = next(
        item
        for item in sample_document.merges
        if item.from_block == "p0038-b0002" and item.to_block == "p0038-b0003"
    )
    assert decision.decision == "merge"
    assert "following_starts_lowercase" in decision.reasons


def test_mixed_font_block_splits_heading_from_body(sample_document) -> None:
    page = next(page for page in sample_document.pages if page.number == 7)
    heading = next(block for block in page.blocks if block.text == "What Makes Something Comedy?")
    assert heading.type == BlockType.HEADING
    assert heading.heading_level == 2
    assert any(block.text.startswith("The difference between") for block in page.blocks)


def test_stacked_chapter_opening_headings_form_a_hierarchy(sample_document) -> None:
    page = next(page for page in sample_document.pages if page.number == 6)
    headings = [
        block for block in page.blocks if block.type == BlockType.HEADING and not block.removed_as
    ]
    assert [block.heading_level for block in headings[:3]] == [1, 2, 3]


def test_native_style_evidence_preserves_underline_and_synthetic_bold(sample_document) -> None:
    page = next(page for page in sample_document.pages if page.number == 7)
    underlined = "".join(
        span.text
        for block in page.blocks
        for line in block.lines
        for span in line.spans
        if span.underline
    )
    bold = " ".join(
        span.text.strip()
        for block in page.blocks
        for line in block.lines
        for span in line.spans
        if span.bold and span.text.strip()
    )
    assert "something" in underlined
    assert "caused that audience to laugh" in underlined
    assert "nearly impossible" in bold
    assert "to listen to" in bold


def test_callouts_and_indented_quotes_become_blockquotes(sample_document) -> None:
    page6 = next(page for page in sample_document.pages if page.number == 6)
    page15 = next(page for page in sample_document.pages if page.number == 15)
    page38 = next(page for page in sample_document.pages if page.number == 38)
    assert any(block.type == BlockType.QUOTE and block.quote_kind == "callout" for block in page6.blocks)
    assert any(block.type == BlockType.QUOTE and block.quote_kind == "callout" for block in page15.blocks)
    quote_lines = [
        line.text
        for block in page38.blocks
        if block.type == BlockType.QUOTE and block.quote_kind == "line_quotes"
        for line in block.lines
        if line.text
    ]
    assert quote_lines == [
        "“Imagine if…”",
        "“Wouldn’t it be weird if…”",
        "“One day I’m going to…”",
        "“I had a dream that I…”",
    ]


def test_toc_is_responsive_nested_list_without_dot_leaders(sample_document) -> None:
    toc_lines = [
        line
        for page in sample_document.pages
        if page.number in {3, 4}
        for block in page.blocks
        if block.type == BlockType.TOC_ENTRY
        for line in block.lines
        if line.toc_title
    ]
    assert {line.toc_level for line in toc_lines} >= {0, 1, 2}
    markdown = render_markdown(sample_document)
    toc_markdown = markdown.split("# Part One", 1)[0]
    assert "....." not in toc_markdown
    assert "  - Introduction" in toc_markdown
    assert "    - the coincidental comedian" in toc_markdown
    assert "<u>something caused that audience to laugh</u>" in markdown
    assert "**nearly impossible to listen to.**" in markdown
    assert "> “Imagine if…”\n\n> “Wouldn’t it be weird if…”" in markdown
    assert re.search(r"\w\*\*\w", markdown) is None
