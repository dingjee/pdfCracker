"""Tests for the footnote-mark translation layer (footnote_marks)."""

from __future__ import annotations

import pytest

from pdfcracker.footnote_marks import translate_footnotes


def test_bound_references_become_anchor_links_with_sequential_numbers():
    md = (
        "# 第一章\n\n"
        "康德[^p24-n2]、柏格森[^p24-n3]与[^p24-n2]再次出现。\n\n"
        "[^p24-n2]: 伊曼努尔·康德(Immanuel Kant)(1724—1804)。\n\n"
        "[^p24-n3]: 亨利·柏格森(Henri Bergson)(1859—1941)。\n"
    )
    result = translate_footnotes(md)
    assert '康德<sup><a href="#fn-p24-n2" id="fnref-p24-n2">[1]</a></sup>' in result.markdown
    assert '柏格森<sup><a href="#fn-p24-n3" id="fnref-p24-n3">[2]</a></sup>' in result.markdown
    # the second occurrence of the same label reuses the number
    assert '[1]</a></sup>再次出现' in result.markdown
    assert (
        '<a id="fn-p24-n2"></a>[1] 伊曼努尔·康德(Immanuel Kant)(1724—1804)。 '
        '<a href="#fnref-p24-n2">↩</a>' in result.markdown
    )
    assert result.numbers == {"p24-n2": 1, "p24-n3": 2}
    assert result.complete


def test_term_text_is_never_swallowed_by_the_reference():
    md = (
        "维纳·贝尔格曼（Werner Bergmann）[^p21-n5]早在80年代初。\n\n"
        "[^p21-n5]: 维纳·贝尔格曼(1950—)，德国社会学家。\n"
    )
    result = translate_footnotes(md)
    assert "维纳·贝尔格曼（Werner Bergmann）<sup>" in result.markdown
    assert "贝尔格曼（Werner Bergmann）" in result.markdown


def test_latex_superscript_wrapper_is_unwrapped():
    md = (
        "这样的哲学家和弗雷德里克·詹明信(Fredric Jameson)**（引文）$^{[^p39-n3]}$继续。\n\n"
        "[^p39-n3]: Jameson, 1998 年, 第 50 页。\n"
    )
    result = translate_footnotes(md)
    assert "$^{" not in result.markdown
    assert "（引文）<sup><a href=" in result.markdown


def test_unbound_definitions_are_numbered_after_bound_ones_and_reported():
    md = (
        "正文引用[^p20-n4]。\n\n"
        "[^p20-n4]: 尤尔根·哈贝马斯(1929—)。\n\n"
        "[^p23-n3]: 安东尼·吉登斯(1938—)，没有在文中绑定。\n"
    )
    result = translate_footnotes(md)
    assert result.unbound_definitions == ["p23-n3"]
    assert result.numbers == {"p20-n4": 1, "p23-n3": 2}
    # the unbound definition carries an anchor but no back link
    assert '<a id="fn-p23-n3"></a>[2] 安东尼·吉登斯' in result.markdown
    assert "fnref-p23-n3" not in result.markdown


def test_raw_asterisk_runs_are_neutralised_and_recorded():
    md = (
        "斯汀*、康德[^p24-n2]等人。\n\n"
        "[^p24-n2]: 康德注释。\n"
    )
    result = translate_footnotes(md)
    assert "斯汀＊、康德<sup>" in result.markdown
    assert "*" not in result.markdown.replace("＊", "")
    assert len(result.neutralised_marks) == 1
    assert result.neutralised_marks[0]["mark"] == "*"


def test_longer_asterisk_series_neutralised_with_matching_width():
    md = "麦克塔格特****、韦伯*****之后。\n\n[^p24-n2]: 注。\n"
    result = translate_footnotes(md)
    assert "麦克塔格特＊＊＊＊、韦伯＊＊＊＊＊之后" in result.markdown


def test_no_footnote_syntax_left_behind():
    md = (
        "引文[^p39-n3]与脚注[^p24-n2]。\n\n"
        "[^p39-n3]: 三。\n\n"
        "[^p24-n2]: 二。\n"
    )
    result = translate_footnotes(md)
    assert result.complete
    assert "[^" not in result.markdown


def test_blank_section_is_untouched():
    result = translate_footnotes("")
    assert result.markdown == ""
    assert result.complete
    assert result.numbers == {}


@pytest.mark.parametrize(
    "label",
    ["p20-n4", "p1-n1", "p444-n19"],
)
def test_labels_with_hyphens_and_digits_resolve(label):
    md = f"正文[^{label}]。\n\n[^{label}]: 注。\n"
    result = translate_footnotes(md)
    assert f'href="#fn-{label}"' in result.markdown
    assert f'<a id="fn-{label}"></a>' in result.markdown
    assert result.complete
