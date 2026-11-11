"""Full-book EPUB3 packaging with independent, evidence-backed verification.

Packages an assembled book Markdown into a reflowable EPUB3 with a pinned
converter stack, then verifies the artifact independently before an atomic
publish. Distilled from a 427-page scanned-book delivery; the run directory
keeps the frozen invocation as provenance.

Pipeline:

1. Images: every standalone Markdown image becomes grayscale JPEG Q90 at
   its original pixel size inside the EPUB; an optional cover keeps color.
   The converter renames media (``media/fileN.jpg``); the staged→epub
   mapping is confirmed by byte equality, so image bytes are never touched.
2. Converter (pinned EPUB3): chapter split at ``#``, TOC depth 2, reader
   CSS, metadata from a base YAML with title/description overrides.
3. Footnote back-links: the converter's ``<aside epub:type="footnote">``
   blocks carry no return link, and footnote ids renumber per chapter file
   — so ids resolve per file first (a noteref whose footnote lives in
   exactly one other file gets a cross-XHTML href); ambiguous or missing
   ids are reported, never guessed. Only ``<aside>`` blocks are rewritten.
4. Independent verification, all recorded in a JSON build log: EPUBCheck
   (0 errors AND 0 warnings), zip integrity (testzip; mimetype first and
   stored), OPF manifest vs. archive files, per-image link resolution and
   re-read pixel/mode check, footnote noteref↔aside↔back-link round-trip,
   chapter count vs. the Markdown's ``#`` headings, and every navigation
   target (including fragments).
5. Atomic publish: build in a temp dir beside the output, then
   ``os.replace``; an existing output is never overwritten.

Usage (library or CLI)::

    PYTHONPATH=src uv run --locked --no-sync python -m pdfcracker.epub_build \\
        --markdown book.md --output book.epub --metadata base.yaml \\
        --css reader.css --title "书名" [--cover cover.png] \\
        [--epub-tools .local/epub-tools]
"""

from __future__ import annotations

import argparse
import io
import json
import os
import posixpath
import re
import shutil
import subprocess
import sys
import tempfile
import zipfile
from pathlib import Path

from PIL import Image

__all__ = ["build_epub", "main"]

REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_TOOLS = REPO_ROOT / ".local" / "epub-tools"

IMG_LINE_RE = re.compile(r"^[ \t]*!\[([^\]]*)\]\(([^)\s]+)[^)]*\)[ \t]*$", re.M)
IMG_ANY_RE = re.compile(r"!\[[^\]]*\]\(([^)\s]+)[^)]*\)")
NOTEREF_TAG_RE = re.compile(r"<a\s+[^>]*role=\"doc-noteref\"[^>]*>", re.S)
ASIDE_RE = re.compile(r"<aside\b[^>]*\bid=\"([^\"]+)\"[^>]*>.*?</aside>", re.S)
BACKLINK_RE = re.compile(r"<a\s+[^>]*role=\"doc-backlink\"[^>]*>", re.S)


def log_fail(log: dict, stage: str, detail: str) -> int:
    log["ok"] = False
    log["failures"].append({"stage": stage, "detail": detail})
    return 1


def run(cmd: list[str], **kw) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, capture_output=True, text=True, **kw)


def tool_version(cmd: list[str], pattern: str) -> str:
    try:
        out = run(cmd).stdout + run(cmd).stderr
    except OSError:
        return "unavailable"
    m = re.search(pattern, out)
    return m.group(0) if m else "unknown"


class EpubTools:
    """Pinned converter stack: pandoc, EPUBCheck jar, and a JRE."""

    def __init__(self, root: Path, pandoc: Path | None = None,
                 java: Path | None = None, epubcheck: Path | None = None):
        self.pandoc = pandoc or next(iter(sorted(root.glob("pandoc-*/bin/pandoc"))), root / "pandoc" / "bin" / "pandoc")
        self.java = java or next(iter(sorted(root.glob("jdk*/Contents/Home/bin/java"))), root / "jdk" / "bin" / "java")
        self.epubcheck = epubcheck or next(iter(sorted(root.glob("epubcheck-*/epubcheck.jar"))), root / "epubcheck" / "epubcheck.jar")


