"""Tests for the reviewed-join application engine."""

from pdfcracker.joins import (
    CrossPageJoin,
    FlowItem,
    PageFlow,
    SamePageJoin,
    apply_reviewed_joins,
    ends_sentence,
)


def flow(page: int, *texts: str) -> PageFlow:
    return PageFlow(
        page,
        [FlowItem("paragraph", f"page-{page:04d}-b{index:04d}", text) for index, text in enumerate(texts)],
    )


def test_sentence_end_detection_ignores_trailing_note_marks() -> None:
    assert ends_sentence("到此为止。")
    assert ends_sentence("完整的一句话！")
    assert ends_sentence("到此为止。①")
    assert ends_sentence("带脚注的结尾。[^p0004-2]")
    assert not ends_sentence("句子还没有结束")
    assert not ends_sentence("句子没有结束①")
    assert not ends_sentence("例如")


def test_same_page_join_merges_blocks_around_a_figure() -> None:
    page = PageFlow(
        10,
        [
            FlowItem("paragraph", "b1", "前半句"),
            FlowItem("figure", None, ""),
            FlowItem("paragraph", "b2", "后半句。"),
        ],
    )
    result = apply_reviewed_joins([page], [SamePageJoin(10, "b1", "b2", evidence="geometry")], [])

    assert result.issues == []
    assert [(item.kind, item.text) for item in page.items] == [
        ("paragraph", "前半句后半句。"),
        ("figure", ""),
    ]
    assert result.redirects == {"b2": "b1"}
    assert [(record.type, record.page, record.left, record.right) for record in result.records] == [
        ("same_page_join", 10, "b1", "b2")
    ]


def test_same_page_join_refuses_blocks_separated_by_text() -> None:
    page = flow(10, "第一段。", "中间的段落。", "第三段。")
    result = apply_reviewed_joins([page], [SamePageJoin(10, "page-0010-b0000", "page-0010-b0002")], [])

    assert [issue.kind for issue in result.issues] == ["same_page_join_blocks_not_adjacent"]
    assert [item.text for item in page.items] == ["第一段。", "中间的段落。", "第三段。"]
    assert result.records == []


def test_cross_page_seam_chains_after_a_same_page_join() -> None:
    # One paragraph split as [b4][b5] on page N and continues on page N+1.
    left_page = flow(20, "第一部分，", "第二部分，")
    right_page = flow(21, "跨页的结尾。")
    result = apply_reviewed_joins(
        [left_page, right_page],
        [SamePageJoin(20, "page-0020-b0000", "page-0020-b0001")],
        [CrossPageJoin(20, "page-0020-b0001", "page-0021-b0000")],
    )

    assert result.issues == []
    assert [item.text for item in left_page.items] == ["第一部分，第二部分，跨页的结尾。"]
    assert right_page.items == []
    cross = result.records[-1]
    assert (cross.type, cross.left, cross.right) == ("cross_page_join", "page-0020-b0000", "page-0021-b0000")


def test_cross_page_seams_chain_across_three_pages_in_source_order() -> None:
    pages = [flow(30, "开头，"), flow(31, "中段，"), flow(32, "结尾。")]
    result = apply_reviewed_joins(
        pages,
        [],
        [
            CrossPageJoin(30, "page-0030-b0000", "page-0031-b0000"),
            CrossPageJoin(31, "page-0031-b0000", "page-0032-b0000"),
        ],
    )

    assert result.issues == []
    assert [item.text for item in pages[0].items] == ["开头，中段，结尾。"]
    assert pages[1].items == [] and pages[2].items == []
    assert [(record.left, record.right) for record in result.records] == [
        ("page-0030-b0000", "page-0031-b0000"),
        ("page-0030-b0000", "page-0032-b0000"),
    ]


def test_cross_page_join_with_a_wrong_target_is_refused() -> None:
    pages = [flow(40, "左页段落"), flow(41, "右页段落")]
    result = apply_reviewed_joins([], [], [CrossPageJoin(40, "page-0040-b0000", "page-0041-b9999")])
    # The flows argument was empty, so even the left page is unavailable.
    assert [issue.kind for issue in result.issues] == ["cross_page_join_source_unavailable"]

    result = apply_reviewed_joins(pages, [], [CrossPageJoin(40, "page-0040-b0000", "page-0041-b9999")])
    assert [issue.kind for issue in result.issues] == ["cross_page_join_source_unavailable"]
    assert [item.text for item in pages[0].items] == ["左页段落"]
    assert [item.text for item in pages[1].items] == ["右页段落"]


def test_unreviewed_seams_keep_their_paragraph_boundary() -> None:
    pages = [flow(50, "独立的一段。"), flow(51, "另一段。")]
    result = apply_reviewed_joins(pages, [], [])

    assert result.records == [] and result.issues == []
    assert [item.text for item in pages[0].items] == ["独立的一段。"]
