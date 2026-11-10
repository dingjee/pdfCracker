"""Paragraph join planning helpers and reviewed-join application.

OCR splits one source paragraph into several blocks: around figures on the
same page and at every page turn.  Reviewed joins reassemble them, and joins
form chains -- a single paragraph can span three or more blocks and pages.
Applying a join merges the right-hand block into the left-hand one and removes
it from the flow, so any later join that still names the consumed block must
redirect to the surviving block.  This module is the distilled, tested core of
that algorithm; per-document drivers decide which seams to propose and review.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import re

#: Punctuation that ends a sentence when it is the last non-note character.
_END_RE = re.compile(r"[。！？.!?；;：:…][”’」』）)】〕\s]*$")
#: Trailing note marks (circled numbers or Markdown footnote refs) that do not
#: make a block a sentence boundary on their own.
_TRAILING_NOTE_RE = re.compile(
    r"(?:\s*(?:[①-⑳]|\[\^[^\]]+\]|\\\(\s*\^\{[①-⑳]\}\s*\\\)|\$\s*\^\{[①-⑳]\}\s*\$))+$"
)


def ends_sentence(text: str) -> bool:
    """True when the text closes a sentence before a possible page seam.

    A seam after a sentence end is a paragraph boundary by default; only
    mid-sentence seams are join candidates.
    """
    return _END_RE.search(_TRAILING_NOTE_RE.sub("", text).rstrip()) is not None


@dataclass
class FlowItem:
    """One item of a page flow in reading order."""

    kind: str  # "paragraph" or "figure"
    block_id: str | None = None
    text: str = ""


@dataclass
class PageFlow:
    """The reading-order item list of one page."""

    page: int
    items: list[FlowItem] = field(default_factory=list)

    def paragraphs(self) -> list[FlowItem]:
        return [item for item in self.items if item.kind == "paragraph"]

    def first_paragraph(self) -> FlowItem | None:
        for item in self.items:
            if item.kind == "paragraph":
                return item
        return None

    def last_paragraph(self) -> FlowItem | None:
        for item in reversed(self.items):
            if item.kind == "paragraph":
                return item
        return None

    def paragraph(self, block_id: str) -> FlowItem | None:
        return next(
            (item for item in self.items if item.kind == "paragraph" and item.block_id == block_id),
            None,
        )


@dataclass
class SamePageJoin:
    """A reviewed seam between two blocks on one page (typically around a figure)."""

    page: int
    left: str
    right: str
    separator: str = ""
    evidence: str = ""


@dataclass
class CrossPageJoin:
    """A reviewed seam from the last paragraph of one page into the next page."""

    page: int
    left: str
    right: str
    separator: str = ""
    evidence: str = ""


@dataclass
class JoinRecord:
    """Provenance record for one applied join."""

    type: str  # "same_page_join" or "cross_page_join"
    page: int
    left: str
    right: str
    separator: str
    evidence: str


@dataclass
class JoinIssue:
    """A reviewed join that could not be applied; the blocks stay separate."""

    kind: str
    page: int
    left: str
    right: str


@dataclass
class JoinApplication:
    records: list[JoinRecord] = field(default_factory=list)
    issues: list[JoinIssue] = field(default_factory=list)
    #: consumed block id -> surviving block id
    redirects: dict[str, str] = field(default_factory=dict)


def apply_reviewed_joins(
    flows: list[PageFlow],
    same_page: list[SamePageJoin],
    cross_page: list[CrossPageJoin],
) -> JoinApplication:
    """Apply reviewed joins in page order and return provenance plus issues.

    Same-page seams are applied first, then cross-page seams in page order, so
    the text of a chain ``A | B | C`` is appended to the surviving block in
    source order.  A rule that names a missing or unexpected block is reported
    as an issue and skipped; its blocks keep their paragraph boundary.
    """
    result = JoinApplication()
    by_page = {flow.page: flow for flow in flows}
    # surviving block id -> (owning flow, item), for blocks merged so far
    survivors: dict[str, tuple[PageFlow, FlowItem]] = {}

    for rule in sorted(same_page, key=lambda rule: rule.page):
        flow = by_page.get(rule.page)
        if flow is None:
            result.issues.append(JoinIssue("same_page_join_source_unavailable", rule.page, rule.left, rule.right))
            continue
        left_id = result.redirects.get(rule.left, rule.left)
        left = flow.paragraph(left_id)
        right = flow.paragraph(rule.right)
        if left is None or right is None:
            result.issues.append(JoinIssue("same_page_join_source_unavailable", rule.page, rule.left, rule.right))
            continue
        first, last = flow.items.index(left), flow.items.index(right)
        if first >= last or any(item.kind != "figure" for item in flow.items[first + 1 : last]):
            result.issues.append(JoinIssue("same_page_join_blocks_not_adjacent", rule.page, rule.left, rule.right))
            continue
        left.text += rule.separator + right.text
        flow.items.remove(right)
        result.redirects[rule.right] = left_id
        survivors[left_id] = (flow, left)
        result.records.append(
            JoinRecord("same_page_join", rule.page, left_id, rule.right, rule.separator, rule.evidence)
        )

    for rule in sorted(cross_page, key=lambda rule: rule.page):
        flow = by_page.get(rule.page)
        following = by_page.get(rule.page + 1)
        left_id = result.redirects.get(rule.left, rule.left)
        if left_id in survivors:
            owner, left = survivors[left_id]
        else:
            owner, left = flow, flow.last_paragraph() if flow is not None else None
        right = following.first_paragraph() if following is not None else None
        if (
            flow is None
            or following is None
            or left is None
            or right is None
            or left.block_id != left_id
            or right.block_id != rule.right
        ):
            result.issues.append(JoinIssue("cross_page_join_source_unavailable", rule.page, rule.left, rule.right))
            continue
        left.text += rule.separator + right.text
        following.items.remove(right)
        result.redirects[rule.right] = left_id
        survivors[left_id] = (owner, left)
        result.records.append(
            JoinRecord("cross_page_join", rule.page, left_id, rule.right, rule.separator, rule.evidence)
        )

    return result