def collect_images(md_text: str) -> tuple[dict[str, str], list[str]]:
    """Map standalone image targets to staged JPEG names (first-appearance
    order, which matches the converter's media/fileN.jpg numbering)."""
    mapping: dict[str, str] = {}
    for m in IMG_LINE_RE.finditer(md_text):
        mapping.setdefault(m.group(2), "")
    inline = [t for t in IMG_ANY_RE.findall(md_text) if t not in mapping]
    for target in mapping:
        stem = Path(target).stem
        if not stem:
            raise ValueError(f"image target has no stem: {target!r}")
        mapping[target] = f"images/{stem}.jpg"
    return mapping, inline


def convert_images(md_path: Path, mapping: dict[str, str], staging: Path,
                   cover: Path | None, log: dict) -> None:
    img_dir = staging / "images"
    img_dir.mkdir(parents=True, exist_ok=True)
    for src_rel, dst_rel in mapping.items():
        src = (md_path.parent / src_rel).resolve()
        if not src.is_file():
            raise FileNotFoundError(f"image referenced but missing: {src}")
        with Image.open(src) as im:
            original_size = im.size
            im.convert("L").save(staging / dst_rel, "JPEG", quality=90)
        with Image.open(staging / dst_rel) as check:
            log["images"].append({
                "source": src_rel, "epub_path": dst_rel,
                "source_size": list(original_size),
                "epub_size": list(check.size),
                "epub_mode": check.mode,
                "epub_bytes": (staging / dst_rel).stat().st_size,
            })
    if cover is not None:
        dst = staging / "images" / "cover.jpg"
        with Image.open(cover) as im:
            original_size = im.size
            im.convert("RGB").save(dst, "JPEG", quality=90)
        with Image.open(dst) as check:
            log["cover"] = {
                "source": str(cover), "epub_path": "images/cover.jpg",
                "source_size": list(original_size),
                "epub_size": list(check.size),
                "epub_mode": check.mode, "epub_bytes": dst.stat().st_size,
            }


def stage_markdown(md_text: str, mapping: dict[str, str], staging: Path) -> None:
    for src_rel, dst_rel in mapping.items():
        md_text = md_text.replace(f"]({src_rel})", f"]({dst_rel})")
    (staging / "book.md").write_text(md_text, encoding="utf-8")


def write_metadata(staging: Path, metadata: Path, css: Path,
                   title: str | None, description: str | None,
                   has_cover: bool) -> None:
    base = Path(metadata).read_text(encoding="utf-8")
    if title:
        base = re.sub(r"^title:.*$", f'title: "{title}"', base, flags=re.M)
    if description:
        base = re.sub(r"^description:.*$", f'description: "{description}"',
                      base, flags=re.M)
    if has_cover:
        base += 'cover-image: "images/cover.jpg"\n'
    (staging / "metadata.yaml").write_text(base, encoding="utf-8")
    shutil.copyfile(css, staging / "reader.css")


def build_with_pandoc(tools: EpubTools, staging: Path,
                      log: dict) -> Path:
    epub_tmp = staging / "book.epub"
    cmd = [
        str(tools.pandoc), str(staging / "book.md"),
        "--from", "markdown+footnotes", "--to", "epub3", "--standalone",
        "--split-level=1", "--toc-depth=2", "--css=reader.css",
        "--metadata-file=metadata.yaml",
        f"--resource-path={staging}", "-o", str(epub_tmp),
    ]
    proc = run(cmd, cwd=staging)
    log["pandoc"] = {"command": cmd, "returncode": proc.returncode,
                     "stderr": proc.stderr.strip()}
    if proc.returncode != 0 or not epub_tmp.is_file():
        raise RuntimeError(f"pandoc failed: {proc.stderr.strip()}")
    return epub_tmp


