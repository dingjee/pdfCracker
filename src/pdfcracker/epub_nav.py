"""Rebuild an EPUB's navigation TOC to mirror a printed table of contents.

Converters generate the EPUB navigation from every heading up to a depth,
which pulls in note sections, inset-box labels, running labels and sentence
fragments that a book's printed TOC never lists.  The reliable fix is to
transcribe the printed TOC (levels and titles in reading order) and rebuild
the navigation list from it, linking each entry to its body heading with an
order cursor so repeated titles resolve to their own chapter.  Entries that
cannot be matched abort the caller — a missing link is a build failure, not a
silent drop.
"""

from __future__ import annotations

from dataclasses import dataclass
import posixpath
import re
from typing import Any
from urllib.parse import unquote, urlsplit
from xml.etree import ElementTree as ET
from zipfile import ZipFile, ZIP_DEFLATED

XHTML_NS = "http://www.w3.org/1999/xhtml"
OPS_NS = "http://www.idpf.org/2007/ops"
OPF_NS = "http://www.idpf.org/2007/opf"
CHAPTER_HREF_RE = re.compile(r"/ch\d+\.xhtml$")
ENUM_PREFIX_RE = re.compile(r"^\s*(?:第[一二三四五六七八九十百零]+[章部分篇]\s*|\d+\s*[.、]\s*)+")
_TITLE_PUNCT_RE = re.compile(r"[\s“”\"'‘’()（）:：,，、.·\-—?？!！;；]")


def outline_title_variants(toc_data: dict[str, Any]) -> list[str]:
    """Normalized title variants of every printed-TOC entry.

    Each entry contributes its title, its ``match`` alias and the enumeration
    prefix removed (``第一章 标题`` / ``1. 标题``), so a body heading matches
    the printed entry even when the prefix is missing or differs.  Used both
    for anchor matching and for deciding which body headings belong to the
    printed outline.
    """
    variants: set[str] = set()
    for entry in load_toc(toc_data):
        for base in {entry.title, entry.match or entry.title}:
            for candidate in {base, ENUM_PREFIX_RE.sub("", base).strip()}:
                value = normalize_title(candidate)
                if value:
                    variants.add(value)
    return sorted(variants)


def _qn(namespace: str, tag: str) -> str:
    return f"{{{namespace}}}{tag}"


def normalize_title(text: str) -> str:
    """Normalize a title for TOC-to-heading matching.

    Width, whitespace and punctuation variants are ignored so the printed
    table of contents matches the body headings it points at; the order
    cursor (not the normalization) disambiguates repeated titles.
    """
    import unicodedata

    value = unicodedata.normalize("NFKC", text).replace("\u3000", " ")
    return _TITLE_PUNCT_RE.sub("", value)


def _target(path: str, href: str) -> str:
    link = urlsplit(href)
    if link.scheme or link.netloc:
        raise ValueError(f"Expected an internal link: {href}")
    return posixpath.normpath(posixpath.join(posixpath.dirname(path), unquote(link.path)))


@dataclass(frozen=True)
class HeadingAnchor:
    """One linkable body heading in reading order."""

    sequence: int
    file: str
    anchor_id: str
    norm: str


@dataclass(frozen=True)
class TocEntry:
    """One printed table-of-contents entry."""

    level: int
    title: str
    match: str | None = None


def load_toc(data: dict[str, Any]) -> list[TocEntry]:
    """Canonical entries from ``{"front_matter": [...], "entries": [...]}``."""
    rows = list(data.get("front_matter", [])) + list(data["entries"])
    return [TocEntry(int(row["level"]), str(row["title"]), row.get("match")) for row in rows]


def collect_heading_anchors(archive: ZipFile, chapter_files: list[str]) -> list[HeadingAnchor]:
    """Every h1/h2/h3 anchor in the chapter files, in spine order.

    Pandoc nests each heading in ``<section id="..." class="levelN">``, so the
    anchor id comes from the nearest preceding section element when the
    heading itself carries none.
    """
    headings: list[HeadingAnchor] = []
    heading_tags = {_qn(XHTML_NS, f"h{n}") for n in (1, 2, 3)}
    for name in chapter_files:
        root = ET.fromstring(archive.read(name))
        pending_section_id: str | None = None
        for element in root.iter():
            if element.tag == _qn(XHTML_NS, "section") and element.get("id"):
                pending_section_id = element.get("id")
            elif element.tag in heading_tags:
                anchor_id = element.get("id") or pending_section_id
                pending_section_id = None
                if anchor_id:
                    headings.append(HeadingAnchor(
                        len(headings), name, anchor_id,
                        normalize_title("".join(element.itertext())),
                    ))
    return headings


