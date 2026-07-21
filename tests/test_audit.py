from pdfcracker.audit import audit_document
from pdfcracker.models import (
    BBox,
    Block,
    BlockType,
    Document,
    EmbeddedImage,
    Line,
    MergeDecision,
    Page,
)


def _document() -> Document:
    bbox = BBox(x0=0, y0=0, x1=100, y1=20)
    block = Block(
        id="p0001-b0001",
        page=1,
        source_block_index=0,
        bbox=bbox,
        bbox_norm=BBox(x0=0, y0=0, x1=1, y1=0.2),
        raw_text="Readable body.",
        text="Readable body.",
        lines=[Line(text="Readable body.", bbox=bbox)],
        type=BlockType.PARAGRAPH,
        confidence=0.9,
        evidence=["body_font_ratio=1.0"],
    )
    return Document(
        source_path="sample.pdf",
        source_sha256="a" * 64,
        source_page_count=1,
        selected_pages=[1],
        pages=[Page(number=1, source_index=0, width=100, height=100, blocks=[block])],
        body_font_size=12,
    )


def test_audit_allows_clean_full_document(tmp_path) -> None:
    report = audit_document(_document(), "Readable body.\n", tmp_path, require_full=True)
    assert report.publishable is True
    assert report.status == "publishable"
    assert report.quality["text_integrity"] == 1.0


def test_audit_routes_ambiguous_merge_to_review(tmp_path) -> None:
    document = _document()
    document.merges.append(
        MergeDecision(
            from_block="p0001-b0001",
            to_block="p0001-b0001",
            from_page=1,
            to_page=1,
            decision="review",
            score=0.6,
            reasons=["ambiguous"],
        )
    )
    report = audit_document(document, "Readable body.\n", tmp_path)
    assert report.publishable is True
    assert report.status == "review_recommended"
    assert any(issue.code == "ambiguous_paragraph_merge" for issue in report.issues)


def test_audit_blocks_missing_image_asset(tmp_path) -> None:
    document = _document()
    document.images.append(
        EmbeddedImage(
            id="p0001-img001",
            page=1,
            object_name="Image1",
            asset_path="assets/images/missing.png",
            sha256="b" * 64,
            bbox=BBox(x0=0, y0=0, x1=20, y1=20),
            bbox_norm=BBox(x0=0, y0=0, x1=0.2, y1=0.2),
            pixel_width=20,
            pixel_height=20,
            format="png",
        )
    )
    report = audit_document(document, "Readable body.\n", tmp_path)
    assert report.publishable is False
    assert any(issue.code == "missing_embedded_image_asset" for issue in report.issues)