def _attrs(tag: str) -> dict[str, str]:
    return dict(re.findall(r'([\w:]+)="([^"]*)"', tag))


def _insert_backlink(aside_html: str, backlink: str) -> str:
    last_p = aside_html.rfind("</p>")
    if last_p != -1:
        return aside_html[:last_p] + backlink + aside_html[last_p:]
    return aside_html.replace("</aside>", backlink + "</aside>")


def patch_footnote_backlinks(epub_path: Path, log: dict) -> None:
    """Insert doc-backlink anchors for every doc-noteref, per-XHTML.

    The converter restarts footnote numbering in each chapter file, so ids
    resolve per file first; a noteref whose footnote lives in exactly one
    other file gets a cross-XHTML href. Only <aside> blocks are rewritten.
    """
    zin = zipfile.ZipFile(epub_path)
    xhtml_names = [n for n in zin.namelist()
                   if n.endswith(".xhtml") and not n.endswith("nav.xhtml")]
    docs = {n: zin.read(n).decode("utf-8") for n in xhtml_names}

    fn_locs: dict[str, list[str]] = {}
    for name, text in docs.items():
        for m in ASIDE_RE.finditer(text):
            fn_locs.setdefault(m.group(1), []).append(name)

    inserted = 0
    unresolved: list[dict] = []
    to_insert: dict[str, dict[str, list[str]]] = {n: {} for n in xhtml_names}
    noteref_count = 0
    for name, text in docs.items():
        for m in NOTEREF_TAG_RE.finditer(text):
            a = _attrs(m.group(0))
            href, ref_id = a.get("href", ""), a.get("id")
            noteref_count += 1
            fn_id = href.split("#")[-1]
            candidates = fn_locs.get(fn_id, [])
            if name in candidates:
                fn_file = name
            elif len(candidates) == 1:
                fn_file = candidates[0]
            else:
                unresolved.append({"file": name, "href": href, "id": ref_id})
                continue
            if not ref_id:
                unresolved.append({"file": name, "href": href, "id": ref_id})
                continue
            target = f"#{ref_id}" if fn_file == name \
                else f"{Path(name).name}#{ref_id}"
            backlink = (f'<a href="{target}" class="footnote-back" '
                        f'role="doc-backlink">↩︎</a>')
            bucket = to_insert[fn_file].setdefault(fn_id, [])
            if _attrs(backlink).get("href") not in {
                    _attrs(b).get("href") for b in bucket}:
                bucket.append(backlink)

    missing_backlinks: list[dict] = []
    for name, text in docs.items():
        def _sub(m: re.Match) -> str:
            nonlocal inserted
            fn_id = m.group(1)
            block = m.group(0)
            wanted = to_insert[name].get(fn_id, [])
            if not wanted:
                return block
            existing = {_attrs(t).get("href", "")
                        for t in BACKLINK_RE.findall(block)}
            added = [bl for bl in wanted
                     if _attrs(bl).get("href") not in existing]
            for bl in added:
                inserted += 1
            return _insert_backlink(block, "".join(added)) if added else block
        docs[name] = ASIDE_RE.sub(_sub, text)

    for name, fns in to_insert.items():
        for fn_id, bl_tags in fns.items():
            m = re.search(rf"<aside\b[^>]*\bid=\"{re.escape(fn_id)}\"[^>]*>"
                          r".*?</aside>", docs[name], re.S)
            if m is None:
                missing_backlinks.append({"file": name, "footnote": fn_id})
                continue
            have = {_attrs(t).get("href", "")
                    for t in BACKLINK_RE.findall(m.group(0))}
            if any(_attrs(bl).get("href") not in have for bl in bl_tags):
                missing_backlinks.append({"file": name, "footnote": fn_id})

    log["footnotes"] = {
        "noterefs": noteref_count, "backlinks_inserted": inserted,
        "unresolved_noterefs": unresolved,
        "missing_backlinks": missing_backlinks,
    }
    if any(docs[n] != zin.read(n).decode("utf-8") for n in xhtml_names):
        tmp = epub_path.with_suffix(".rewritten.epub")
        with zipfile.ZipFile(tmp, "w") as zout:
            for info in zin.infolist():
                data = docs[info.filename].encode("utf-8") \
                    if info.filename in docs else zin.read(info.filename)
                zi = zipfile.ZipInfo(info.filename, date_time=info.date_time)
                zi.compress_type = info.compress_type
                zi.external_attr = info.external_attr
                zout.writestr(zi, data)
        zin.close()
        os.replace(tmp, epub_path)
    else:
        zin.close()


