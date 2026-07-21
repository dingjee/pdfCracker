import pytest

from pdfcracker.models import BBox, Block, BlockType, Document, Line, MergeDecision, Page
from pdfcracker.profile import DocumentProfile, apply_profile


def _document() -> Document:
    bbox = BBox(x0=0, y0=0, x1=100, y1=20)
    blocks = [
        Block(
            id=f"p0001-b000{index}",
            page=1,
            source_block_index=index,
            bbox=bbox,
            bbox_norm=BBox(x0=0, y0=0, x1=1, y1=0.2),
            raw_text=text,
            text=text,
            lines=[Line(text=text, bbox=bbox)],
            type=BlockType.HEADING,
            confidence=0.8,
            heading_level=3,
        )
        for index, text in enumerate(("First", "Second"))
    ]
    return Document(
        source_path="sample.pdf",
        source_sha256="a" * 64,
        source_page_count=1,
        selected_pages=[1],
        pages=[Page(number=1, source_index=0, width=100, height=100, blocks=blocks)],
        body_font_size=12,
        merges=[
            MergeDecision(
                from_block=blocks[0].id,
                to_block=blocks[1].id,
                from_page=1,
                to_page=1,
                decision="review",
                score=0.6,
            )
        ],
    )


def test_profile_applies_auditable_overrides() -> None:
    document = _document()
    profile = DocumentProfile(
        source_sha256="a" * 64,
        merge_overrides={
            "p0001-b0000->p0001-b0001": {
                "decision": "keep_separate",
                "reason": "human_review",
            }
        },
        block_overrides={
            "p0001-b0001": {"heading_level": 2, "reason": "visual_hierarchy"}
        },
    )
    counts = apply_profile(document, profile)
    assert counts == {"merge": 1, "block": 1, "image": 0, "table": 0}
    assert document.merges[0].decision == "keep_separate"
    assert document.pages[0].blocks[1].heading_level == 2
    assert "profile_override:human_review" in document.merges[0].reasons


def test_profile_rejects_wrong_source() -> None:
    with pytest.raises(ValueError, match="source_sha256"):
        apply_profile(_document(), DocumentProfile(source_sha256="b" * 64))
