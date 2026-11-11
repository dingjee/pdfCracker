"""Synthetic end-to-end test for pdfcracker.epub_build.

Builds a small fake book (3 chapters, sections, Pandoc footnotes, RGB PNG
images, colored cover) and asserts every verification gate recorded in the
JSON build log. Requires the pinned converter stack (.local/epub-tools with
pandoc, EPUBCheck and a JRE, gitignored); skipped when it is absent so the
test suite stays green on machines without the tools.
"""

from __future__ import annotations

import io
import json
import re
import zipfile
from pathlib import Path

import pytest
from PIL import Image

from pdfcracker.epub_build import DEFAULT_TOOLS, EpubTools, build_epub

pytestmark = pytest.mark.skipif(
    not (DEFAULT_TOOLS / "pandoc-3.13-arm64" / "bin" / "pandoc").exists()
    or not list(DEFAULT_TOOLS.glob("epubcheck-*/epubcheck.jar"))
    or not list(DEFAULT_TOOLS.glob("jdk*/Contents/Home/bin/java")),
    reason="pinned pandoc/epubcheck/jre stack not installed")


def _png(path: Path, size: tuple[int, int],
         color: tuple[int, int, int]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    Image.new("RGB", size, color).save(path, "PNG")


@pytest.fixture()
def synthetic_book(tmp_path: Path) -> dict:
    img_dir = tmp_path / "images"
    img_dir.mkdir()
    _png(img_dir / "a.png", (37, 41), (200, 30, 30))
    _png(img_dir / "b.png", (64, 48), (30, 200, 30))
    _png(img_dir / "c.png", (11, 13), (30, 30, 200))
    _png(tmp_path / "cover.png", (120, 160), (250, 200, 40))
    (tmp_path / "book.md").write_text(
        "# 第一章 合成\n\n"
        "正文甲[^f1]与图：\n\n"
        "![图一](images/a.png)\n\n"
        "## 小节一\n\n"
        "正文乙[^f2]结束。\n\n"
        "# 第二章 合成\n\n"
        "![图二](images/b.png)\n\n"
        "第二章正文[^f3]。\n\n"
        "## 小节二\n\n"
        "![图三](images/c.png)\n\n"
        "# 第三章 合成\n\n"
        "无脚注章。\n\n"
        "[^f1]: 注一。\n"
        "[^f2]: 注二第一段。\n\n"
        "    注二第二段。\n"
        "[^f3]: 注三。\n",
        encoding="utf-8")
    (tmp_path / "metadata.yaml").write_text(
        'title: "合成书"\nauthor:\n  - "作者一"\nlang: zh-Hans\n'
        'description: "合成测试。"\n', encoding="utf-8")
    (tmp_path / "reader.css").write_text(
        "body { line-height: 1.55; }\n", encoding="utf-8")
    return {"dir": tmp_path, "md": tmp_path / "book.md",
            "metadata": tmp_path / "metadata.yaml",
            "css": tmp_path / "reader.css",
            "cover": tmp_path / "cover.png"}


def _build(book: dict, out_name: str, cover: bool = True) -> dict:
    out = book["dir"] / out_name
    code = build_epub(
        markdown=book["md"], output=out, metadata=book["metadata"],
        css=book["css"], tools=EpubTools(DEFAULT_TOOLS),
        cover=book["cover"] if cover else None,
        title="电影史：理论与实践", description="全书。",
        log_path=out.with_suffix(".log.json"))
    log = json.loads(out.with_suffix(".log.json").read_text(encoding="utf-8"))
    return {"code": code, "out": out, "log": log}


def test_build_ok_and_all_gates_green(synthetic_book: dict):
    r = _build(synthetic_book, "book.epub")
    assert r["code"] == 0, r["log"]["failures"]
    log = r["log"]
    assert log["ok"] is True and log["failures"] == []
    assert log["epubcheck"]["errors"] == 0
    assert log["epubcheck"]["warnings"] == 0
    assert log["zip"]["testzip_ok"] is True
    assert log["zip"]["mimetype_first_stored"] is True
    assert log["manifest"]["missing"] == []
    assert log["manifest"]["unmanifested_files"] == []
    assert log["image_links"]["missing"] == []
    assert log["nav"]["missing"] == []
    assert log["chapters"]["markdown_h1"] == 3
    assert log["chapters"]["chapter_xhtml"] == 3
    fn = log["footnotes"]
    assert fn["noterefs"] == 3
    assert fn["backlinks_inserted"] == 3
    assert fn["unresolved_noterefs"] == []
    assert fn["missing_backlinks"] == []
    assert log["footnote_links"]["unresolved"] == []
    assert log["footnote_links"]["missing_backlinks"] == []


def test_images_grayscale_q90_original_size_cover_color(synthetic_book: dict):
    r = _build(synthetic_book, "book.epub")
    with zipfile.ZipFile(r["out"]) as z:
        for entry in r["log"]["images"]:
            data = z.read(f"EPUB/{entry['epub_path']}")
            with Image.open(io.BytesIO(data)) as im:
                assert im.mode == "L"
                assert list(im.size) == entry["source_size"]
        with Image.open(io.BytesIO(
                z.read(f"EPUB/{r['log']['cover']['epub_path']}"))) as im:
            assert im.mode == "RGB"


def test_footnote_backlinks_present_in_xhtml(synthetic_book: dict):
    r = _build(synthetic_book, "book.epub")
    with zipfile.ZipFile(r["out"]) as z:
        names = [n for n in z.namelist() if n.endswith(".xhtml")
                 and not n.endswith("nav.xhtml")]
        joined = "".join(z.read(n).decode("utf-8") for n in names)
    assert 'role="doc-backlink"' in joined
    assert re.search(
        r'<a href="#fnref\d+" class="footnote-back" role="doc-backlink">',
        joined)


def test_metadata_title_override(synthetic_book: dict):
    r = _build(synthetic_book, "book.epub")
    with zipfile.ZipFile(r["out"]) as z:
        opf = next(n for n in z.namelist() if n.endswith(".opf"))
        text = z.read(opf).decode("utf-8")
    assert "电影史：理论与实践" in text


def test_refuses_overwrite(synthetic_book: dict):
    assert _build(synthetic_book, "book.epub")["code"] == 0
    r2 = _build(synthetic_book, "book.epub")
    assert r2["code"] == 1
    assert any("overwrite" in f["detail"] for f in r2["log"]["failures"])


def test_no_cover_builds(synthetic_book: dict):
    r = _build(synthetic_book, "nocover.epub", cover=False)
    assert r["code"] == 0, r["log"]["failures"]