def map_media_paths(epub_path: Path, staging: Path, mapping: dict[str, str],
                    log: dict) -> None:
    """Confirm the staged-image → converter-media mapping by byte equality
    and record the actual EPUB path for every source image."""
    import xml.etree.ElementTree as ET

    ns = {"opf": "http://www.idpf.org/2007/opf",
          "xhtml": "http://www.w3.org/1999/xhtml",
          "xlink": "http://www.w3.org/1999/xlink"}
    z = zipfile.ZipFile(epub_path)
    container = ET.fromstring(z.read("META-INF/container.xml"))
    opf_name = container.find(
        ".//{urn:oasis:names:tc:opendocument:xmlns:container}"
        "rootfile").get("full-path")
    opf = ET.fromstring(z.read(opf_name))
    opf_dir = Path(opf_name).parent
    id2href = {i.get("id"): i.get("href")
               for i in opf.findall(".//opf:manifest/opf:item", ns)}
    spine = [str(opf_dir / id2href[e.get("idref")])
             for e in opf.findall(".//opf:spine/opf:itemref", ns)
             if e.get("idref") in id2href]
    seen: list[str] = []
    for p in spine:
        if not p.endswith(".xhtml"):
            continue
        root = ET.fromstring(z.read(p))
        base = Path(p).parent
        for el in root.iter():
            src = None
            if el.tag == f"{{{ns['xhtml']}}}img":
                src = el.get("src")
            elif el.tag == f"{{{ns['xhtml']}}}image":
                src = el.get(f"{{{ns['xlink']}}}href") or el.get("href")
            if not src or src.startswith(("data:", "http")):
                continue
            full = posixpath.normpath(str(base / src))
            if full not in seen:
                seen.append(full)
    z.close()

    problems = []
    expected = list(mapping.values())
    if len(seen) != len(expected):
        problems.append(f"media count mismatch: {len(expected)} source "
                        f"images vs {len(seen)} in EPUB")
    else:
        with zipfile.ZipFile(epub_path) as zf:
            for zip_path, staged_rel in zip(seen, expected):
                staged_bytes = (staging / staged_rel).read_bytes()
                if zf.read(zip_path) != staged_bytes:
                    problems.append(f"content mismatch for {staged_rel}")
                    continue
                src_rel = next(k for k, v in mapping.items() if v == staged_rel)
                for entry in log["images"]:
                    if entry["source"] == src_rel:
                        entry["epub_path"] = str(
                            Path(zip_path).relative_to(opf_dir))
    if log.get("cover"):
        cover_bytes = (staging / "images" / "cover.jpg").read_bytes()
        with zipfile.ZipFile(epub_path) as zf:
            for n in zf.namelist():
                if n.startswith(str(opf_dir / "media")) and \
                        zf.read(n) == cover_bytes:
                    log["cover"]["epub_path"] = str(
                        Path(n).relative_to(opf_dir))
                    break
    if problems:
        raise RuntimeError("; ".join(problems))


def read_opf(z: zipfile.ZipFile) -> tuple[str, "object"]:
    import xml.etree.ElementTree as ET
    container = ET.fromstring(z.read("META-INF/container.xml"))
    opf_name = container.find(
        ".//{urn:oasis:names:tc:opendocument:xmlns:container}"
        "rootfile").get("full-path")
    return opf_name, ET.fromstring(z.read(opf_name))