def match_toc_entries(entries: list[TocEntry], headings: list[HeadingAnchor]) -> list[tuple[TocEntry, HeadingAnchor]]:
    """Link every entry to the next matching heading (order cursor).

    ``entry.match`` overrides the title used for anchor matching (the printed
    TOC and the body heading occasionally drift, e.g. 结论 vs 结语); after
    that, the enumeration prefix (第一章 / 1. / 1.1) is stripped as a
    fallback.  Raises ValueError listing every entry that found no heading.
    """
    matched: list[tuple[TocEntry, HeadingAnchor]] = []
    cursor = 0
    misses: list[str] = []
    for entry in entries:
        variants = [entry.match or entry.title, entry.title]
        for base in list(variants):
            stripped = ENUM_PREFIX_RE.sub("", base).strip()
            if stripped and stripped != base:
                variants.append(stripped)
        hit = None
        for variant in variants:
            norm = normalize_title(variant)
            if not norm:
                continue
            hit = next((h for h in headings[cursor:] if h.norm == norm), None)
            if hit is not None:
                break
        if hit is None:
            misses.append(entry.title)
            continue
        cursor = hit.sequence + 1
        matched.append((entry, hit))
    if misses:
        raise ValueError(
            f"printed TOC entries without a matching heading: {misses}; "
            f"collected {len(headings)} headings"
        )
    return matched


def build_toc_list(matched: list[tuple[TocEntry, HeadingAnchor]], nav_dir: str) -> ET.Element:
    """The nested <ol> for the navigation document.

    Recursive descent: items at one level become <li>; deeper items nest under
    their preceding item, so leaf items carry no empty <ol>.
    """
    flat = [
        (entry.level, entry.title, f"{posixpath.relpath(hit.file, nav_dir)}#{hit.anchor_id}")
        for entry, hit in matched
    ]

    def descend(level: int, index: int):
        ol = ET.Element(_qn(XHTML_NS, "ol"), {"class": "toc"})
        i = index
        while i < len(flat):
            lvl, title, href = flat[i]
            if lvl < level:
                break
            item = ET.SubElement(ol, _qn(XHTML_NS, "li"))
            anchor = ET.SubElement(item, _qn(XHTML_NS, "a"))
            anchor.set("href", href)
            anchor.text = title
            i += 1
            if i < len(flat) and flat[i][0] > lvl:
                child, i = descend(flat[i][0], i)
                item.append(child)
        return ol, i

    root_ol, end = descend(flat[0][0], 0)
    if end != len(flat):
        raise ValueError(f"printed TOC levels do not nest cleanly near: {flat[end]}")
    return root_ol


def rewrite_epub_nav(epub_path, toc_data: dict[str, Any], log: dict | None = None) -> int:
    """Replace the navigation TOC of the EPUB at ``epub_path`` in place.

    ``toc_data`` is the canonical printed TOC (see ``load_toc``).  Returns
    the number of linked entries.  Raises ValueError when any entry cannot be
    matched.  With ``log``, writes the matched link list under
    ``log["printed_toc"]``.
    """
    from pathlib import Path

    ET.register_namespace("", XHTML_NS)
    ET.register_namespace("epub", OPS_NS)
    entries = load_toc(toc_data)
    path = Path(epub_path)
    with ZipFile(path) as z:
        container = ET.fromstring(z.read("META-INF/container.xml"))
        opf_name = next(node.get("full-path") for node in container.iter() if node.get("full-path"))
        opf = ET.fromstring(z.read(opf_name))
        manifest = {item.get("id"): item
                    for item in opf.findall(f"{_qn(OPF_NS, 'manifest')}/{_qn(OPF_NS, 'item')}")}
        spine_ids = [ref.get("idref") for ref in opf.findall(f"{_qn(OPF_NS, 'spine')}/{_qn(OPF_NS, 'itemref')}")]
        nav_item = next((item for item in manifest.values()
                         if "nav" in (item.get("properties") or "")), None)
        if nav_item is None:
            raise ValueError("no nav document in the OPF manifest")
        nav_name = _target(opf_name, nav_item.get("href"))
        chapter_files = [
            _target(opf_name, manifest[item_id].get("href"))
            for item_id in spine_ids
            if item_id in manifest and CHAPTER_HREF_RE.search(_target(opf_name, manifest[item_id].get("href")))
        ]
        headings = collect_heading_anchors(z, chapter_files)
        nav_root = ET.fromstring(z.read(nav_name))

    matched = match_toc_entries(entries, headings)
    root_ol = build_toc_list(matched, posixpath.dirname(nav_name))

    toc_nav = next(node for node in nav_root.iter(_qn(XHTML_NS, "nav"))
                   if node.get(_qn(OPS_NS, "type")) == "toc")
    for child in list(toc_nav):
        if child.tag == _qn(XHTML_NS, "ol"):
            toc_nav.remove(child)
    toc_nav.append(root_ol)

    payload = ET.tostring(nav_root, encoding="utf-8", xml_declaration=True)
    tmp = path.with_suffix(".navtmp")
    with ZipFile(path) as zin, ZipFile(tmp, "w", ZIP_DEFLATED) as zout:
        for item in zin.infolist():
            zout.writestr(item, payload if item.filename == nav_name else zin.read(item.filename))
    tmp.replace(path)

    if log is not None:
        log["printed_toc"] = {
            "entries": len(matched),
            "links": [f"{hit.file}#{hit.anchor_id}" for _entry, hit in matched],
        }
    return len(matched)
