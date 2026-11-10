"""Tests for rebuilding an EPUB navigation TOC from a printed table of contents."""

from pathlib import Path
import zipfile

import pytest

from pdfcracker.epub_nav import (
    build_toc_list,
    collect_heading_anchors,
    load_toc,
    match_toc_entries,
    normalize_title,
    rewrite_epub_nav,
)

CONTAINER = """<?xml version="1.0" encoding="utf-8"?>
<container xmlns="urn:oasis:names:tc:opendocument:xmlns:container">
<rootfiles><rootfile full-path="EPUB/content.opf" media-type="application/oebps-package+xml"/></rootfiles>
</container>
"""

OPF = """<?xml version="1.0" encoding="utf-8"?>
<package xmlns="http://www.idpf.org/2007/opf" version="3.0" unique-identifier="id">
<metadata xmlns:dc="http://purl.org/dc/elements/1.1/"><dc:title>试验</dc:title></metadata>
<manifest>
<item id="ch1" href="text/ch001.xhtml" media-type="application/xhtml+xml"/>
<item id="ch2" href="text/ch002.xhtml" media-type="application/xhtml+xml"/>
<item id="nav" href="nav.xhtml" media-type="application/xhtml+xml" properties="nav"/>
</manifest>
<spine><itemref idref="ch1"/><itemref idref="ch2"/></spine>
</package>
"""

CHAPTER_1 = """<?xml version="1.0" encoding="utf-8"?>
<html xmlns="http://www.w3.org/1999/xhtml" xmlns:epub="http://www.idpf.org/2007/ops">
<body>
<section id="第一章-试验" class="level1"><h1>第一章　试验</h1>
<p>正文。</p>
<section id="第一节" class="level2"><h2>1.1 第一节</h2><p>小节正文。</p></section>
<section id="第二节" class="level2"><h2>1.2 第二节</h2><p>小节正文。</p>
<section id="结语" class="level3"><h3>结语</h3><p>末段。</p></section>
</section>
</section>
</body>
</html>
"""

# a bare heading without a wrapping section id exercises the fallback
CHAPTER_2 = """<?xml version="1.0" encoding="utf-8"?>
<html xmlns="http://www.w3.org/1999/xhtml" xmlns:epub="http://www.idpf.org/2007/ops">
<body>
<h1 id="第二章-续篇">第二章　续篇</h1>
<section id="结论" class="level2"><h2>结论</h2><p>末段。</p></section>
</body>
</html>
"""

OLD_NAV = """<?xml version="1.0" encoding="utf-8"?>
<html xmlns="http://www.w3.org/1999/xhtml" xmlns:epub="http://www.idpf.org/2007/ops">
<head><title>试验</title></head>
<body>
<nav epub:type="toc" role="doc-toc" id="toc"><h1 id="toc-title">试验</h1>
<ol class="toc"><li><a href="text/ch001.xhtml#第一章-试验">第一章　试验</a></li></ol>
</nav>
</body>
</html>
"""

TOC = {
    "entries": [
        {"level": 1, "title": "第一章　试验"},
        {"level": 2, "title": "1.1 第一节"},
        {"level": 2, "title": "1.2 第二节"},
        {"level": 3, "title": "结语"},
        {"level": 1, "title": "第二章　续篇"},
        {"level": 2, "title": "结论", "match": "末段"},  # placeholder; fixed below
    ]
}
TOC["entries"][-1].pop("match")


def build_epub(path: Path) -> None:
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("mimetype", "application/epub+zip")
        archive.writestr("META-INF/container.xml", CONTAINER)
        archive.writestr("EPUB/content.opf", OPF)
        archive.writestr("EPUB/text/ch001.xhtml", CHAPTER_1)
        archive.writestr("EPUB/text/ch002.xhtml", CHAPTER_2)
        archive.writestr("EPUB/nav.xhtml", OLD_NAV)


def read_nav_toc(path: Path):
    from xml.etree import ElementTree as ET

    XHTML = "{http://www.w3.org/1999/xhtml}"
    OPS = "{http://www.idpf.org/2007/ops}"
    with zipfile.ZipFile(path) as z:
        root = ET.fromstring(z.read("EPUB/nav.xhtml"))
    toc = next(node for node in root.iter(XHTML + "nav") if node.get(OPS + "type") == "toc")
    rows = []

    def walk(ol, depth):
        for li in ol:
            anchor = li.find(XHTML + "a")
            rows.append((depth, "".join(anchor.itertext()), anchor.get("href")))
            for child in li:
                if child.tag == XHTML + "ol":
                    walk(child, depth + 1)

    for child in toc:
        if child.tag == XHTML + "ol":
            walk(child, 0)
    return rows