def _ns() -> dict[str, str]:
    return {"opf": "http://www.idpf.org/2007/opf",
            "xhtml": "http://www.w3.org/1999/xhtml",
            "epub": "http://www.idpf.org/2007/ops",
            "xlink": "http://www.w3.org/1999/xlink"}


def _spine(z: zipfile.ZipFile) -> list[str]:
    ns = _ns()
    opf_name, opf = read_opf(z)
    opf_dir = Path(opf_name).parent
    id2href = {i.get("id"): i.get("href")
               for i in opf.findall(".//opf:manifest/opf:item", ns)}
    return [str(opf_dir / id2href[e.get("idref")])
            for e in opf.findall(".//opf:spine/opf:itemref", ns)
            if e.get("idref") in id2href]


def verify_zip(epub_path: Path, log: dict) -> list[str]:
    errors: list[str] = []
    with zipfile.ZipFile(epub_path) as z:
        bad = z.testzip()
        infos = z.infolist()
        log["zip"] = {
            "entries": len(infos), "testzip_ok": bad is None,
            "mimetype_first_stored": (
                infos[0].filename == "mimetype"
                and infos[0].compress_type == zipfile.ZIP_STORED),
        }
        if bad:
            errors.append(f"corrupt zip entry: {bad}")
        if not log["zip"]["mimetype_first_stored"]:
            errors.append("mimetype is not the first stored entry")
    return errors


def verify_manifest(z: zipfile.ZipFile, log: dict) -> list[str]:
    ns = _ns()
    errors: list[str] = []
    opf_name, opf = read_opf(z)
    opf_dir = Path(opf_name).parent
    items = {}
    for item in opf.findall(".//opf:manifest/opf:item", ns):
        items[item.get("id")] = {"href": item.get("href")}
    names = set(z.namelist())
    missing = []
    for iid, it in items.items():
        if posixpath.normpath(str(opf_dir / it["href"])) not in names:
            missing.append({"id": iid, "href": it["href"]})
    manifested = {posixpath.normpath(str(opf_dir / it["href"]))
                  for it in items.values()}
    unmanifested = sorted(
        n for n in names if not n.endswith("/") and n != "mimetype"
        and n != opf_name and not n.startswith("META-INF/")
        and n not in manifested)
    log["manifest"] = {"opf": opf_name, "items": len(items),
                       "missing": missing,
                       "unmanifested_files": unmanifested}
    if missing:
        errors.append(f"manifest hrefs missing from zip: {missing}")
    if unmanifested:
        errors.append(f"files not listed in manifest: {unmanifested}")
    return errors


def verify_image_links(z: zipfile.ZipFile, log: dict) -> list[str]:
    import xml.etree.ElementTree as ET
    ns = _ns()
    errors: list[str] = []
    names = set(z.namelist())
    checked, missing = 0, []
    spine_files = []
    for p in _spine(z):
        spine_files.append(p)
        if not p.endswith(".xhtml"):
            continue
        root = ET.fromstring(z.read(p))
        base = Path(p).parent
        for el in root.iter():
            src = None
            if el.tag == f"{{{ns['xhtml']}}}img":
                src = el.get("src")
            elif el.tag == f"{{{ns['xhtml']}}}image":
                src = el.get(f"{{{ns['xlink']}}}href") or el.get("href")
            if not src or src.startswith(("data:", "http")):
                continue
            checked += 1
            if posixpath.normpath(str(base / src)) not in names:
                missing.append({"file": p, "src": src})
    log["image_links"] = {"checked": checked, "missing": missing,
                          "spine_files": spine_files}
    if missing:
        errors.append(f"unresolvable image links: {missing}")
    return errors


