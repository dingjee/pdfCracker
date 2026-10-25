from pathlib import Path

import pytest

from pdfcracker.markdown_content import image_references, normalized_markdown_text, resolve_image


def test_image_parser_includes_empty_and_reference_links_but_ignores_code() -> None:
    markdown = (
        '![empty]()\n![a \\] label](<images/a b.png> "title")\n'
        '![reference][photo]\n\n[photo]: images/c.png\n\n'
        '```markdown\n![example](missing.png)\n```\n'
    )
    assert [ref.source for ref in image_references(markdown)] == [
        "", "images/a%20b.png", "images/c.png"
    ]


def test_text_normalization_preserves_nested_semantics_and_table_text() -> None:
    text = '## 标题\n\n<strong><em><u>正文</u></em></strong>\n\n| 列 |\n| --- |\n| A&#124;B |'
    assert normalized_markdown_text(text) == "标题正文列A|B"
    assert normalized_markdown_text('![正文](image.png)') == ""


def test_image_resolution_is_relative_to_markdown_not_artifact_root(tmp_path) -> None:
    markdown = tmp_path / "output" / "book.md"
    target = tmp_path / "assets" / "a b.png"
    target.parent.mkdir()
    target.write_bytes(b"image")
    assert resolve_image(markdown, tmp_path, "../assets/a%20b.png#detail") == target


@pytest.mark.parametrize("source", ["", "../../outside.png", "https://example.com/a.png", "/outside.png"])
def test_image_resolution_rejects_empty_external_and_escaping_paths(tmp_path, source) -> None:
    with pytest.raises(ValueError):
        resolve_image(tmp_path / "book.md", tmp_path, source)


def test_image_resolution_rejects_directory_target(tmp_path: Path) -> None:
    with pytest.raises(ValueError):
        resolve_image(tmp_path / "book.md", tmp_path, ".")
