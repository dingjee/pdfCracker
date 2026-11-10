"""Tests for the independent line-cache coverage review."""

from pdfcracker.coverage_review import flag_missing_lines, normalize_text

PAGE = "电影诞生于十九世纪末的巴黎。卢米埃尔兄弟在地下室放映影片，观众付费入场。"


def test_contained_and_similar_lines_are_not_flagged() -> None:
    lines = [
        "卢米埃尔兄弟在地下室放映影片，观众付费入场。",
        "卢米埃尔兄弟在地下室放映影片，观众买票入场。",  # one misread character
    ]
    assert flag_missing_lines(PAGE, lines) == []


def test_a_missing_line_is_flagged_with_its_overlap() -> None:
    missing = "这一行完全不在页面文本之中，是真正遗漏的独立证据。"
    flagged = flag_missing_lines(PAGE, [missing])
    assert len(flagged) == 1
    assert flagged[0].text == missing
    assert flagged[0].bigram_overlap < 0.55


def test_short_and_low_confidence_lines_are_skipped() -> None:
    assert flag_missing_lines(PAGE, ["短行", ("完全不同的低置信度行内容文本", 0.4)]) == []


def test_normalization_ignores_case_width_and_punctuation() -> None:
    assert normalize_text("Ａｂｃ１２３，你好。") == "abc123你好"
    assert normalize_text("") == ""


def test_thresholds_are_tunable() -> None:
    drifting = "电影诞生于十八世纪末的柏林。卢米埃尔兄弟在地下室放映影片。"  # several edits
    assert flag_missing_lines(PAGE, [drifting], bigram_threshold=0.55) == []
    assert flag_missing_lines(PAGE, [drifting], bigram_threshold=0.95) != []
