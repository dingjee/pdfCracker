import base64
from html.parser import HTMLParser
from pathlib import Path
import zipfile

from PIL import Image
import pytest

from pdfcracker.delivery import package_markdown


def _book(root: Path) -> Path:
    (root / "output").mkdir(parents=True)
    (root / "assets").mkdir()
    Image.new("RGB", (12, 8), "navy").save(root / "assets" / "a b.png")
    book = root / "output" / "book.md"
    book.write_text(
        '# Book\n\n<strong><em><u>Body</u></em></strong>\n\n'
        '![photo][object]\n\n[object]: <../assets/a b.png> "Caption"\n\n'
        '```md\n![example](absent.png)\n```\n\n'
        '| Feature | Value |\n| --- | --- |\n| Cell | Kept |\n', encoding="utf-8",
    )
    return book


def test_portable_archive_preserves_relative_structure_and_original_bytes(tmp_path) -> None:
    root = tmp_path / "source"
    book = _book(root)
    original = book.read_bytes()
    result = package_markdown([book], root, tmp_path / "delivery", self_contained=True)
    with zipfile.ZipFile(result.archive_path) as archive:
        assert archive.testzip() is None
        assert archive.read("output/book.md") == original
        assert archive.read("assets/a b.png") == (root / "assets" / "a b.png").read_bytes()
        html = archive.read("output/book.html").decode()
        assert 'src="../assets/a%20b.png"' in html
        assert '<strong><em><u>Body</u></em></strong>' in html
        assert "<table>" in html
    assert book.read_bytes() == original
    assert result.image_count == 1
    assert result.reference_count == 1


def test_standalone_html_contains_identical_image_bytes(tmp_path) -> None:
    book = _book(tmp_path / "source")
    result = package_markdown([book], book.parents[1], tmp_path / "delivery", self_contained=True)

    class Images(HTMLParser):
        def __init__(self):
            super().__init__()
            self.sources = []
        def handle_starttag(self, tag, attrs):
            if tag == "img":
                self.sources.append(dict(attrs)["src"])

    parser = Images()
    parser.feed(Path(result.standalone_path).read_text(encoding="utf-8"))
    assert len(parser.sources) == 1
    assert parser.sources[0].startswith("data:image/png;base64,")
    assert base64.b64decode(parser.sources[0].split(",", 1)[1]) == (
        book.parents[1] / "assets" / "a b.png"
    ).read_bytes()


@pytest.mark.parametrize("reference", ["", "missing.png", "../../outside.png"])
def test_invalid_reference_aborts_without_partial_delivery(tmp_path, reference) -> None:
    book = _book(tmp_path / "source")
    book.write_text(f"![broken]({reference})", encoding="utf-8")
    with pytest.raises(ValueError):
        package_markdown([book], book.parents[1], tmp_path / "delivery")
    assert not (tmp_path / "delivery").exists()


def test_package_never_overwrites_existing_delivery(tmp_path) -> None:
    book = _book(tmp_path / "source")
    output = tmp_path / "delivery"
    output.mkdir()
    with pytest.raises(ValueError, match="already exists"):
        package_markdown([book], book.parents[1], output)


def test_html_escapes_active_raw_html(tmp_path) -> None:
    book = _book(tmp_path / "source")
    book.write_text('<script>alert("test")</script>\n\n![a](../assets/a%20b.png)', encoding="utf-8")
    result = package_markdown([book], book.parents[1], tmp_path / "delivery")
    with zipfile.ZipFile(result.archive_path) as archive:
        html = archive.read("output/book.html").decode()
    assert "<script>" not in html
    assert "&lt;script&gt;" in html
