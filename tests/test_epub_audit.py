"""Tests for the independent EPUB-versus-Markdown audit."""

from pathlib import Path
import zipfile

import pytest

from pdfcracker.epub_audit import preserve_unlinked_notes, verify

MARKDOWN = """# 第一章 试验

正文第一段。

带脚注的句子[^n1]。

![图 1-1 测试图](images/a.jpg)

[^n1]: 脚注内容。
"""

CHAPTER = """<?xml version="1.0" encoding="utf-8"?>
<html xmlns="http://www.w3.org/1999/xhtml" xmlns:epub="http://www.idpf.org/2007/ops">
<body>
<h1>第一章 试验</h1>
<p>正文第一段。</p>
<p>带脚注的句子<a id="ref-1" epub:type="noteref" href="#note-1">1</a>。</p>
<p><img src="media/a.jpg" alt="图 1-1 测试图"/></p>
<aside epub:type="footnote" id="note-1">
<p><a class="footnote-back" href="#ref-1">返回</a>脚注内容。</p>
</aside>
</body>
</html>
"""

NAV = """<?xml version="1.0" encoding="utf-8"?>
<html xmlns="http://www.w3.org/1999/xhtml" xmlns:epub="http://www.idpf.org/2007/ops">
<body><nav epub:type="toc"><ol><li><a href="ch001.xhtml">第一章 试验</a></li></ol></nav></body>
</html>
"""

CONTAINER = """<?xml version="1.0" encoding="utf-8"?>
<container xmlns="urn:oasis:names:tc:opendocument:xmlns:container">
<rootfiles><rootfile full-path="OPF/opf.opf" media-type="application/oebps-package+xml"/></rootfiles>
</container>
"""

OPF = """<?xml version="1.0" encoding="utf-8"?>
<package xmlns="http://www.idpf.org/2007/opf" version="3.0" unique-identifier="id">
<metadata xmlns:dc="http://purl.org/dc/elements/1.1/"><dc:title>试验</dc:title></metadata>
<manifest>
<item id="ch1" href="ch001.xhtml" media-type="application/xhtml+xml"/>
<item id="nav" href="nav.xhtml" media-type="application/xhtml+xml" properties="nav"/>
</manifest>
<spine><itemref idref="ch1"/></spine>
</package>
"""


def build_epub(path: Path, chapter: str = CHAPTER) -> None:
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("META-INF/container.xml", CONTAINER)
        archive.writestr("OPF/opf.opf", OPF)
        archive.writestr("OPF/ch001.xhtml", chapter)
        archive.writestr("OPF/nav.xhtml", NAV)
        archive.writestr("OPF/media/a.jpg", b"jpeg-bytes")


def test_verify_passes_and_counts_a_minimal_epub(tmp_path: Path) -> None:
    epub = tmp_path / "book.epub"
    build_epub(epub)
    report = verify(MARKDOWN, epub)
    assert report["status"] == "passed"
    assert report["images"] == 1
    assert report["footnotes"] == 1
    assert report["footnote_roundtrips"] == 1
    assert report["navigation_links"] == 1
    assert report["chapters_and_parts"][0]["characters_checked"] > 0


def test_verify_detects_a_broken_footnote_backlink(tmp_path: Path) -> None:
    epub = tmp_path / "book.epub"
    build_epub(epub, CHAPTER.replace(' class="footnote-back"', ""))
    with pytest.raises(AssertionError, match="backlink"):
        verify(MARKDOWN, epub)


def test_verify_detects_altered_chapter_text(tmp_path: Path) -> None:
    epub = tmp_path / "book.epub"
    build_epub(epub, CHAPTER.replace("正文第一段。", "正文被改写。"))
    with pytest.raises(ValueError, match="text differs"):
        verify(MARKDOWN, epub)


def test_unlinked_note_definitions_become_visible_markers() -> None:
    markdown = "正文[^a]。\n\n[^a]: 被引用的注。\n\n[^p0004-2]: 无人引用的注。"
    converted, labels = preserve_unlinked_notes(markdown)
    assert labels == ["p0004-2"]
    assert "（第 4 页，注 2）无人引用的注。" in converted
    assert "[^a]: 被引用的注。" in converted
    assert "[^p0004-2]:" not in converted