def verify_image_pixels(z: zipfile.ZipFile, log: dict) -> list[str]:
    ns = _ns()
    errors: list[str] = []
    opf_name, opf = read_opf(z)
    opf_dir = Path(opf_name).parent
    hrefs = {Path(i.get("href")).name: str(opf_dir / i.get("href"))
             for i in opf.findall(".//opf:manifest/opf:item", ns)}
    for entry in log["images"]:
        name = hrefs.get(Path(entry["epub_path"]).name)
        if name is None or name not in z.namelist():
            errors.append(f"image not in manifest/zip: {entry['epub_path']}")
            continue
        with Image.open(io.BytesIO(z.read(name))) as im:
            if list(im.size) != entry["source_size"]:
                errors.append(f"size changed for {entry['epub_path']}: "
                              f"{im.size} != {entry['source_size']}")
            if im.mode != "L":
                errors.append(
                    f"not grayscale: {entry['epub_path']} mode={im.mode}")
    return errors


def verify_footnote_links(z: zipfile.ZipFile, log: dict) -> list[str]:
    import xml.etree.ElementTree as ET
    errors: list[str] = []
    spine = _spine(z)
    ids_by_file: dict[str, set] = {}
    for p in spine:
        if not p.endswith(".xhtml"):
            continue
        root = ET.fromstring(z.read(p))
        ids_by_file[p] = {e.get("id") for e in root.iter() if e.get("id")}
    basename = {Path(p).name: p for p in spine}

    def _resolve(p: str, href: str) -> tuple[str, str]:
        path_part, _, frag = href.partition("#")
        return (basename.get(path_part, p) if path_part else p), frag

    unresolved, backlinkless = [], []
    for p in spine:
        if not p.endswith(".xhtml"):
            continue
        text = z.read(p).decode("utf-8")
        noterefs = [_attrs(m.group(0))
                    for m in NOTEREF_TAG_RE.finditer(text)]
        for a in noterefs:
            target, frag = _resolve(p, a.get("href", ""))
            if not frag or frag not in ids_by_file.get(target, set()):
                unresolved.append({"file": p, "href": a.get("href")})
        for m in ASIDE_RE.finditer(text):
            fn_id, block = m.group(1), m.group(0)
            refs = [a for a in noterefs
                    if _resolve(p, a.get("href", ""))[1] == fn_id]
            if not refs:
                unresolved.append({"file": p, "orphan_footnote": fn_id})
                continue
            have = {_attrs(t).get("href", "")
                    for t in BACKLINK_RE.findall(block)}
            for a in refs:
                t_file, _ = _resolve(p, a.get("href", ""))
                expected = (f"#{a['id']}" if t_file == p
                            else f"{Path(p).name}#{a['id']}")
                if expected not in have:
                    backlinkless.append(
                        {"file": p, "footnote": fn_id,
                         "expected_backlink": expected})
    log["footnote_links"] = {"unresolved": unresolved,
                             "missing_backlinks": backlinkless}
    if unresolved:
        errors.append(f"unresolved footnote hrefs/ids: {unresolved}")
    if backlinkless:
        errors.append(f"footnote asides missing backlink: {backlinkless}")
    return errors


