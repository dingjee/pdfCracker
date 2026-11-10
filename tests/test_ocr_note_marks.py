"""Tests for the note-mark protection in the OCR Markdown parser.

Scanned academic books mark same-page notes with an asterisk series. A
Markdown parser reads such a run as emphasis and removes it, so the mark
would be lost from the delivered text. The parser therefore protects the
runs while parsing and restores them afterwards.
"""

from __future__ import annotations

from pdfcracker.ocr import (
    NOTE_MARK_SENTINEL,
    parse_page_markdown,
    protect_note_marks,
    restore_note_marks,
)


def test_protect_and_restore_roundtrip():
    text = "科耶夫***和韦伯****所指出，还有 * 一个"
    protected, runs = protect_note_marks(text)
    assert runs == 3
    assert NOTE_MARK_SENTINEL in protected
    assert "*" not in protected
    assert restore_note_marks(protected) == text


def test_runs_longer_than_four_are_left_alone():
    text = "五连星号*****在这里"
    protected, runs = protect_note_marks(text)
    assert runs == 0
    assert protected == text


def test_real_emphasis_is_left_to_the_markdown_parser():
    text = "A *word* and **bold** in text."
    protected, runs = protect_note_marks(text)
    assert runs == 0
    assert protected == text
    blocks = parse_page_markdown(text)
    types = {child["type"] for child in blocks[0].inline}
    assert "em_open" in types


def test_note_marks_are_protected_even_when_they_look_like_delimiters():
    # Every run follows a term and is followed by punctuation: no run can open
    # or close an emphasis span, so all of them are note marks.
    text = "斯汀*、康德**、柏格森***、麦克塔格特****、海德格尔*或米德**等人所提出的假设"
    protected, runs = protect_note_marks(text)
    assert runs == 6
    assert "*" not in protected
    assert restore_note_marks(protected) == text


def test_parser_keeps_note_marks_in_the_block_text():
    raw = "这个侧面尽管都已经由科耶夫***和韦伯****所指出，并且仿佛从一开始就共同表现为是现代性的“潜台词”。"
    blocks = parse_page_markdown(raw)
    assert blocks
    text = " ".join(block.text for block in blocks)
    assert "科耶夫***和韦伯****所指出" in text
    assert "***" not in text.replace("科耶夫***和韦伯****", "")


def test_parser_keeps_note_marks_in_the_markdown():
    raw = "- 斯蒂芬·布罗伊尔(1948—)，德国社会学家。——译者注\n"
    blocks = parse_page_markdown(raw)
    assert blocks
    assert "布罗伊尔" in blocks[0].markdown


def test_parser_still_keeps_footnote_definitions_as_text():
    raw = "正文引用[^note-1]。\n\n[^note-1]: 注释内容。\n"
    blocks = parse_page_markdown(raw)
    text = " ".join(block.text for block in blocks)
    assert "[^note-1]" in text
    assert "[^note-1]: 注释内容。" in text


def test_parser_still_detects_headings_and_lists():
    raw = "## 1. 社会中的时间结构\n\n- 第一项\n- 第二项\n"
    blocks = parse_page_markdown(raw)
    roles = {block.role for block in blocks}
    assert "heading" in roles
    assert any(block.role == "list_item" for block in blocks)
