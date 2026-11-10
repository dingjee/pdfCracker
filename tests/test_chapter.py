"""Regression tests for review-driven chapter assembly.

Every fixture is synthetic: the tests never reuse text from a real book, and
they assert on the artifacts that are actually written.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from pdfcracker import chapter
from pdfcracker.chapter import ChapterPlanError, assemble_chapter

SHA = "a" * 64


def _block(
    page: int,
    index: int,
    role: str = "body",
    text: str = "",
    markdown: str | None = None,
    heading_level: int | None = None,
) -> dict[str, Any]:
    block: dict[str, Any] = {
        "id": f"p{page}-b{index}",
        "page": page,
        "index": index,
        "role": role,
        "text": text,
    }
    if markdown is not None:
        block["markdown"] = markdown
    if heading_level is not None:
        block["heading_level"] = heading_level
    return block


def _document(pages: dict[int, list[dict[str, Any]]], sha256: str = SHA) -> dict[str, Any]:
    return {
        "source": {"sha256": sha256},
        "pages": [
            {"page": number, "status": "ok", "structured_blocks": blocks}
            for number, blocks in sorted(pages.items())
        ],
    }


def _plan(pages: list[int], **overrides: Any) -> dict[str, Any]:
    plan: dict[str, Any] = {
        "source_sha256": SHA,
        "pages": pages,
        "title": "Chapter One",
        "edits": [],
        "joins": [],
    }
    plan.update(overrides)
    return plan


def _write_inputs(tmp_path: Path, document: dict[str, Any], plan: dict[str, Any]) -> tuple[Path, Path]:
    document_path = tmp_path / "document.structured.json"
    document_path.write_text(json.dumps(document, ensure_ascii=False), encoding="utf-8")
    plan_path = tmp_path / "plan.json"
    plan_path.write_text(json.dumps(plan, ensure_ascii=False), encoding="utf-8")
    return document_path, plan_path


def _run(tmp_path: Path, document: dict[str, Any], plan: dict[str, Any]) -> dict[str, Any]:
    document_path, plan_path = _write_inputs(tmp_path, document, plan)
    result = assemble_chapter(document_path, plan_path, tmp_path / "chapter")
    result["output"] = tmp_path / "chapter"
    return result


def _merge(left: str, right: str, separator: str = "", decision: str = "merge") -> dict[str, Any]:
    return {
        "left": left,
        "right": right,
        "decision": decision,
        "separator": separator,
        "reason": "the reviewed cross-page seam was inspected on the render",
        "evidence": ["seam.png"],
        "review_status": "agent_visual_checked_human_pending",
    }


def test_reviewed_merge_joins_the_two_page_halves(tmp_path: Path) -> None:
    document = _document(
        {
            10: [_block(10, 0, text="The first half.", markdown="The first half.")],
            11: [_block(11, 0, text="The second half.", markdown="The second half.")],
        }
    )
    plan = _plan([10, 11], joins=[_merge("p10-b0", "p11-b0")])
    result = _run(tmp_path, document, plan)
    markdown = (result["output"] / "chapter.md").read_text(encoding="utf-8")
    assert "The first half.The second half." in markdown
    assert "The first half.\n\nThe second half." not in markdown
    qa = result["qa"]
    assert qa["known_body_deletions"] == 0
    assert qa["conservation_check"] == "passed"
    assert qa["counts"]["merges"] == 1


def test_keep_separate_decision_keeps_two_paragraphs(tmp_path: Path) -> None:
    document = _document(
        {
            10: [_block(10, 0, text="First sentence.", markdown="First sentence.")],
            11: [_block(11, 0, text="Second sentence.", markdown="Second sentence.")],
        }
    )
    plan = _plan([10, 11], joins=[_merge("p10-b0", "p11-b0", decision="keep_separate")])
    result = _run(tmp_path, document, plan)
    markdown = (result["output"] / "chapter.md").read_text(encoding="utf-8")
    assert "First sentence.\n\nSecond sentence." in markdown
    assert result["qa"]["counts"]["keep_separate"] == 1


def test_footnote_definition_is_relocated_to_the_chapter_end(tmp_path: Path) -> None:
    document = _document(
        {
            10: [_block(10, 0, text="Body text with a note[^1].", markdown="Body text with a note[^1].")],
            11: [_block(11, 0, role="footnote_definition", text="[^1]: The note.", markdown="[^1]: The note.")],
        }
    )
    result = _run(tmp_path, document, plan=_plan([10, 11]))
    markdown = (result["output"] / "chapter.md").read_text(encoding="utf-8")
    assert markdown.rstrip().endswith("[^1]: The note.")
    assert "\\[^1]" not in markdown
    sidecar = json.loads((result["output"] / "chapter.document.json").read_text(encoding="utf-8"))
    relocation = [
        item for item in sidecar["transformations"] if item["type"] == "footnote_relocation"
    ]
    assert relocation and relocation[0]["block_id"] == "p11-b0"
    assert relocation[0]["to"] == "chapter_end"


def test_footnote_pairing_is_reported_from_the_body_reference(tmp_path: Path) -> None:
    document = _document(
        {
            10: [_block(10, 0, text="Body text with a note[^7].", markdown="Body text with a note[^7].")],
            11: [_block(11, 0, role="footnote_definition", text="[^7]: The note.", markdown="[^7]: The note.")],
        }
    )
    result = _run(tmp_path, document, plan=_plan([10, 11]))
    note = result["qa"]["footnotes"][0]
    assert note["label"] == "7"
    assert note["paired"] is True
    assert note["reference_count"] == 1


def test_unreferenced_footnote_definition_is_reported_as_unpaired(tmp_path: Path) -> None:
    document = _document(
        {
            10: [_block(10, 0, text="Body text without a note.", markdown="Body text without a note.")],
            11: [_block(11, 0, role="footnote_definition", text="[^9]: The note.", markdown="[^9]: The note.")],
        }
    )
    result = _run(tmp_path, document, plan=_plan([10, 11]))
    assert result["qa"]["footnotes"][0]["paired"] is False
    assert any("without a matching body reference" in item for item in result["qa"]["warnings"])


def test_missing_join_decision_is_rejected(tmp_path: Path) -> None:
    document = _document(
        {
            10: [_block(10, 0, text="First half.", markdown="First half.")],
            11: [_block(11, 0, text="Second half.", markdown="Second half.")],
        }
    )
    with pytest.raises(ChapterPlanError, match="missing reviewed join decision"):
        _run(tmp_path, document, _plan([10, 11]))
    assert not (tmp_path / "chapter").exists()


def test_merge_across_a_heading_is_rejected(tmp_path: Path) -> None:
    document = _document(
        {
            10: [
                _block(10, 0, text="Body before.", markdown="Body before."),
                _block(10, 1, role="heading", text="Section", markdown="## Section"),
            ],
            11: [_block(11, 0, text="Body after.", markdown="Body after.")],
        }
    )
    plan = _plan([10, 11], joins=[_merge("p10-b0", "p11-b0")])
    with pytest.raises(ChapterPlanError, match="not between adjacent blocks"):
        _run(tmp_path, document, plan)
    assert not (tmp_path / "chapter").exists()


def test_merge_of_a_heading_is_rejected(tmp_path: Path) -> None:
    document = _document(
        {
            10: [_block(10, 0, text="Body.", markdown="Body.")],
            11: [_block(11, 0, role="heading", text="Section", markdown="## Section")],
        }
    )
    plan = _plan([10, 11], joins=[_merge("p10-b0", "p11-b0")])
    with pytest.raises(ChapterPlanError, match="may only merge body blocks"):
        _run(tmp_path, document, plan)


def test_merge_inside_one_page_is_rejected(tmp_path: Path) -> None:
    document = _document(
        {
            10: [
                _block(10, 0, text="First.", markdown="First."),
                _block(10, 1, text="Second.", markdown="Second."),
            ]
        }
    )
    plan = _plan([10], joins=[_merge("p10-b0", "p10-b1")])
    with pytest.raises(ChapterPlanError, match="only allowed across a page boundary"):
        _run(tmp_path, document, plan)


def test_plan_pages_must_be_contiguous(tmp_path: Path) -> None:
    document = _document(
        {
            10: [_block(10, 0, text="First.", markdown="First.")],
            12: [_block(12, 0, text="Third.", markdown="Third.")],
        }
    )
    with pytest.raises(ChapterPlanError, match="contiguous"):
        _run(tmp_path, document, _plan([10, 12]))
    assert not (tmp_path / "chapter").exists()


def test_merge_across_a_missing_page_is_rejected(tmp_path: Path) -> None:
    document = _document(
        {
            10: [_block(10, 0, text="First.", markdown="First.")],
            11: [_block(11, 0, role="heading", text="Section", markdown="## Section")],
            12: [_block(12, 0, text="Third.", markdown="Third.")],
        }
    )
    plan = _plan([10, 11, 12], joins=[_merge("p10-b0", "p12-b0")])
    with pytest.raises(ChapterPlanError, match="not between adjacent blocks"):
        _run(tmp_path, document, plan)


def test_edit_is_applied_and_recorded_with_a_tombstone(tmp_path: Path) -> None:
    document = _document(
        {
            10: [_block(10, 0, text="A mistyped word here.", markdown="A mistyped word here.")],
        }
    )
    plan = _plan(
        [10],
        edits=[
            {
                "block_id": "p10-b0",
                "before": "mistyped",
                "after": "misread",
                "kind": "ocr_correction",
                "reason": "the render shows the corrected word",
                "evidence": ["render-10.png"],
                "review_status": "agent_visual_checked_human_pending",
            }
        ],
    )
    result = _run(tmp_path, document, plan)
    markdown = (result["output"] / "chapter.md").read_text(encoding="utf-8")
    assert "A misread word here." in markdown
    assert "mistyped" not in markdown
    sidecar = json.loads((result["output"] / "chapter.document.json").read_text(encoding="utf-8"))
    edit = sidecar["edits"][0]
    assert edit["before"] == "mistyped"
    assert edit["after"] == "misread"
    assert edit["kind"] == "ocr_correction"
    assert edit["review_status"] == "agent_visual_checked_human_pending"
    assert edit["tombstone"]["removed_text"] == "mistyped"
    assert edit["tombstone"]["replacement_text"] == "misread"
    assert result["qa"]["known_body_deletions"] == 0


def test_edit_must_match_exactly_once(tmp_path: Path) -> None:
    document = _document(
        {10: [_block(10, 0, text="word word", markdown="word word")]},
    )
    plan = _plan(
        [10],
        edits=[
            {
                "block_id": "p10-b0",
                "before": "word",
                "after": "term",
                "reason": "ambiguous match",
                "evidence": ["render-10.png"],
            }
        ],
    )
    with pytest.raises(ChapterPlanError, match="must match exactly once"):
        _run(tmp_path, document, plan)


def test_edits_given_out_of_source_order_are_all_applied(tmp_path: Path) -> None:
    document = _document(
        {10: [_block(10, 0, text="alpha beta", markdown="alpha beta")]},
    )
    plan = _plan(
        [10],
        edits=[
            {
                "block_id": "p10-b0",
                "before": "beta",
                "after": "gamma",
                "reason": "second source position listed first",
                "evidence": ["render-10.png"],
            },
            {
                "block_id": "p10-b0",
                "before": "alpha",
                "after": "delta",
                "reason": "first source position listed second",
                "evidence": ["render-10.png"],
            },
        ],
    )
    result = _run(tmp_path, document, plan)
    markdown = (result["output"] / "chapter.md").read_text(encoding="utf-8")
    assert "delta gamma" in markdown
    assert result["qa"]["known_body_deletions"] == 0


def test_inline_markup_is_preserved_and_headings_are_not_rebuilt(tmp_path: Path) -> None:
    document = _document(
        {
            10: [
                _block(10, 0, text="Plain text", markdown="Text with **bold** and *italic*."),
                _block(10, 1, role="heading", text="Section", markdown="## Section", heading_level=1),
            ]
        }
    )
    result = _run(tmp_path, document, _plan([10], title=""))
    markdown = (result["output"] / "chapter.md").read_text(encoding="utf-8")
    assert "Text with **bold** and *italic*." in markdown
    assert "\n## Section" in markdown
    assert "\n# Section" not in markdown
    sidecar = json.loads((result["output"] / "chapter.document.json").read_text(encoding="utf-8"))
    heading = next(block for block in sidecar["blocks"] if block["id"] == "p10-b1")
    assert heading["original_heading_level"] == 1
    assert heading["heading_level"] == 2


def test_the_chapter_title_maps_to_the_source_heading(tmp_path: Path) -> None:
    document = _document(
        {
            10: [
                _block(10, 0, role="heading", text="Chapter One", markdown="# Chapter One"),
                _block(10, 1, text="Body.", markdown="Body."),
            ]
        }
    )
    result = _run(tmp_path, document, _plan([10], title="Chapter One"))
    markdown = (result["output"] / "chapter.md").read_text(encoding="utf-8")
    assert markdown.count("# Chapter One") == 1
    sidecar = json.loads((result["output"] / "chapter.document.json").read_text(encoding="utf-8"))
    title_paragraph = sidecar["paragraphs"][0]
    assert title_paragraph["kind"] == "title"
    assert title_paragraph["source_pages"] == [10]
    assert title_paragraph["source_block_ids"] == ["p10-b0"]
    assert title_paragraph["components"][0]["block_id"] == "p10-b0"
    block = next(item for item in sidecar["blocks"] if item["id"] == "p10-b0")
    assert block["disposition"] == "in_flow"
    assert not [
        item for item in sidecar["transformations"] if item["type"] == "title_duplicate_suppressed"
    ]


def test_repeated_source_title_headings_are_all_retained(tmp_path: Path) -> None:
    document = _document(
        {
            10: [
                _block(10, 0, role="heading", text="Chapter One", markdown="# Chapter One"),
                _block(10, 1, text="Body.", markdown="Body."),
            ],
            11: [_block(11, 0, role="heading", text="Chapter One", markdown="# Chapter One")],
        }
    )
    result = _run(tmp_path, document, _plan([10, 11]))
    markdown = (result["output"] / "chapter.md").read_text(encoding="utf-8")
    assert markdown.count("# Chapter One") == 2
    sidecar = json.loads((result["output"] / "chapter.document.json").read_text(encoding="utf-8"))
    represented = [
        block_id
        for paragraph in sidecar["paragraphs"]
        for block_id in paragraph["source_block_ids"]
    ]
    assert "p10-b0" in represented
    assert "p11-b0" in represented
    assert not [
        item for item in sidecar["transformations"] if item["type"] == "title_duplicate_suppressed"
    ]


def test_a_plan_title_without_a_source_heading_stays_synthetic(tmp_path: Path) -> None:
    document = _document({10: [_block(10, 0, text="Body.", markdown="Body.")]})
    result = _run(tmp_path, document, _plan([10], title="Chapter One"))
    markdown = (result["output"] / "chapter.md").read_text(encoding="utf-8")
    assert markdown.startswith("# Chapter One\n")
    sidecar = json.loads((result["output"] / "chapter.document.json").read_text(encoding="utf-8"))
    assert sidecar["paragraphs"][0]["source_block_ids"] == []


def test_page_furniture_roles_are_preserved_instead_of_deleted(tmp_path: Path) -> None:
    document = _document(
        {
            10: [
                _block(10, 0, role="header", text="Running head", markdown="Running head"),
                _block(10, 1, text="Body.", markdown="Body."),
            ]
        }
    )
    result = _run(tmp_path, document, _plan([10]))
    markdown = (result["output"] / "chapter.md").read_text(encoding="utf-8")
    assert "Running head" in markdown
    sidecar = json.loads((result["output"] / "chapter.document.json").read_text(encoding="utf-8"))
    block = next(item for item in sidecar["blocks"] if item["id"] == "p10-b0")
    assert block["disposition"] == "in_flow"
    assert not [
        item for item in sidecar["transformations"] if item["type"] == "exclusion"
    ]


def test_qa_requires_human_review_and_never_approves_the_pilot(tmp_path: Path) -> None:
    document = _document({10: [_block(10, 0, text="Body.", markdown="Body.")]})
    plan = _plan([10], human_review_required=False, pilot_approved=True)
    result = _run(tmp_path, document, plan)
    qa = result["qa"]
    assert qa["human_review_required"] is True
    assert qa["pilot_approved"] is False
    assert qa["known_body_deletions"] == 0
    assert qa["review_status"] == "pending"


def test_a_failed_quality_check_writes_no_artifacts(tmp_path: Path, monkeypatch) -> None:
    document = _document({10: [_block(10, 0, text="Body.", markdown="Body.")]})
    document_path, plan_path = _write_inputs(tmp_path, document, _plan([10]))

    def failing(*args: Any, **kwargs: Any) -> tuple[dict[str, Any], list[dict[str, Any]]]:
        return {"known_body_deletions": 4}, [
            {"name": "body_characters_preserved", "status": "failed", "detail": "injected"}
        ]

    monkeypatch.setattr(chapter, "_conservation_report", failing)
    with pytest.raises(ChapterPlanError, match="quality checks failed"):
        assemble_chapter(document_path, plan_path, tmp_path / "chapter")
    assert not (tmp_path / "chapter").exists()


def test_existing_artifacts_are_preserved(tmp_path: Path) -> None:
    document = _document({10: [_block(10, 0, text="Body.", markdown="Body.")]})
    result = _run(tmp_path, document, _plan([10]))
    markdown_path = result["output"] / "chapter.md"
    before = markdown_path.read_text(encoding="utf-8")
    with pytest.raises(ChapterPlanError, match="already exist"):
        _run(tmp_path, document, _plan([10]))
    assert markdown_path.read_text(encoding="utf-8") == before


def test_a_plan_for_another_document_is_rejected(tmp_path: Path) -> None:
    document = _document({10: [_block(10, 0, text="Body.", markdown="Body.")]}, sha256="b" * 64)
    with pytest.raises(ChapterPlanError, match="does not match"):
        _run(tmp_path, document, _plan([10]))
    assert not (tmp_path / "chapter").exists()


def test_the_output_directory_must_be_separate_from_the_sources(tmp_path: Path) -> None:
    document = _document({10: [_block(10, 0, text="Body.", markdown="Body.")]})
    document_path, plan_path = _write_inputs(tmp_path, document, _plan([10]))
    with pytest.raises(ChapterPlanError, match="output directory"):
        assemble_chapter(document_path, plan_path, tmp_path)