def verify_chapters_and_nav(z: zipfile.ZipFile, expected_h1: int,
                            log: dict) -> list[str]:
    import xml.etree.ElementTree as ET
    ns = _ns()
    errors: list[str] = []
    spine = _spine(z)
    chapter_files = [p for p in spine if Path(p).name.startswith("ch")]
    log["chapters"] = {"markdown_h1": expected_h1,
                       "spine_items": len(spine),
                       "chapter_xhtml": len(chapter_files),
                       "files": [Path(p).name for p in chapter_files]}
    if len(chapter_files) != expected_h1:
        errors.append(f"chapter count mismatch: markdown has {expected_h1} "
                      f"'# ' headings but EPUB has {len(chapter_files)}")

    nav_items = []
    opf_name, opf = read_opf(z)
    opf_dir = Path(opf_name).parent
    for i in opf.findall(".//opf:manifest/opf:item", ns):
        if "nav" in (i.get("properties") or ""):
            nav_items.append(i)
    nav_missing = []
    if not nav_items:
        errors.append("no nav document in manifest")
    else:
        nav_path = str(opf_dir / nav_items[0].get("href"))
        root = ET.fromstring(z.read(nav_path))
        names = set(z.namelist())
        targets = 0
        for el in root.iter():
            href = el.get("href") or el.get("{{http://www.w3.org/1999/xhtml}}href")
            if not href or href.startswith(("http", "data:")):
                continue
            targets += 1
            target = posixpath.normpath(
                str(Path(nav_path).parent / href.split("#")[0]))
            if target not in names:
                nav_missing.append(href)
            elif "#" in href:
                frag = href.split("#")[1]
                doc = ET.fromstring(z.read(target))
                if frag and frag not in {e.get("id") for e in doc.iter()
                                         if e.get("id")}:
                    nav_missing.append(href)
        log["nav"] = {"doc": Path(nav_path).name, "targets": targets,
                      "missing": nav_missing}
        if nav_missing:
            errors.append(f"nav targets unresolved: {nav_missing}")
    return errors


def run_epubcheck(tools: EpubTools, epub_path: Path, staging: Path,
                  log: dict) -> list[str]:
    errors: list[str] = []
    json_out = staging / "epubcheck.json"
    proc = run([str(tools.java), "-jar", str(tools.epubcheck),
                str(epub_path), "--json", str(json_out)])
    log["epubcheck"] = {"jar": str(tools.epubcheck),
                        "returncode": proc.returncode,
                        "stdout": proc.stdout.strip()[-2000:],
                        "stderr": proc.stderr.strip()[-2000:]}
    if not json_out.is_file():
        errors.append("epubcheck produced no JSON report")
        return errors
    report = json.loads(json_out.read_text(encoding="utf-8"))
    messages = report.get("messages", [])
    n_errors = sum(1 for m in messages if m.get("severity") == "ERROR")
    n_warnings = sum(1 for m in messages
                     if m.get("severity") == "WARNING")
    log["epubcheck"].update({
        "errors": n_errors, "warnings": n_warnings,
        "messages": [
            {"severity": m.get("severity"), "message": m.get("message"),
             "locations": [
                 {"path": loc.get("path"), "line": loc.get("line"),
                  "column": loc.get("column")}
                 for loc in (m.get("locations") or [])
             ] if m.get("locations") else None}
            for m in messages],
    })
    if n_errors or n_warnings:
        errors.append(f"epubcheck reported {n_errors} errors / "
                      f"{n_warnings} warnings")
    return errors


