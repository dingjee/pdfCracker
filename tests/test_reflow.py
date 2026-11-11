"""Synthetic geometry tests for pdfcracker.reflow.

Fabricates Vision-style page records (normalized bboxes) that exercise each
rule of the geometry-authoritative paragraph reflow, including every pitfall
that produced wrong or missed merges on the 427-page delivery:

* flush continuation merges (same page and across a page turn, including a
  sentence-final period at the line end);
* indented paragraph starts never merge;
* the bottom-note area is excluded via the first circled-marker line, while
  body text on note-free pages may extend below y=0.86;
* display pages (only short stacked lines) freeze merging;
* a far-right start only counts as a wrap column with figure evidence;
* enumeration items and source notes never merge.
"""

from __future__ import annotations

import json
from pathlib import Path


from pdfcracker.reflow import (
    DEFAULT_EXCLUDED_REGIONS,
    BlockGeom,
    find_excluded,
    load_figures,
    load_pages,
    reflow_blocks,
    reflow_markdown,
    text_fallback_joinable,
)

MARGIN = 0.150
INDENT = 0.197
RIGHT = 0.910


def line(text: str, x0: float, x1: float, y0: float,
         h: float = 0.024) -> dict:
    return {"bbox": [x0, y0, x1, y0 + h], "text": text, "confidence": 1}


def body_page(num: int, lines: list[dict], figures=None) -> dict:
    return {"num": num, "lines": lines, "figures": figures or []}


def make_vision(tmp: Path, pages: list[dict]) -> Path:
    d = tmp / "vision"
    d.mkdir(exist_ok=True)
    for p in pages:
        (d / f"apple-vision-page-{p['num']:04d}.json").write_text(
            json.dumps(p["lines"], ensure_ascii=False), encoding="utf-8")
    return d


def book_pages() -> list[dict]:
    return [
        body_page(1, [
            line("章题", 0.4, 0.6, 0.05),                    # header zone
            line("第一段第一行缩进开始的内容在", INDENT, RIGHT, 0.12),
            line("本行继续直到行末被填满为止", MARGIN, RIGHT, 0.16),
            line("段一结束", MARGIN, 0.4, 0.20),             # short final
            line("第二段首行缩进另一段落开始", INDENT, RIGHT, 0.26),
            line("第二段中间一行继续直到行末", MARGIN, RIGHT, 0.30),
            line("第二段末行停在半途", MARGIN, 0.5, 0.34),
        ]),
        body_page(2, [
            line("续行顶格接上文继续", MARGIN, 0.45, 0.12),  # flush, P short?
            line("第三段首行缩进", INDENT, RIGHT, 0.18),
            line("第三段继续直到行末。", MARGIN, RIGHT, 0.22),
            line("① 这是页脚脚注不会被并入正文。", MARGIN + 0.02, 0.5, 0.86),
        ]),
    ]


def load(tmp: Path, pages: list[dict]) -> dict[int, object]:
    d = make_vision(tmp, pages)
    return load_pages(d)


def test_flush_same_page_merge_and_indent_split(tmp_path: Path):
    pages = load(tmp_path, book_pages())
    blocks = [
        "# 标题",
        "第一段第一行缩进开始的内容在本行继续直到行末被填满为止段一结束",
        "第二段首行缩进另一段落开始第二段中间一行继续直到行末",
    ]
    out, report = reflow_blocks(blocks, pages, sorted(pages))
    # block 1 ends short (real paragraph end): block 2 is indented anyway
    assert out == ["# 标题",
                   "第一段第一行缩进开始的内容在本行继续直到行末被填满为止段一结束",
                   "第二段首行缩进另一段落开始第二段中间一行继续直到行末"]


def test_cross_page_flush_merges_even_after_period(tmp_path: Path):
    pages = load(tmp_path, book_pages())
    # page 2 first line is flush: continuation of page 1's last paragraph,
    # which ended mid-line (short final line) — but a true continuation
    # requires P's last line to reach the right margin, so give P one.
    blocks = [
        "第二段首行缩进另一段落开始第二段中间一行继续直到行末",
        "续行顶格接上文继续",
    ]
    out, _ = reflow_blocks(blocks, pages, sorted(pages))
    assert len(out) == 2  # P's last line is short: no merge, conservative

    pages2 = load(tmp_path, [
        body_page(1, [
            line("段落开始", INDENT, RIGHT, 0.12),
            line("句子在本行末尾以句号结束。", MARGIN, RIGHT, 0.16),
        ]),
        body_page(2, [
            line("下一页顶格续行继续该段落", MARGIN, 0.45, 0.12),
        ]),
    ])
    blocks2 = ["段落开始句子在本行末尾以句号结束。", "下一页顶格续行继续该段落"]
    out2, report2 = reflow_blocks(blocks2, pages2, sorted(pages2))
    assert out2 == ["段落开始句子在本行末尾以句号结束。下一页顶格续行继续该段落"]
    assert report2["stats"]["merged_geometry"] == 1


def test_footnote_zone_excluded_from_last_line(tmp_path: Path):
    # Page 1 ends mid-paragraph and carries circled footnotes below; P's
    # last line must be the body line, not the footnote ending short.
    pages = load(tmp_path, [
        body_page(1, [
            line("段落开始", INDENT, RIGHT, 0.12),
            line("正文最后一行填满整行宽度", MARGIN, RIGHT, 0.80),
            line("① 脚注一内容很短。", 0.17, 0.4, 0.86),
        ]),
        body_page(2, [
            line("续行顶格继续该段落", MARGIN, 0.5, 0.12),
        ]),
    ])
    out, _ = reflow_blocks(
        ["段落开始正文最后一行填满整行宽度", "续行顶格继续该段落"],
        pages, sorted(pages))
    assert len(out) == 1


