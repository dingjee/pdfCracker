from pdfcracker.audit import audit_document
from pdfcracker.models import (
    BBox,
    Block,
    BlockType,
    Document,
    EmbeddedImage,
    Line,
    MergeDecision,
    NativeTable,
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


def test_audit_rejects_body_that_exists_only_in_sidecar(tmp_path) -> None:
    report = audit_document(_document(), "# A plausible but incomplete output\n", tmp_path)
    assert not report.publishable
    assert any(issue.code == "missing_rendered_text" for issue in report.issues)
    assert report.quality["text_integrity"] == 0


def test_audit_rejects_empty_image_reference_without_manifest_entry(tmp_path) -> None:
    report = audit_document(_document(), "Readable body.\n\n![Lost image]()", tmp_path)
    assert not report.publishable
    assert any(issue.code == "invalid_markdown_image" for issue in report.issues)


def test_audit_resolves_images_from_the_delivered_markdown_directory(tmp_path) -> None:
    (tmp_path / "assets").mkdir()
    (tmp_path / "assets" / "a b.png").write_bytes(b"example")
    report = audit_document(
        _document(), 'Readable body.\n\n![Photo](../assets/a%20b.png "title")', tmp_path,
        markdown_path=tmp_path / "output" / "book.md",
    )
    assert report.publishable


def test_audit_checks_table_cells_in_delivered_markdown(tmp_path) -> None:
    document = _document()
    document.tables.append(NativeTable(
        id="table1", page=1, bbox=BBox(x0=0, y0=30, x1=80, y1=80),
        bbox_norm=BBox(x0=0, y0=0.3, x1=0.8, y1=0.8),
        rows=[["Feature", "Value"], ["Small-font content", "Preserved"]],
        source_column_count=2, role="table", include_in_markdown=True,
        anchor_before_block_id="p0001-b0001",
    ))
    missing = audit_document(document, "Readable body.\n", tmp_path)
    assert not missing.publishable
    assert any(issue.code == "missing_rendered_table_cell" for issue in missing.issues)
    complete = audit_document(document,
        "Readable body.\n\n| Feature | Value |\n| --- | --- |\n"
        "| Small-font content | Preserved |\n", tmp_path)
    assert complete.publishable


def test_audit_blocks_modified_asset_bytes(tmp_path) -> None:
    import hashlib

    document = _document()
    document.images.append(EmbeddedImage(
        id="image1", page=1, object_name="Im0", asset_path="image.png",
        sha256=hashlib.sha256(b"original").hexdigest(),
        bbox=BBox(x0=0, y0=0, x1=20, y1=20),
        bbox_norm=BBox(x0=0, y0=0, x1=0.2, y1=0.2),
        pixel_width=20, pixel_height=20, format="png", role="decorative",
    ))
    (tmp_path / "image.png").write_bytes(b"changed")
    report = audit_document(document, "Readable body.\n", tmp_path)
    assert not report.publishable
    assert any(issue.code == "embedded_image_hash_mismatch" for issue in report.issues)


def test_audit_accepts_alphabetic_list_labels_replaced_by_markdown_bullets(tmp_path) -> None:
    document = _document()
    block = document.pages[0].blocks[0]
    block.type = BlockType.LIST_ITEM
    block.text = "b. Preserve this item."
    block.lines[0].text = block.text
    assert audit_document(document, "- Preserve this item.\n", tmp_path).publishable
    assert not audit_document(document, "- Incomplete item.\n", tmp_path).publishable
