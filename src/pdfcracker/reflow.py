"""Geometry-authoritative paragraph reflow for assembled book Markdown.

OCR pipelines split one printed paragraph into several Markdown blocks at
every hard line break the converter emitted, and text-only heuristics
cannot distinguish a wrap at a sentence-final period from a real paragraph
boundary. Chinese typographic books obey a strict, recoverable invariant:

* every paragraph's first print line is indented (2 em, ~0.047 page-width);
* continuation lines start flush at the column's left margin;
* justified paragraphs stop filling to the right margin only on their
  final line (trailing full-width punctuation squeezes ink ~0.035).

So a block B is a continuation of the previous body block P iff B's first
print line is FLUSH at its column edge AND P's last print line still
REACHES its column's right edge. Both facts are read from line-level OCR
geometry (normalized bboxes); text heuristics are only a fallback when a
block cannot be matched to a print line.

Validated on a 427-page scanned book via Apple Vision line OCR plus a
figure-asset manifest. Pitfalls baked into the rules below (each one
produced wrong merges or missed merges before the fix):

* The bottom-note area must be located from the first line starting with a
  circled marker (①②…), not from a y threshold: notes can start as high as
  y0≈0.837 while pages without notes run body text down to y0≈0.878.
* The left-margin mode is polluted by indented quotes and by short
  paragraphs whose first lines make the indent position dense; only a
  figure-verified wrap column (figure ending at/before the line start and
  vertically overlapping it) may serve as a second left edge.
* Divider/TOC pages contain only short stacked lines (no full-width line);
  flush/reach geometry must be frozen there.
* Enumeration items (一、/1./（1）) and source notes ("- 本文发表于…")
  never merge regardless of geometry.

Every merge decision is returned with its evidence (page, bboxes, margins,
flush/reach verdicts) for provenance logs; non-space character count is
asserted unchanged by the caller-facing ``reflow_markdown`` helper.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from collections import Counter
from dataclasses import dataclass, field
from difflib import SequenceMatcher
from pathlib import Path

__all__ = [
    "FLUSH_TOL", "REACH_TOL", "Page", "load_pages", "load_figures",
    "reflow_blocks", "reflow_markdown", "main",
]

#: ~1 char; the paragraph indent is ~0.047 (2 em).
FLUSH_TOL = 0.020
#: Justified lines stop short only on the final line; trailing full-width
#: punctuation squeezes ink by up to ~0.035.
REACH_TOL = 0.055
#: A second column is only plausible this far right of the main margin.
COLUMN_OFFSET = 0.06
HEADER_Y_MAX = 0.10
FOOTNOTE_Y_MIN = 0.86
MATCH_RATIO = 0.66
PREFIX_CHARS = 12
CIRCLED = "①②③④⑤⑥⑦⑧⑨⑩⑪⑫⑬⑭⑮⑯⑰⑱⑲⑳"

CJK_RE = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff]")
STRIP_RE = re.compile(r"[^\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaffA-Za-z0-9]")
FOOTREF_RE = re.compile(r"\[\^[^\]]*\]")
NOTE_PREFIX_RE = re.compile(r"^（第\s*\d+\s*页，\s*注\s*\d+\s*）")
CIRCLED_RE = re.compile(f"^[{CIRCLED}]")
ANNOTATION_START_RE = re.compile(
    r"^(-\s|—\s?|–\s?|·|※|（注|注：|译注|编者按|本文发表于|原载|摘自)")
LISTY_NEXT_RE = re.compile(
    r"^(\d+[.、．)]|[（(]\d+[)）]|[一二三四五六七八九十]+、|"
    r"第[一二三四五六七八九十百\d]+[章节部分篇])")
TAIL_OK = tuple("。！？!?…—–-」』）》〉”’\"'%.")
TAIL_JOIN_PUNCT = tuple("，、；：,;:")
PAGE_NUM_TAIL_RE = re.compile(r"[ …···]*\d+\s*$")

#: Freeze merging inside front-matter/TOC regions: their lines are
#: intentional list lines even when they look flush and justified.
DEFAULT_EXCLUDED_REGIONS = (("# 封面与出版资料", "# 作者序"),
                            ("# 原书目录", "# 第一部分"))


def norm_text(s: str) -> str:
    s = FOOTREF_RE.sub("", s)
    s = re.sub(r"[*_~`]", "", s)
    return STRIP_RE.sub("", s)


def classify(block: str) -> str:
    s = block.strip()
    if not s:
        return "empty"
    if re.match(r"^#{1,6} ", s):
        return "heading"
    if s.startswith("!["):
        return "image"
    if s.startswith("[^"):
        return "notedef"
    if s.startswith(">"):
        return "quote"
    if s.startswith("|"):
        return "table"
    if s.startswith("<!--"):
        return "comment"
    if NOTE_PREFIX_RE.match(s) or CIRCLED_RE.match(s):
        return "note"
    if s.startswith("*"):
        return "strap"
    return "body"


def hard_guarded(next_block: str) -> bool:
    """Blocks that must never merge regardless of geometry."""
    s = next_block.lstrip()
    return bool(ANNOTATION_START_RE.match(s) or CIRCLED_RE.match(s)
                or s.startswith("[^") or NOTE_PREFIX_RE.match(s))


def text_fallback_joinable(prev_text: str, next_text: str) -> bool:
    """Conservative text predicate for blocks without usable geometry."""
    p = prev_text.rstrip()
    n = next_text.lstrip()
    while p and p[-1] in "*_~":
        p = p[:-1].rstrip()
    if not p or len(re.sub(r"[*_~]", "", p).strip()) <= 4:
        return False
    if p.strip().startswith("*") and p.rstrip().endswith("*"):
        return False
    if PAGE_NUM_TAIL_RE.search(p):
        return False
    if re.search(r"[\u4e00-\u9fff] [\u4e00-\u9fff]", p) or \
            re.search(r"[\u4e00-\u9fff] [\u4e00-\u9fff]", n):
        return False  # space-separated classical quotation
    tail = p[-1]
    if tail in TAIL_OK or tail.isdigit():
        return False
    if not (CJK_RE.match(tail) or tail.isascii() and tail.islower()
            or tail in TAIL_JOIN_PUNCT):
        return False
    if re.search(r"(如下|如左|如次)[:：]\s*[*_~]*$", p):
        return False
    head = n[:1]
    if not head or hard_guarded(n):
        return False
    latin_paren = bool(re.match(r"^[（(][A-Za-z][A-Za-z .·'’\-]*[)）]", n))
    if head in tuple("#!>[|*0123456789（()「『“‘—…·%") and not latin_paren:
        return False
    if LISTY_NEXT_RE.match(n):
        return False
    if head.isascii() and head.isupper():
        return False
    return True


def joiner(prev: str, nxt: str) -> str:
    a, b = prev[-1:], nxt[:1]
    if a and b and a.isascii() and a.isalnum() and b.isascii() and b.isalnum():
        return " "
    return ""


def _lowest_dense_cluster(values: list[float]) -> float | None:
    """Left margin: continuation lines are the leftmost recurring group."""
    if not values:
        return None
    counts = Counter(values)
    dense = sorted(v for v, n in counts.items() if n >= 2)
    return dense[0] if dense else min(values)


def _highest_dense_cluster(values: list[float]) -> float | None:
    """Right margin: justified body lines cluster at the right edge."""
    if not values:
        return None
    counts = Counter(values)
    dense = sorted((v for v, n in counts.items() if n >= 2), reverse=True)
    return dense[0] if dense else max(values)


@dataclass
class Page:
    """Line-level OCR evidence for one page (normalized bboxes)."""

    num: int
    lines: list[dict]
    figures: list[tuple[float, float, float, float]] = field(default_factory=list)
    footnote_start: int | None = None
    margin: float | None = None
    right: float | None = None
    x0_clusters: list[float] = field(default_factory=list)
    x1_clusters: list[float] = field(default_factory=list)
    is_display: bool = False

    def __post_init__(self) -> None:
        self.lines = sorted(self.lines, key=lambda L: L["bbox"][1])
        for L in self.lines:
            L["norm"] = norm_text(L.get("text") or "")
        self.footnote_start = self._footnote_start()
        core = [L for i, L in enumerate(self.lines) if self._is_core(i)]
        self.margin = _lowest_dense_cluster(
            [round(L["bbox"][0], 3) for L in core])
        self.right = _highest_dense_cluster(
            [round(L["bbox"][2], 3) for L in core])
        self.x0_clusters = sorted(v for v, n in Counter(
            round(L["bbox"][0], 3) for L in core).items() if n >= 2)
        self.x1_clusters = sorted(v for v, n in Counter(
            round(L["bbox"][2], 3) for L in core).items() if n >= 2)
        wide = [L for L in core if L["bbox"][2] - L["bbox"][0] >= 0.55]
        self.is_display = len(core) >= 4 and not wide

    def _footnote_start(self) -> int | None:
        """Index of the first bottom-note line: every footnote in this book
        starts with a circled number; positional thresholds fail."""
        for i, L in enumerate(self.lines):
            t = (L.get("text") or "").strip()
            if t and t[0] in CIRCLED:
                return i
        return None

    def _is_core(self, i: int) -> bool:
        L = self.lines[i]
        y0 = L["bbox"][1]
        if y0 < HEADER_Y_MAX:
            return False
        if self.footnote_start is not None:
            return i < self.footnote_start
        # No circled-number footnote: body text may extend below y=0.86,
        # so only drop bare page-number lines there.
        if y0 >= FOOTNOTE_Y_MIN and len(L["norm"]) < 4:
            return False
        return True

    def core_indices(self) -> list[int]:
        return [i for i in range(len(self.lines)) if self._is_core(i)]

    def local_right(self, x1: float) -> float | None:
        """Right edge of the column this line ends in (wrapped figures make
        narrow columns whose right edge is far left of the page)."""
        if not self.x1_clusters:
            return self.right
        for c in reversed(self.x1_clusters):
            if c <= x1 + 0.012:
                return c
        return self.x1_clusters[0]

    def wrap_column(self, x0: float, yc: float) -> bool:
        """True if this line starts beside a figure (text-wrap layout).

        Stacked lists and indented quotes also start far right, but nothing
        is printed to their left; a wrap column needs figure evidence.
        """
        for fx0, fy0, fx1, fy1 in self.figures:
            if fx0 < x0 - 0.04 and fx1 <= x0 + 0.03 \
                    and fy0 - 0.05 <= yc <= fy1 + 0.05:
                return True
        return False


def load_pages(vision_dir: Path) -> dict[int, Page]:
    """Load ``apple-vision-page-NNNN.json`` line records as pages."""
    pages: dict[int, Page] = {}
    for f in sorted(Path(vision_dir).glob("*-page-*.json")):
        try:
            num = int(f.stem.rsplit("-", 1)[1])
        except ValueError:
            continue
        pages[num] = Page(num, json.loads(f.read_text(encoding="utf-8")))
    return pages


def load_figures(path: Path) -> dict[int, list[tuple[float, ...]]]:
    """Figure bboxes per page (``bbox_norm``), for wrap-column detection."""
    out: dict[int, list[tuple[float, ...]]] = {}
    p = Path(path)
    if not p.is_file():
        return out
    for f in json.loads(p.read_text(encoding="utf-8")):
        bb, page = f.get("bbox_norm"), f.get("page")
        if bb and page:
            out.setdefault(int(page), []).append(tuple(bb))
    return out


def attach_figures(pages: dict[int, Page],
                   figures: dict[int, list[tuple[float, ...]]]) -> None:
    for num, boxes in figures.items():
        if num in pages:
            pages[num].figures = boxes  # type: ignore[assignment]


@dataclass
class BlockGeom:
    block: str
    index: int
    norm: str = ""
    kind: str = "body"
    page: int | None = None
    line: int | None = None
    ratio: float = 0.0
    matched_line: dict | None = None
    page_obj: Page | None = None
    excluded: bool = False

    def __post_init__(self) -> None:
        self.norm = norm_text(self.block)
        self.kind = classify(self.block)

    @property
    def flush(self) -> bool | None:
        """Does this block's first print line start at its column edge?

        Paragraph first lines sit one indent (~0.047) right of the main
        margin; a far-right start only counts when the line wraps beside a
        figure (verified against figure bboxes).
        """
        if self.matched_line is None or self.page_obj is None:
            return None
        margin = self.page_obj.margin
        if margin is None:
            return None
        x0, y0, _, y1 = self.matched_line["bbox"]
        if x0 - margin <= FLUSH_TOL:
            return True
        if x0 >= margin + COLUMN_OFFSET and \
                self.page_obj.wrap_column(x0, (y0 + y1) / 2):
            return True
        return False

    @property
    def in_footnote_zone(self) -> bool:
        if self.matched_line is None or self.page_obj is None:
            return False
        page = self.page_obj
        if page.footnote_start is not None and \
                (self.line or 0) >= page.footnote_start:
            return True
        return self.matched_line["bbox"][1] >= FOOTNOTE_Y_MIN


def find_excluded(blocks: list[str],
                  regions: tuple[tuple[str, str], ...]) -> list[tuple[int, int]]:
    ranges = []
    for start_mark, end_mark in regions:
        start = end = None
        for i, b in enumerate(blocks):
            if b.strip() == start_mark:
                start = i
            if start is not None and b.strip().startswith(end_mark):
                end = i
                break
        if start is not None and end is not None:
            ranges.append((start, end))
    return ranges


def match_first_line(pnorm: str, pages: dict[int, Page],
                     page_order: list[int], cursor: int
                     ) -> tuple[int, int, float] | None:
    """Best match of a block's first print line: a forward page window
    first, then a bounded backward window (chapter-end note blocks point at
    earlier pages). None below MATCH_RATIO."""
    probe = pnorm[:PREFIX_CHARS]
    if not probe:
        return None
    fwd = page_order[cursor:cursor + 8]
    bwd = page_order[max(0, cursor - 45):cursor][::-1]

    def scan(order: list[int]) -> tuple[float, tuple[int, int] | None]:
        best = (0.0, None)
        for pi in order:
            page = pages.get(pi)
            if page is None:
                continue
            for li, L in enumerate(page.lines):
                if len(L["norm"]) < 4:
                    continue
                r = SequenceMatcher(None, probe,
                                    L["norm"][:PREFIX_CHARS]).ratio()
                if r > best[0]:
                    best = (r, (pi, li))
        return best

    for order in (fwd, bwd):
        ratio, hit = scan(order)
        if hit is not None and ratio >= MATCH_RATIO:
            return hit[0], hit[1], ratio
    return None


def find_last_line(p: BlockGeom, b: BlockGeom | None,
                   pages: dict[int, Page]) -> dict | None:
    """P's last print line = nearest body-flow line above B's first line.

    Markdown keeps print order, so every core (non-header, non-footnote)
    line between P's first line and B's first line belongs to P; the line
    directly above B — or the previous page's last core line when B opens a
    page — is P's final line, without any text matching.
    """
    if b is None or b.page is None or b.line is None:
        return None
    page = pages.get(b.page)
    if page is None:
        return None
    for li in range(b.line - 1, -1, -1):
        if not page._is_core(li):
            continue
        if b.page == p.page and p.line is not None and li < p.line:
            return None  # geometric sanity failure
        return page.lines[li]
    for pi in range(b.page - 1, max(0, (p.page or 1) - 1), -1):
        prev = pages.get(pi)
        if prev is None:
            continue
        for li in range(len(prev.lines) - 1, -1, -1):
            if prev._is_core(li):
                return prev.lines[li]
    return None


def reflow_blocks(blocks: list[str], pages: dict[int, Page],
                  page_order: list[int],
                  excluded_regions: tuple[tuple[str, str], ...] | None = None,
                  ) -> tuple[list[str], dict]:
    """Merge mid-paragraph block splits; returns new blocks + report."""
    regions = DEFAULT_EXCLUDED_REGIONS if excluded_regions is None \
        else excluded_regions
    excluded = find_excluded(blocks, regions)
    geoms: list[BlockGeom] = []
    cursor = 0
    stats: Counter = Counter()
    for i, b in enumerate(blocks):
        g = BlockGeom(b, i)
        g.excluded = any(s <= i < e for s, e in excluded)
        geoms.append(g)
        if g.kind != "body" or g.excluded:
            continue
        m = match_first_line(g.norm, pages, page_order, cursor)
        if m is None:
            stats["unmatched"] += 1
            continue
        pi, li, ratio = m
        g.page, g.line, g.ratio = pi, li, ratio
        g.matched_line = pages[pi].lines[li]
        g.page_obj = pages[pi]
        cursor = max(cursor, page_order.index(pi))
        stats["matched"] += 1

    out: list[str] = []
    decisions: list[dict] = []
    buf: BlockGeom | None = None

    def emit() -> None:
        nonlocal buf
        if buf is not None:
            out.append(buf.block)
        buf = None

    for g in geoms:
        if g.kind != "body" or g.excluded:
            emit()
            out.append(g.block)
            continue
        if buf is None:
            buf = g
            continue
        decision: dict = {"block_index": g.index, "rule": None, "merged": False}
        same_flow = (buf.page is not None and g.page is not None
                     and g.page >= buf.page)
        b_flush = g.flush
        p_last = find_last_line(buf, g, pages)
        reach = None
        if p_last is not None and buf.page_obj is not None:
            local = buf.page_obj.local_right(p_last["bbox"][2])
            if local is not None:
                reach = p_last["bbox"][2] >= local - REACH_TOL
        geometry_ok = (
            same_flow and b_flush is True and reach is True
            and not g.in_footnote_zone
            and not (g.page_obj and g.page_obj.is_display)
            and not (buf.page_obj and buf.page_obj.is_display)
            and not hard_guarded(g.block)
            and not LISTY_NEXT_RE.match(g.block.strip()))
        if geometry_ok:
            decision["rule"] = "geometry"
            decision["merged"] = True
        elif b_flush is None or reach is None or not same_flow:
            if not hard_guarded(g.block) and \
                    text_fallback_joinable(buf.block, g.block):
                decision["rule"] = "fallback-text"
                decision["merged"] = True
        decision.update({
            "prev_kind": buf.kind, "prev_page": buf.page,
            "this_page": g.page, "match_ratio": round(g.ratio, 3),
            "b_first_x0": round(g.matched_line["bbox"][0], 4)
            if g.matched_line else None,
            "page_margin": round(g.page_obj.margin, 4)
            if g.page_obj and g.page_obj.margin is not None else None,
            "b_flush": b_flush,
            "p_last_x1": round(p_last["bbox"][2], 4) if p_last else None,
            "page_right": round(buf.page_obj.right, 4)
            if buf.page_obj and buf.page_obj.right is not None else None,
            "p_reach": reach,
        })
        decisions.append(decision)
        if decision["merged"]:
            buf.block = buf.block.rstrip() + joiner(buf.block.rstrip(),
                                                    g.block.strip()) \
                + g.block.strip()
            buf.norm = (buf.norm or "") + g.norm
            if g.page is not None:
                buf.page, buf.line = g.page, g.line
                buf.page_obj = g.page_obj
            stats["merged_geometry" if decision["rule"] == "geometry"
                  else "merged_fallback"] += 1
        else:
            emit()
            buf = g
    emit()
    stats["blocks_in"] = len(blocks)
    stats["blocks_out"] = len(out)
    return out, {"stats": dict(stats), "decisions": decisions}


def reflow_markdown(md_text: str, pages: dict[int, Page],
                    excluded_regions: tuple[tuple[str, str], ...] | None = None,
                    ) -> tuple[str, dict]:
    """Reflow a whole Markdown document; asserts no text is lost."""
    blocks = [b for b in re.split(r"\n[ \t]*\n", md_text) if b.strip("\n")]
    out_blocks, report = reflow_blocks(
        blocks, pages, sorted(pages), excluded_regions=excluded_regions)
    result = "\n\n".join(out_blocks) + "\n"
    n_in = len(re.sub(r"\s", "", md_text))
    n_out = len(re.sub(r"\s", "", result))
    report["integrity"] = {"nonspace_chars_in": n_in, "nonspace_chars_out": n_out}
    if n_in != n_out:
        raise ValueError("reflow changed the non-space character count")
    return result, report


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--input", required=True, type=Path)
    ap.add_argument("--output", required=True, type=Path)
    ap.add_argument("--log", type=Path, default=None)
    ap.add_argument("--vision-dir", required=True, type=Path,
                    help="directory of *-page-NNNN.json line records")
    ap.add_argument("--figures", type=Path, default=None,
                    help="figure manifest with bbox_norm (wrap columns)")
    ap.add_argument("--force", action="store_true")
    args = ap.parse_args(argv)

    pages = load_pages(args.vision_dir)
    if args.figures:
        attach_figures(pages, load_figures(args.figures))
    src = args.input.read_text(encoding="utf-8")
    try:
        result, report = reflow_markdown(src, pages)
    except ValueError as e:
        print(f"refusing: {e}", file=sys.stderr)
        return 1
    log_path = args.log or args.output.with_suffix(".reflow-geometry.json")
    report["input"] = str(args.input.resolve())
    report["output"] = str(args.output.resolve())
    if args.output.exists() and not args.force:
        print(f"refusing to overwrite existing: {args.output}", file=sys.stderr)
        return 1
    tmp = args.output.with_name(args.output.name + ".tmp")
    tmp.write_text(result, encoding="utf-8")
    os.replace(tmp, args.output)
    tmp_log = log_path.with_name(log_path.name + ".tmp")
    tmp_log.write_text(json.dumps(report, ensure_ascii=False, indent=2),
                       encoding="utf-8")
    os.replace(tmp_log, log_path)
    print(json.dumps({"stats": report["stats"],
                      "integrity": report["integrity"]},
                     ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