def test_note_free_page_body_below_y086_counts(tmp_path: Path):
    pages = load(tmp_path, [
        body_page(1, [
            line("段落开始", INDENT, RIGHT, 0.12),
            line("正文可以延伸到很低的位置直到填满", MARGIN, RIGHT, 0.87),
            line("3", 0.5, 0.52, 0.93),  # bare page number
        ]),
        body_page(2, [
            line("续行顶格继续", MARGIN, 0.4, 0.12),
        ]),
    ])
    out, _ = reflow_blocks(
        ["段落开始正文可以延伸到很低的位置直到填满", "续行顶格继续"],
        pages, sorted(pages))
    assert len(out) == 1


def test_display_page_freezes_merging(tmp_path: Path):
    pages = load(tmp_path, [
        body_page(1, [
            line("第一部分", 0.40, 0.55, 0.12),
            line("做电影史", 0.41, 0.60, 0.16),
            line("第一章 概述", 0.40, 0.62, 0.20),
            line("第二章 方法", 0.40, 0.62, 0.24),
        ]),
    ])
    out, _ = reflow_blocks(["第一部分", "做电影史", "第一章 概述", "第二章 方法"],
                           pages, sorted(pages))
    assert len(out) == 4


def test_wrap_column_requires_figure_evidence(tmp_path: Path):
    fig = [(0.01, 0.30, 0.49, 0.55)]
    with_fig = load(tmp_path, [
        body_page(1, [
            line("段落开始", INDENT, RIGHT, 0.12),
            line("正文行填满整行宽度", MARGIN, RIGHT, 0.16),
            line("绕图栏续行接上文", 0.52, 0.88, 0.35),
        ]),
    ])
    for p in with_fig.values():
        p.figures = fig
    out, _ = reflow_blocks(
        ["段落开始正文行填满整行宽度", "绕图栏续行接上文"],
        with_fig, sorted(with_fig))
    assert len(out) == 1

    no_fig = load(tmp_path, [
        body_page(1, [
            line("段落开始", INDENT, RIGHT, 0.12),
            line("正文行填满整行宽度", MARGIN, RIGHT, 0.16),
            line("列表项缩进在右侧", 0.52, 0.88, 0.35),
        ]),
    ])
    out2, _ = reflow_blocks(
        ["段落开始正文行填满整行宽度", "列表项缩进在右侧"],
        no_fig, sorted(no_fig))
    assert len(out2) == 2


def test_enumeration_and_note_guards(tmp_path: Path):
    pages = load(tmp_path, [
        body_page(1, [
            line("段落开始", INDENT, RIGHT, 0.12),
            line("正文行填满整行宽度", MARGIN, RIGHT, 0.16),
            line("一、枚举项顶格出现", MARGIN, RIGHT, 0.22),
        ]),
        body_page(2, [
            line("段落开始二", INDENT, RIGHT, 0.12),
            line("正文行填满整行宽度", MARGIN, RIGHT, 0.16),
            line("- 本文发表于某刊2009年第4期", MARGIN, 0.6, 0.22),
        ]),
    ])
    out, _ = reflow_blocks(
        ["段落开始正文行填满整行宽度", "一、枚举项顶格出现"],
        pages, sorted(pages))
    assert len(out) == 2
    out2, _ = reflow_blocks(
        ["段落开始二正文行填满整行宽度", "- 本文发表于某刊2009年第4期"],
        pages, sorted(pages))
    assert len(out2) == 2


def test_excluded_regions_frozen(tmp_path: Path):
    blocks = ["# 封面与出版资料", "ISBN 978-7-5100-1804-6",
              "定价：36.00元", "# 作者序", "正文开始"]
    ranges = find_excluded(blocks, DEFAULT_EXCLUDED_REGIONS)
    assert (0, 3) in ranges


def test_text_fallback_predicate():
    assert text_fallback_joinable("从而对上述问题起了推波", "助澜的作用。")
    assert not text_fallback_joinable("句子已经结束。", "新段落开始")
    assert not text_fallback_joinable("如下：", "闻东安市场开明电影院")
    assert not text_fallback_joinable("可也", "①脚注内容")


def test_reflow_markdown_integrity(tmp_path: Path):
    pages = load(tmp_path, [
        body_page(1, [
            line("段落开始", INDENT, RIGHT, 0.12),
            line("正文直到行末", MARGIN, RIGHT, 0.16),
        ]),
        body_page(2, [
            line("续行顶格继续", MARGIN, 0.4, 0.12),
        ]),
    ])
    md = "# 标题\n\n段落开始正文直到行末\n\n续行顶格继续\n"
    result, report = reflow_markdown(md, pages,
                                     excluded_regions=DEFAULT_EXCLUDED_REGIONS)
    assert "段落开始正文直到行末续行顶格继续" in result
    n_in = report["integrity"]["nonspace_chars_in"]
    n_out = report["integrity"]["nonspace_chars_out"]
    assert n_in == n_out


def test_load_figures_manifest(tmp_path: Path):
    manifest = tmp_path / "figures.json"
    manifest.write_text(json.dumps(
        [{"page": 3, "bbox_norm": [0.0, 0.1, 0.4, 0.5]}]), encoding="utf-8")
    figs = load_figures(manifest)
    assert 3 in figs
    assert figs[3] == [(0.0, 0.1, 0.4, 0.5)]


def test_blockgeom_classify_kinds():
    assert BlockGeom("# 标题", 0).kind == "heading"
    assert BlockGeom("![图](images/a.png)", 0).kind == "image"
    assert BlockGeom("[^n1]: 注", 0).kind == "notedef"
    assert BlockGeom("①脚注内容", 0).kind == "note"
    assert BlockGeom("正文段落", 0).kind == "body"