def test_normalize_title_ignores_width_spacing_and_punctuation() -> None:
    assert normalize_title("第一章　试验") == normalize_title("第一章 试验")
    assert normalize_title("1.1 第一节") == "11第一节"
    assert normalize_title("结语") == "结语"


def test_match_uses_order_cursor_for_repeated_titles() -> None:
    entries = load_toc(TOC)
    with zipfile.ZipFile(_fixture_epub()) as z:
        headings = collect_heading_anchors(z, ["EPUB/text/ch001.xhtml", "EPUB/text/ch002.xhtml"])
    matched = match_toc_entries(entries, headings)
    # 结语 lands in chapter 1, 结论 in chapter 2 despite both being two-character closers
    assert [(entry.title, hit.file, hit.anchor_id) for entry, hit in matched] == [
        ("第一章　试验", "EPUB/text/ch001.xhtml", "第一章-试验"),
        ("1.1 第一节", "EPUB/text/ch001.xhtml", "第一节"),
        ("1.2 第二节", "EPUB/text/ch001.xhtml", "第二节"),
        ("结语", "EPUB/text/ch001.xhtml", "结语"),
        ("第二章　续篇", "EPUB/text/ch002.xhtml", "第二章-续篇"),
        ("结论", "EPUB/text/ch002.xhtml", "结论"),
    ]


def _fixture_epub() -> str:
    import tempfile

    path = Path(tempfile.mkdtemp()) / "fixture.epub"
    if not path.exists():
        build_epub(path)
    return str(path)


def test_match_alias_resolves_toc_body_drift() -> None:
    entries = [load_toc({"entries": [{"level": 2, "title": "4.20 结论", "match": "4.20 结语"}]})[0]]
    from pdfcracker.epub_nav import HeadingAnchor

    anchors = [HeadingAnchor(0, "EPUB/text/ch010.xhtml", "结语", normalize_title("4.20 结语"))]
    matched = match_toc_entries(entries, anchors)
    assert matched[0][0].title == "4.20 结论"
    assert matched[0][1].anchor_id == "结语"


def test_unmatched_entry_raises_with_details() -> None:
    from pdfcracker.epub_nav import HeadingAnchor

    entries = load_toc({"entries": [{"level": 1, "title": "不存在的章"}]})
    with pytest.raises(ValueError, match="不存在的章"):
        match_toc_entries(entries, [HeadingAnchor(0, "f.xhtml", "x", "别的")])


def test_rewrite_epub_nav_replaces_list_and_links() -> None:
    import tempfile

    epub = Path(tempfile.mkdtemp()) / "book.epub"
    build_epub(epub)
    log: dict = {}
    count = rewrite_epub_nav(epub, TOC, log)
    assert count == 6
    assert log["printed_toc"]["entries"] == 6
    rows = read_nav_toc(epub)
    assert rows == [
        (0, "第一章　试验", "text/ch001.xhtml#第一章-试验"),
        (1, "1.1 第一节", "text/ch001.xhtml#第一节"),
        (1, "1.2 第二节", "text/ch001.xhtml#第二节"),
        (2, "结语", "text/ch001.xhtml#结语"),
        (0, "第二章　续篇", "text/ch002.xhtml#第二章-续篇"),
        (1, "结论", "text/ch002.xhtml#结论"),
    ]
    # the rest of the archive is untouched
    with zipfile.ZipFile(epub) as z:
        assert z.read("EPUB/text/ch001.xhtml").decode("utf-8") == CHAPTER_1


def test_build_toc_list_rejects_ill_nesting() -> None:
    from pdfcracker.epub_nav import HeadingAnchor

    entries = load_toc({"entries": [
        {"level": 1, "title": "甲"},
        {"level": 3, "title": "丙"},  # skips level 2: nests under 甲 fine
    ]})
    anchors = [HeadingAnchor(0, "t/ch001.xhtml", "a", normalize_title("甲")),
               HeadingAnchor(1, "t/ch001.xhtml", "b", normalize_title("丙"))]
    matched = match_toc_entries(entries, anchors)
    root = build_toc_list(matched, "EPUB")
    assert root.tag == "{http://www.w3.org/1999/xhtml}ol"
