"""Independently compare an EPUB with its Markdown source.

The audit is deliberately independent of the packager: it re-reads the EPUB
(XHTML, OPF, nav) and checks the chapter prose, footnote references and their
back links, image placements and alt texts, and navigation targets against the
Markdown the book claims to render.  A packager bug cannot pass this audit
without also corrupting the Markdown.  Unreferenced note definitions are
preserved inline as visible markers instead of being dropped silently.
"""

from __future__ import annotations

from collections.abc import Callable
from hashlib import sha256
from pathlib import Path
import posixpath
import re
from urllib.parse import unquote, urlsplit
from xml.etree import ElementTree as ET
from zipfile import ZipFile

from .markdown_content import image_references, normalized_markdown_text

XHTML = "{http://www.w3.org/1999/xhtml}"
OPF = "{http://www.idpf.org/2007/opf}"
OPS_TYPE = "{http://www.idpf.org/2007/ops}type"
NOTE_DEF = re.compile(r"^\[\^([^\]\n]+)\]:[ \t]*(.*)$", re.M)
NOTE_REF = re.compile(r"\[\^([^\]\n]+)\]")


def _tight(text: str) -> str:
    return "".join(text.split())


def _prose(element: ET.Element, omit: Callable[[ET.Element], bool]) -> str:
    pieces = [element.text or ""]
    for child in element:
        if not omit(child):
            pieces.append(_prose(child, omit))
        pieces.append(child.tail or "")
    return "".join(pieces)


def _target(path: str, href: str) -> tuple[str, str]:
    link = urlsplit(href)
    if link.scheme or link.netloc:
        raise ValueError(f"Expected an internal link: {href}")
    return (
        posixpath.normpath(posixpath.join(posixpath.dirname(path), unquote(link.path))) if link.path else path,
        unquote(link.fragment),
    )


def preserve_unlinked_notes(markdown: str) -> tuple[str, list[str]]:
    """Convert note definitions nothing references into visible inline markers.

    Returns the converted Markdown and the labels of the converted notes.  A
    definition whose label is referenced somewhere in the body is kept as a
    proper definition; the rest become ``（第 N 页，注 M）`` style markers so no
    source note is lost in the reflowable output.
    """
    labels = set(NOTE_REF.findall(NOTE_DEF.sub("", markdown)))
    converted: list[str] = []

    def note(match: re.Match[str]) -> str:
        label, body = match.groups()
        if label in labels:
            return match[0]
        converted.append(label)
        page = re.fullmatch(r"p(\d+)-(\d+)", label)
        marker = f"（第 {int(page[1])} 页，注 {int(page[2])}）" if page else f"（注 {label}）"
        return marker + body

    return NOTE_DEF.sub(note, markdown), converted