def build_epub(markdown: Path, output: Path, metadata: Path, css: Path,
               tools: EpubTools, cover: Path | None = None,
               title: str | None = None, description: str | None = None,
               log_path: Path | None = None) -> int:
    """Full package-and-verify cycle; returns a process exit code."""
    output = output.resolve()
    log_path = (log_path or output.with_suffix(
        ".build-log.json")).resolve()
    log: dict = {
        "ok": True, "failures": [], "stage": "init",
        "markdown": str(markdown.resolve()), "output": str(output),
        "cover": str(cover.resolve()) if cover else None,
        "images": [],
        "tools": {
            "pandoc": tool_version([str(tools.pandoc), "--version"],
                                   r"pandoc \d+\.\d+"),
            "java": tool_version([str(tools.java), "-version"],
                                 r"\d+\.\d+\.\d+"),
            "epubcheck": tools.epubcheck.name,
        },
    }

    def fail(stage: str, detail: str) -> int:
        log["stage"] = stage
        log_fail(log, stage, detail)
        log_path.parent.mkdir(parents=True, exist_ok=True)
        tmp_log = log_path.with_name(log_path.name + ".tmp")
        tmp_log.write_text(json.dumps(log, ensure_ascii=False, indent=2),
                           encoding="utf-8")
        os.replace(tmp_log, log_path)
        print(json.dumps(log, ensure_ascii=False, indent=2))
        return 1

    if output.exists():
        return fail("preflight", f"refusing to overwrite existing: {output}")
    for tool in (tools.pandoc, tools.java, tools.epubcheck, metadata, css):
        if not Path(tool).exists():
            return fail("preflight", f"missing tool/asset: {tool}")
    if not markdown.is_file():
        return fail("preflight", f"markdown not found: {markdown}")
    if cover is not None and not cover.is_file():
        return fail("preflight", f"cover not found: {cover}")

    staging = Path(tempfile.mkdtemp(prefix=".epub-build-", dir=output.parent))
    try:
        log["stage"] = "images"
        md_text = markdown.read_text(encoding="utf-8")
        expected_h1 = len(re.findall(r"^# ", md_text, re.M))
        try:
            mapping, inline = collect_images(md_text)
        except ValueError as e:
            return fail("images", str(e))
        log["skipped_inline_images"] = inline
        try:
            convert_images(markdown, mapping, staging, cover, log)
        except (FileNotFoundError, ValueError) as e:
            return fail("images", str(e))

        log["stage"] = "pandoc"
        stage_markdown(md_text, mapping, staging)
        write_metadata(staging, metadata, css, title, description,
                       has_cover=cover is not None)
        try:
            epub_tmp = build_with_pandoc(tools, staging, log)
        except RuntimeError as e:
            return fail("pandoc", str(e))

        log["stage"] = "footnotes"
        patch_footnote_backlinks(epub_tmp, log)
        try:
            map_media_paths(epub_tmp, staging, mapping, log)
        except RuntimeError as e:
            return fail("footnotes", str(e))

        log["stage"] = "verify"
        errors: list[str] = []
        with zipfile.ZipFile(epub_tmp) as z:
            errors += verify_zip(epub_tmp, log)
            errors += verify_manifest(z, log)
            errors += verify_image_links(z, log)
            errors += verify_image_pixels(z, log)
            errors += verify_footnote_links(z, log)
            errors += verify_chapters_and_nav(z, expected_h1, log)
        errors += run_epubcheck(tools, epub_tmp, staging, log)
        if errors:
            return fail("verify", "; ".join(errors))

        log["stage"] = "publish"
        log["bytes"] = epub_tmp.stat().st_size
        tmp_out = output.with_name(output.name + ".tmp")
        shutil.copyfile(epub_tmp, tmp_out)
        os.replace(tmp_out, output)
        tmp_log = log_path.with_name(log_path.name + ".tmp")
        tmp_log.write_text(json.dumps(log, ensure_ascii=False, indent=2),
                           encoding="utf-8")
        os.replace(tmp_log, log_path)
        log["stage"] = "done"
        print(json.dumps(log, ensure_ascii=False, indent=2))
        return 0
    finally:
        shutil.rmtree(staging, ignore_errors=True)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--markdown", required=True, type=Path)
    ap.add_argument("--output", required=True, type=Path)
    ap.add_argument("--metadata", required=True, type=Path,
                    help="base metadata YAML (title/description overridden)")
    ap.add_argument("--css", required=True, type=Path)
    ap.add_argument("--title", default=None)
    ap.add_argument("--description", default=None)
    ap.add_argument("--cover", type=Path, default=None)
    ap.add_argument("--log", type=Path, default=None)
    ap.add_argument("--epub-tools", type=Path, default=DEFAULT_TOOLS,
                    help="pinned pandoc/epubcheck/jre root")
    ap.add_argument("--pandoc", type=Path, default=None)
    ap.add_argument("--java", type=Path, default=None)
    ap.add_argument("--epubcheck", type=Path, default=None)
    args = ap.parse_args(argv)
    tools = EpubTools(args.epub_tools, args.pandoc, args.java, args.epubcheck)
    return build_epub(args.markdown, args.output, args.metadata, args.css,
                      tools, args.cover, args.title, args.description,
                      args.log)


if __name__ == "__main__":
    sys.exit(main())
