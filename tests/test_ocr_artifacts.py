"""Tests for the OCR raw-markup screen."""

from pdfcracker.ocr_artifacts import find_markup_artifacts, has_markup_artifacts

# Real fragments of a table page whose OCR model emitted pseudo-HTML cells.
PSEUDO_TABLE = "<fcel>供方<fcel>求方<nl><fcel>原材料<fcel>价格的灵活性<nl>"
PSEUDO_LIST = "<fcel>制定价格<nl><fcel>营销策略<nl>"


def test_finds_pseudo_cell_and_newline_tags() -> None:
    artifacts = find_markup_artifacts(PSEUDO_TABLE)
    assert [artifact.tag for artifact in artifacts] == ["fcel", "fcel", "nl", "fcel", "fcel", "nl"]
    assert all(artifact.text.endswith(">") for artifact in artifacts)


def test_finds_truncated_and_closing_tags() -> None:
    artifacts = find_markup_artifacts("正文<fc 继续")
    assert [artifact.text for artifact in artifacts] == ["<fc"]
    assert find_markup_artifacts("结束</b>之后") [0].tag == "b"


def test_plain_text_and_comparisons_are_not_flags() -> None:
    for text in ("普通的中文正文。", "a < b 与 b > a 的比较", "温度 <3 摄氏度", "x<=10 且 y>=2"):
        assert find_markup_artifacts(text) == [], text
        assert not has_markup_artifacts(text)


def test_has_markup_artifacts_matches_find() -> None:
    assert has_markup_artifacts(PSEUDO_LIST)
    assert not has_markup_artifacts("完全干净的文本")