def verify(markdown: str, epub: Path) -> dict:
    """Audit ``epub`` against ``markdown``; raise on any mismatch.

    Returns a report with byte size, SHA-256, image, navigation and footnote
    counts.  The caller gates publication on the absence of exceptions plus
    its own expectations for the reported counts.
    """
    chapters = [chunk for chunk in re.split(r"(?=^#\s+\S)", markdown, flags=re.M) if chunk.strip()]
    images = image_references(markdown)
    notes = 0
    refs_count = 0
    packaged_images: list[tuple[str, str]] = []
    chapter_reports: list[dict] = []
    toc_links: list[ET.Element] = []
    with ZipFile(epub) as archive:
        assert archive.testzip() is None
        container = ET.fromstring(archive.read("META-INF/container.xml"))
        opf_path = next(node.get("full-path") for node in container.iter() if node.get("full-path"))
        opf = ET.fromstring(archive.read(opf_path))
        manifest = {item.attrib["id"]: item for item in opf.findall(f"{OPF}manifest/{OPF}item")}
        spine = [manifest[item.attrib["idref"]] for item in opf.findall(f"{OPF}spine/{OPF}itemref")]
        # Chapter files are the spine documents whose resolved path ends in
        # chN.xhtml, regardless of where the OPF itself lives in the archive.
        resolved_spine = [_target(opf_path, item.attrib["href"])[0] for item in spine]
        chapter_paths = [path for path in resolved_spine if re.search(r"/ch\d+\.xhtml$", path)]
        assert len(chapters) == len(chapter_paths), (len(chapters), len(chapter_paths))
        roots = {path: ET.fromstring(archive.read(path)) for path in chapter_paths}
        ids = {path: {item.get("id"): item for item in root.iter() if item.get("id")} for path, root in roots.items()}

        for raw, path in zip(chapters, chapter_paths, strict=True):
            document = roots[path]
            body = document.find(XHTML + "body")
            assert body is not None
            assert not list(document.iter(XHTML + "script"))
            main = NOTE_DEF.sub("", raw)
            definitions = dict(NOTE_DEF.findall(raw))
            ref_labels = NOTE_REF.findall(main)
            assert set(definitions) == set(ref_labels), (path, "reference definitions mismatch")
            refs = [item for item in body.iter() if item.get(OPS_TYPE) == "noteref"]
            assert len(refs) == len(ref_labels), (path, "reference count mismatch")
            for label, ref in zip(ref_labels, refs, strict=True):
                dest, ident = _target(path, ref.attrib["href"])
                note_element = ids[dest][ident]
                assert note_element.get(OPS_TYPE) == "footnote", (path, label)

                def back_link(item: ET.Element) -> bool:
                    return "footnote-back" in item.get("class", "").split()

                assert any(
                    _target(dest, anchor.attrib["href"]) == (path, ref.attrib["id"])
                    for anchor in note_element.iter(XHTML + "a")
                    if back_link(anchor)
                ), (path, label, "missing backlink")
                assert _tight(_prose(note_element, back_link)) == normalized_markdown_text(
                    definitions[label]
                ), (path, label, "note text mismatch")
            expected = normalized_markdown_text(NOTE_REF.sub("", main))
            actual = _tight(
                _prose(body, lambda node: node.get(OPS_TYPE) in {"noteref", "footnote", "footnotes"})
            )
            if expected != actual:
                index = next(
                    (i for i, (x, y) in enumerate(zip(expected, actual)) if x != y),
                    min(len(expected), len(actual)),
                )
                raise ValueError(f"{path} text differs at {index}. Source {expected[index:index+35]!r}, EPUB {actual[index:index+35]!r}")
            for image in body.iter(XHTML + "img"):
                resource, _fragment = _target(path, image.attrib["src"])
                packaged_images.append((image.get("alt", ""), resource))
            notes += len(definitions)
            refs_count += len(ref_labels)
            chapter_reports.append(
                {"file": path, "characters_checked": len(expected), "notes": len(definitions)}
            )

        assert [reference.alt for reference in images] == [alt for alt, _name in packaged_images]
        assert all(name in archive.namelist() for _alt, name in packaged_images)
        nav_path = _target(
            opf_path, next(item.attrib["href"] for item in manifest.values() if item.get("properties") == "nav")
        )[0]
        nav = ET.fromstring(archive.read(nav_path))
        toc = next(node for node in nav.iter(XHTML + "nav") if node.get(OPS_TYPE) == "toc")
        toc_links = list(toc.iter(XHTML + "a"))
        for anchor in toc_links:
            file_name, fragment = _target(nav_path, anchor.attrib["href"])
            if file_name not in ids:
                other = ET.fromstring(archive.read(file_name))
                ids[file_name] = {node.get("id"): node for node in other.iter() if node.get("id")}
            assert not fragment or fragment in ids[file_name], anchor.attrib["href"]

    return {
        "status": "passed",
        "epub_bytes": epub.stat().st_size,
        "sha256": sha256(epub.read_bytes()).hexdigest(),
        "chapters_and_parts": chapter_reports,
        "images": len(packaged_images),
        "navigation_links": len(toc_links),
        "footnotes": notes,
        "footnote_roundtrips": refs_count,
    }
