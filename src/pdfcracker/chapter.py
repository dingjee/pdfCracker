"""Assemble a reviewed chapter from an OCR structured document.

The chapter is produced only from an explicit, per-document plan. The plan
names the source pages, every text correction and every cross-page merge
decision, so no merge is inferred from layout heuristics and no OCR call is
made. A plan that disagrees with the source is rejected before any artifact is
written.

Artifacts written to ``output_dir``:

``chapter.md``
    Clean Markdown: no page markers, no table of contents. Block content is
    taken from the source Markdown so inline emphasis, links and footnote
    definitions keep their meaning.
``chapter.document.json``
    Provenance sidecar with the original blocks, every transformation and the
    output character range plus source pages of every paragraph.
``chapter.qa.json``
    Quality summary. ``known_body_deletions`` is ``0`` only when the body
    character conservation check passes. ``human_review_required`` stays true
    and ``pilot_approved`` stays false; approval is never inferred.
"""
from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
import re
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

__all__ = [
    "ChapterPlan",
    "ChapterPlanError",
    "PlanEdit",
    "PlanJoin",
    "assemble_chapter",
]

CHAPTER_SCHEMA = "pdfcracker-chapter-document/1"
QA_SCHEMA = "pdfcracker-chapter-qa/1"
BODY_ROLE = "body"
FOOTNOTE_DEFINITION_ROLE = "footnote_definition"
#: Roles that usually carry page furniture or table-of-contents entries. Such
#: blocks are preserved like any other block: a role label is a triage signal,
#: never a deletion decision. The review plan keeps such pages out instead.
FURNITURE_ROLES = frozenset(
    {"toc_entry", "table_of_contents", "page_number", "header", "footer", "separator"}
)
FOOTNOTE_DEFINITION_RE = re.compile(r"^\[\^([^\]\s]+)\]:")
ARTIFACT_NAMES = ("chapter.md", "chapter.document.json", "chapter.qa.json")
EDIT_RECORD_KEYS = (
    "block_id",
    "page",
    "before",
    "after",
    "kind",
    "reason",
    "evidence",
    "review_status",
)


class ChapterPlanError(ValueError):
    """Raised when a plan is invalid or disagrees with the source document."""


class _Reviewed(BaseModel):
    """Review metadata that every plan decision has to carry."""

    model_config = ConfigDict(extra="forbid")

    reason: str = Field(min_length=1)
    evidence: list[str] = Field(default_factory=list, min_length=1)
    review_status: str = ""

    @field_validator("evidence", mode="before")
    @classmethod
    def _normalize_evidence(cls, value: Any) -> Any:
        if value is None:
            return []
        if isinstance(value, str):
            return [value] if value.strip() else []
        return value


class PlanEdit(_Reviewed):
    """A reviewed correction of one block's source Markdown."""

    block_id: str = Field(min_length=1)
    before: str = Field(min_length=1)
    after: str
    kind: str = ""


class PlanJoin(_Reviewed):
    """A reviewed decision about one body boundary across a page break."""

    left: str = Field(min_length=1)
    right: str = Field(min_length=1)
    decision: Literal["merge", "keep_separate"]
    separator: str = " "


class ChapterPlan(BaseModel):
    """The per-document plan that drives one chapter assembly."""

    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    schema_: str = Field(default="", alias="schema")
    source_sha256: str = Field(min_length=1)
    input_document_sha256: str = ""
    title: str = ""
    pages: list[int] = Field(min_length=1)
    printed_pages: list[int] = Field(default_factory=list)
    human_review_required: bool = True
    pilot_approved: bool = False
    metadata: dict[str, Any] = Field(default_factory=dict)
    edits: list[PlanEdit] = Field(default_factory=list)
    joins: list[PlanJoin] = Field(default_factory=list)


@dataclass(frozen=True)
class SourceBlock:
    """One OCR block of the source document."""

    id: str
    page: int
    index: int
    role: str
    content: str
    text: str
    has_markdown: bool
    heading_level: int | None


def _sha256_bytes(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _sha256_file(path: Path) -> str:
    return _sha256_bytes(path.read_bytes())


def _load_json(path: Path, label: str) -> dict[str, Any]:
    if not path.is_file():
        raise ChapterPlanError(f"{label} not found: {path}")
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError) as error:
        raise ChapterPlanError(f"cannot read {label} {path}: {error}") from error
    except json.JSONDecodeError as error:
        raise ChapterPlanError(f"{label} {path} is not valid JSON: {error}") from error
    if not isinstance(payload, dict):
        raise ChapterPlanError(f"{label} {path} must contain a JSON object")
    return payload


def _load_plan(path: Path) -> ChapterPlan:
    try:
        return ChapterPlan.model_validate(_load_json(path, "chapter plan"))
    except ValidationError as error:
        raise ChapterPlanError(f"invalid chapter plan {path}: {error}") from error


def _require_separate_output(
    document_path: Path, plan_path: Path, output_dir: Path
) -> None:
    """Keep generated artifacts away from the source artifacts."""
    resolved = output_dir.resolve()
    for source in (document_path.resolve(), plan_path.resolve()):
        if source.is_relative_to(resolved):
            raise ChapterPlanError(
                f"output directory {resolved} contains the source artifact {source}"
            )
        if resolved == source or resolved in source.parents:
            raise ChapterPlanError(f"output directory {resolved} must not contain {source}")


def _collect_pages(document: dict[str, Any]) -> dict[int, dict[str, Any]]:
    pages: dict[int, dict[str, Any]] = {}
    for record in document.get("pages") or []:
        if not isinstance(record, dict):
            raise ChapterPlanError("document pages must be JSON objects")
        number = record.get("page")
        if not isinstance(number, int) or isinstance(number, bool):
            raise ChapterPlanError("document page record is missing an integer 'page'")
        if number in pages:
            raise ChapterPlanError(f"duplicate page {number} in document")
        pages[number] = record
    return pages


def _collect_blocks(document: dict[str, Any], warnings: list[str]) -> list[SourceBlock]:
    blocks: list[SourceBlock] = []
    seen: set[str] = set()
    for record in document.get("pages") or []:
        page = record["page"]
        raw_blocks = record.get("structured_blocks")
        if raw_blocks is None:
            raw_blocks = record.get("blocks") or []
            if raw_blocks:
                warnings.append(
                    f"page {page} has no 'structured_blocks'; the 'blocks' fallback was used"
                )
        ordered = []
        for position, raw in enumerate(raw_blocks):
            if not isinstance(raw, dict):
                raise ChapterPlanError(f"block on page {page} is not a JSON object")
            block_id = raw.get("id")
            if not isinstance(block_id, str) or not block_id.strip():
                raise ChapterPlanError(f"block on page {page} has no usable id")
            if block_id in seen:
                raise ChapterPlanError(f"duplicate block id {block_id!r} in document")
            seen.add(block_id)
            index = raw.get("index")
            index = index if isinstance(index, int) and not isinstance(index, bool) else position
            ordered.append((index, position, raw))
        for index, _, raw in sorted(ordered, key=lambda item: (item[0], item[1])):
            role = str(raw.get("role") or "unknown")
            text = raw.get("text")
            text = text if isinstance(text, str) else ""
            markdown = raw.get("markdown")
            markdown = markdown if isinstance(markdown, str) else ""
            level = raw.get("heading_level")
            blocks.append(
                SourceBlock(
                    id=raw["id"],
                    page=page,
                    index=index,
                    role=role,
                    content=markdown if markdown else text,
                    text=text,
                    has_markdown=bool(markdown),
                    heading_level=(
                        level if isinstance(level, int) and not isinstance(level, bool) else None
                    ),
                )
            )
    return blocks


def _validate_pages(plan: ChapterPlan, pages: dict[int, dict[str, Any]]) -> list[int]:
    selected = list(plan.pages)
    for number in selected:
        if number < 1:
            raise ChapterPlanError(f"plan page {number} is not a positive page number")
    for previous, current in zip(selected, selected[1:]):
        if current != previous + 1:
            raise ChapterPlanError(
                "plan pages must be contiguous and ascending; "
                f"page {previous} is followed by page {current}"
            )
    for number in selected:
        record = pages.get(number)
        if record is None:
            raise ChapterPlanError(f"plan page {number} is missing from the document")
        status = record.get("status", "ok")
        if status != "ok":
            raise ChapterPlanError(f"plan page {number} did not pass OCR (status {status!r})")
    return selected


def _count_occurrences(haystack: str, needle: str) -> int:
    count = 0
    index = haystack.find(needle)
    while index != -1:
        count += 1
        index = haystack.find(needle, index + 1)
    return count


def _apply_edits(
    plan: ChapterPlan, scope: dict[str, SourceBlock]
) -> tuple[dict[str, str], list[dict[str, Any]]]:
    """Apply the reviewed edits and keep before/after tombstone provenance."""
    grouped: dict[str, list[PlanEdit]] = {}
    for edit in plan.edits:
        if edit.block_id not in scope:
            raise ChapterPlanError(
                f"edit references unknown or out-of-scope block {edit.block_id!r}"
            )
        grouped.setdefault(edit.block_id, []).append(edit)
    edited: dict[str, str] = {}
    records: list[dict[str, Any]] = []
    for block_id, edits in grouped.items():
        original = scope[block_id].content
        spans: list[tuple[int, int, PlanEdit]] = []
        for edit in edits:
            matches = _count_occurrences(original, edit.before)
            if matches != 1:
                raise ChapterPlanError(
                    f"edit for block {block_id!r} must match exactly once but matched "
                    f"{matches} times"
                )
            start = original.index(edit.before)
            spans.append((start, start + len(edit.before), edit))
        ordered_spans = sorted(spans, key=lambda item: (item[0], item[1]))
        for first, second in zip(ordered_spans, ordered_spans[1:]):
            if second[0] < first[1]:
                raise ChapterPlanError(
                    f"edits for block {block_id!r} overlap; each match must be unique"
                )
        content = original
        for start, end, edit in sorted(spans, key=lambda item: item[0], reverse=True):
            content = content[:start] + edit.after + content[end:]
        edited[block_id] = content
        for start, end, edit in spans:
            records.append(
                {
                    "block_id": block_id,
                    "page": scope[block_id].page,
                    "before": edit.before,
                    "after": edit.after,
                    "kind": edit.kind,
                    "reason": edit.reason,
                    "evidence": list(edit.evidence),
                    "review_status": edit.review_status,
                    "original_text": original,
                    "tombstone": {
                        "removed_text": edit.before,
                        "replacement_text": edit.after,
                        "original_span": [start, end],
                        "removed_characters": len(edit.before),
                        "added_characters": len(edit.after),
                    },
                }
            )
    return edited, records


def _validate_joins(
    plan: ChapterPlan, flow: list[SourceBlock], scope: dict[str, SourceBlock]
) -> dict[tuple[str, str], PlanJoin]:
    """Check every join against the body page boundaries that need a decision."""
    position = {block.id: index for index, block in enumerate(flow)}
    required = {
        (left.id, right.id)
        for left, right in zip(flow, flow[1:])
        if left.role == BODY_ROLE
        and right.role == BODY_ROLE
        and right.page == left.page + 1
    }
    decisions: dict[tuple[str, str], PlanJoin] = {}
    for join in plan.joins:
        for block_id in (join.left, join.right):
            if block_id not in scope:
                raise ChapterPlanError(
                    f"join references unknown or out-of-scope block {block_id!r}"
                )
        pair = (join.left, join.right)
        if pair in decisions:
            raise ChapterPlanError(f"duplicate join decision for {pair}")
        left, right = scope[join.left], scope[join.right]
        if FOOTNOTE_DEFINITION_ROLE in (left.role, right.role):
            raise ChapterPlanError(
                f"join {join.left!r}->{join.right!r} touches a footnote definition; "
                "footnote definitions are never merged into the body"
            )
        if join.left not in position or join.right not in position:
            raise ChapterPlanError(
                f"join {join.left!r}->{join.right!r} does not join two selected blocks"
            )
        if position[join.right] != position[join.left] + 1:
            raise ChapterPlanError(
                f"join {join.left!r}->{join.right!r} is not between adjacent blocks; "
                "nonadjacent body blocks must not be merged"
            )
        if left.role != BODY_ROLE or right.role != BODY_ROLE:
            raise ChapterPlanError(
                f"join {join.left!r}->{join.right!r} may only merge body blocks; "
                f"roles are {left.role!r} and {right.role!r}"
            )
        if right.page == left.page:
            raise ChapterPlanError(
                f"join {join.left!r}->{join.right!r} stays on page {left.page}; "
                "merges are only allowed across a page boundary"
            )
        if right.page != left.page + 1:
            raise ChapterPlanError(
                f"join {join.left!r}->{join.right!r} crosses a page gap; merges are "
                "only allowed across a page boundary"
            )
        if pair not in required:
            raise ChapterPlanError(
                f"join {join.left!r}->{join.right!r} is not a reviewed body page boundary"
            )
        decisions[pair] = join
    missing = sorted(pair for pair in required if pair not in decisions)
    if missing:
        described = ", ".join(f"{left}->{right}" for left, right in missing)
        raise ChapterPlanError(
            f"missing reviewed join decision for body page boundary: {described}"
        )
    return decisions


def _build_groups(
    flow: list[SourceBlock], decisions: dict[tuple[str, str], PlanJoin]
) -> list[list[SourceBlock]]:
    groups: list[list[SourceBlock]] = []
    current: list[SourceBlock] = []
    for block in flow:
        if current:
            join = decisions.get((current[-1].id, block.id))
            if join is not None and join.decision == "merge":
                current.append(block)
                continue
        if current:
            groups.append(current)
        current = [block]
    if current:
        groups.append(current)
    return groups


def _render_block(block: SourceBlock, content: str) -> str:
    """Keep source Markdown verbatim; only invent a prefix without Markdown."""
    if block.has_markdown:
        return content
    if block.role == "heading":
        level = max(1, min(6, block.heading_level or 2))
        return f"{'#' * level} {content}".rstrip()
    if block.role == "list_item":
        return f"- {content}".rstrip()
    if block.role == "quote":
        return f"> {content}".rstrip()
    return content


def _plain_heading(content: str) -> str:
    """The heading text without its Markdown markers."""
    return content.lstrip("#").strip()


def _is_h1(content: str) -> bool:
    """True when the source Markdown carries a level one heading."""
    lines = content.lstrip().splitlines()
    return bool(lines) and lines[0].startswith("# ") and not lines[0].startswith("##")


def _component(block: SourceBlock, content: str) -> dict[str, Any]:
    return {
        "block_id": block.id,
        "page": block.page,
        "role": block.role,
        "char_start": 0,
        "char_end": len(content),
        "text": content,
    }


def _render_group(
    group: list[SourceBlock],
    edited: dict[str, str],
    decisions: dict[tuple[str, str], PlanJoin],
) -> tuple[str, list[dict[str, Any]]]:
    """Render one output paragraph and describe its components."""
    if len(group) == 1:
        block = group[0]
        content = edited.get(block.id, block.content)
        return _render_block(block, content), [_component(block, content)]
    parts: list[str] = []
    components: list[dict[str, Any]] = []
    offset = 0
    for index, block in enumerate(group):
        if index:
            separator = decisions[(group[index - 1].id, block.id)].separator
            parts.append(separator)
            offset += len(separator)
        content = edited.get(block.id, block.content)
        components.append(
            {
                "block_id": block.id,
                "page": block.page,
                "role": block.role,
                "char_start": offset,
                "char_end": offset + len(content),
                "text": content,
            }
        )
        parts.append(content)
        offset += len(content)
    return "".join(parts), components


def _conservation_report(
    scope: dict[str, SourceBlock],
    edited: dict[str, str],
    groups: list[list[SourceBlock]],
    rendered: list[str],
    decisions: dict[tuple[str, str], PlanJoin],
    edit_records: list[dict[str, Any]],
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """Check that no body character is lost outside the authorized edits."""
    body = [block for block in scope.values() if block.role == BODY_ROLE]
    original = sum(len(block.content) for block in body)
    after = sum(len(edited.get(block.id, block.content)) for block in body)
    delta = sum(
        len(record["after"]) - len(record["before"])
        for record in edit_records
        if scope[record["block_id"]].role == BODY_ROLE
    )
    output = sum(
        len(text) for group, text in zip(groups, rendered) if group[0].role == BODY_ROLE
    )
    separators = sum(
        len(decisions[(group[index - 1].id, group[index].id)].separator)
        for group in groups
        for index in range(1, len(group))
    )
    used = {block.id for group in groups for block in group}
    missing = [block for block in body if block.id not in used]
    preserved = output - separators
    deletions = max(0, after - preserved) + sum(
        len(edited.get(block.id, block.content)) for block in missing
    )
    checks = [
        {
            "name": "body_edit_delta_is_authorized",
            "status": "passed" if original + delta == after else "failed",
            "detail": (
                f"original {original} characters, authorized edit delta {delta:+d}, "
                f"expected {after} characters"
            ),
        },
        {
            "name": "body_characters_preserved",
            "status": "passed" if preserved == after else "failed",
            "detail": (
                f"{preserved} of {after} body characters in the output, "
                f"{separators} authorized join separator characters"
            ),
        },
        {
            "name": "every_body_block_is_present",
            "status": "passed" if not missing else "failed",
            "detail": (
                "all body blocks of the selected pages appear in the output"
                if not missing
                else f"missing blocks: {', '.join(block.id for block in missing)}"
            ),
        },
    ]
    report = {
        "original_body_characters": original,
        "body_characters_after_authorized_edits": after,
        "body_characters_in_output": preserved,
        "authorized_edit_delta": delta,
        "authorized_join_separator_characters": separators,
        "known_body_deletions": deletions,
        "missing_body_blocks": [block.id for block in missing],
    }
    return report, checks


def _write_artifacts(output_dir: Path, payloads: dict[str, str]) -> None:
    """Write every artifact atomically so a failure leaves no partial output."""
    try:
        output_dir.mkdir(parents=True, exist_ok=True)
        staging = []
        for name, payload in payloads.items():
            path = output_dir / f".{name}.tmp"
            path.write_text(payload, encoding="utf-8")
            staging.append(path)
        for name, path in zip(payloads, staging):
            os.replace(path, output_dir / name)
    except OSError as error:
        for path in output_dir.glob(".*.tmp"):
            path.unlink(missing_ok=True)
        raise ChapterPlanError(f"cannot write chapter artifacts: {error}") from error


def _paragraph_records(
    chunks: list[dict[str, Any]]
) -> tuple[list[dict[str, Any]], str]:
    """Lay the paragraphs out and record their output character ranges."""
    paragraphs: list[dict[str, Any]] = []
    offset = 0
    for index, chunk in enumerate(chunks):
        start = offset
        end = start + len(chunk["text"])
        offset = end + 2
        group = chunk["group"]
        paragraphs.append(
            {
                "index": index,
                "kind": chunk["kind"],
                "text": chunk["text"],
                "output_char_start": start,
                "output_char_end": end,
                "source_pages": sorted({block.page for block in group}) if group else [],
                "source_block_ids": [block.id for block in group] if group else [],
                "components": [
                    {
                        **item,
                        "output_char_start": start + item["char_start"],
                        "output_char_end": start + item["char_end"],
                    }
                    for item in chunk["components"]
                ],
            }
        )
    markdown = "\n\n".join(chunk["text"] for chunk in chunks)
    return paragraphs, markdown + "\n" if markdown else markdown


def assemble_chapter(
    document_path: Path,
    plan_path: Path,
    output_dir: Path,
    *,
    overwrite: bool = False,
) -> dict[str, Any]:
    """Assemble one reviewed chapter from an OCR structured document.

    ``document_path`` points at an existing ``document.structured.json`` with
    ``source.sha256`` and ``pages[].structured_blocks[]``. ``plan_path`` points
    at the reviewed plan for this document. Nothing is merged heuristically and
    no OCR is requested: a body block may only be joined to the next body block
    across a page boundary, and only when the plan records that decision.

    Returns the artifact paths and the QA summary.
    """
    document_path, plan_path, output_dir = (
        Path(document_path),
        Path(plan_path),
        Path(output_dir),
    )
    document = _load_json(document_path, "OCR structured document")
    plan = _load_plan(plan_path)
    _require_separate_output(document_path, plan_path, output_dir)

    source = document.get("source")
    source_sha256 = source.get("sha256") if isinstance(source, dict) else None
    if not isinstance(source_sha256, str) or not source_sha256.strip():
        raise ChapterPlanError("the document is missing source.sha256")
    if plan.source_sha256.strip().casefold() != source_sha256.strip().casefold():
        raise ChapterPlanError(
            f"plan source_sha256 {plan.source_sha256!r} does not match the document "
            f"source.sha256 {source_sha256!r}"
        )
    document_sha256 = _sha256_file(document_path)
    if plan.input_document_sha256 and plan.input_document_sha256.casefold() != document_sha256:
        raise ChapterPlanError(
            f"plan input_document_sha256 {plan.input_document_sha256!r} does not match "
            f"the document {document_sha256!r}"
        )

    warnings: list[str] = []
    pages = _collect_pages(document)
    blocks = _collect_blocks(document, warnings)
    selected = _validate_pages(plan, pages)
    scope = {block.id: block for block in blocks if block.page in set(selected)}
    if not scope:
        raise ChapterPlanError("the selected pages contain no blocks")

    edited, edit_records = _apply_edits(plan, scope)
    flow = sorted(
        (block for block in scope.values() if block.role != FOOTNOTE_DEFINITION_ROLE),
        key=lambda block: (block.page, block.index),
    )
    relocated = sorted(
        (block for block in scope.values() if block.role == FOOTNOTE_DEFINITION_ROLE),
        key=lambda block: (block.page, block.index),
    )
    decisions = _validate_joins(plan, flow, scope)
    groups = _build_groups(flow, decisions)
    rendered = [_render_group(group, edited, decisions) for group in groups]
    texts = [text for text, _ in rendered]
    components = [items for _, items in rendered]

    title = " ".join(plan.title.split())
    title_source = next(
        (
            block
            for block in flow
            if title
            and block.role == "heading"
            and _is_h1(block.content)
            and _plain_heading(block.content) == title
        ),
        None,
    )
    chunks: list[dict[str, Any]] = []
    if title and title_source is None:
        chunks.append({"kind": "title", "text": f"# {title}", "group": None, "components": []})
    for group, text, items in zip(groups, texts, components):
        if not text.strip():
            continue
        if title_source is not None and len(group) == 1 and group[0] is title_source:
            kind = "title"
        else:
            kind = "body" if len(group) > 1 else group[0].role
        chunks.append({"kind": kind, "text": text, "group": group, "components": items})
    for block in relocated:
        content = edited.get(block.id, block.content)
        note = _render_block(block, content)
        if not note.strip():
            continue
        chunks.append(
            {
                "kind": FOOTNOTE_DEFINITION_ROLE,
                "text": note,
                "group": [block],
                "components": [_component(block, content)],
            }
        )
    paragraphs, markdown = _paragraph_records(chunks)

    transformations: list[dict[str, Any]] = [
        {
            "type": "text_edit",
            **{key: record[key] for key in EDIT_RECORD_KEYS},
            "tombstone": record["tombstone"],
        }
        for record in edit_records
    ]
    transformations.extend(
        {
            "type": "cross_page_merge" if join.decision == "merge" else "keep_separate",
            "left": join.left,
            "right": join.right,
            "from_page": scope[join.left].page,
            "to_page": scope[join.right].page,
            "separator": join.separator,
            "reason": join.reason,
            "evidence": list(join.evidence),
            "review_status": join.review_status,
        }
        for join in decisions.values()
    )
    transformations.extend(
        {
            "type": "footnote_relocation",
            "block_id": block.id,
            "from_page": block.page,
            "to": "chapter_end",
            "reason": "footnote definitions are moved to the chapter end",
            "evidence": ["role:footnote_definition", "never_merged_into_body"],
        }
        for block in relocated
    )
    transformations.extend(
        {
            "type": "title_source_retained",
            "block_id": block.id,
            "page": block.page,
            "reason": "the source heading renders the chapter title",
            "evidence": [f"title:{title}"],
        }
        for block in [title_source]
        if block is not None
    )
    furniture = [block for block in scope.values() if block.role in FURNITURE_ROLES]
    if furniture:
        warnings.append(
            f"{len(furniture)} selected block(s) carry a page furniture or table-of-contents "
            "role; they are preserved verbatim, so keep such pages out of the plan"
        )

    body_stream = "".join(
        edited.get(block.id, block.content)
        for block in scope.values()
        if block.role == BODY_ROLE
    )
    footnotes = [
        {
            "block_id": block.id,
            "page": block.page,
            "label": (FOOTNOTE_DEFINITION_RE.match(block.content.strip()) or [None, ""])[1],
            "reference_count": 0,
            "paired": False,
        }
        for block in relocated
    ]
    for note in footnotes:
        note["reference_count"] = body_stream.count(f"[^{note['label']}]")
        note["paired"] = bool(note["label"]) and note["reference_count"] > 0
    unmatched = [note["label"] or note["block_id"] for note in footnotes if not note["paired"]]
    if unmatched:
        warnings.append(
            "footnote definitions without a matching body reference: " + ", ".join(unmatched)
        )

    report, checks = _conservation_report(
        scope, edited, groups, texts, decisions, edit_records
    )
    for paragraph in paragraphs:
        if markdown[paragraph["output_char_start"] : paragraph["output_char_end"]] != paragraph["text"]:
            raise ChapterPlanError(
                f"paragraph {paragraph['index']} does not match the assembled Markdown"
            )
    checks.append(
        {
            "name": "paragraph_ranges_match_markdown",
            "status": "passed",
            "detail": f"{len(paragraphs)} output paragraphs verified against chapter.md",
        }
    )
    unrendered = [
        block.id
        for block in relocated
        if edited.get(block.id, block.content) not in markdown
    ]
    checks.append(
        {
            "name": "footnote_definitions_rendered",
            "status": "passed" if not unrendered else "failed",
            "detail": (
                f"{len(relocated)} relocated footnote definitions are present in chapter.md"
                if not unrendered
                else f"footnote definitions missing from chapter.md: {', '.join(unrendered)}"
            ),
        }
    )
    failed = [check["name"] for check in checks if check["status"] != "passed"]
    if failed:
        raise ChapterPlanError(
            "chapter quality checks failed, no artifacts were written: " + ", ".join(failed)
        )

    blocks_out = []
    for block in scope.values():
        final_content = edited.get(block.id, block.content)
        heading_match = re.match(r"^(#{1,6})[ \t]+", _render_block(block, final_content))
        final_heading_level = (
            len(heading_match.group(1)) if heading_match else block.heading_level
        ) if block.role == "heading" else None
        if block.role == FOOTNOTE_DEFINITION_ROLE:
            disposition = "relocated_to_chapter_end"
        else:
            disposition = "in_flow"
        blocks_out.append(
            {
                "id": block.id,
                "page": block.page,
                "index": block.index,
                "role": block.role,
                "disposition": disposition,
                "original_text": block.content,
                "final_text": final_content,
                "original_heading_level": block.heading_level,
                "heading_level": final_heading_level,
            }
        )

    plan_sha256 = _sha256_file(plan_path)
    qa = {
        "schema": QA_SCHEMA,
        "source_sha256": source_sha256,
        "input_document_sha256": document_sha256,
        "plan_sha256": plan_sha256,
        "pages": selected,
        "printed_pages": list(plan.printed_pages),
        "title": title,
        "known_body_deletions": report["known_body_deletions"],
        "conservation_check": (
            "passed" if all(check["status"] == "passed" for check in checks) else "failed"
        ),
        "human_review_required": True,
        "pilot_approved": False,
        "review_status": "pending",
        "plan_declared_review": {
            "human_review_required": plan.human_review_required,
            "pilot_approved": plan.pilot_approved,
        },
        "body_characters": {
            "original": report["original_body_characters"],
            "after_authorized_edits": report["body_characters_after_authorized_edits"],
            "in_output": report["body_characters_in_output"],
            "authorized_edit_delta": report["authorized_edit_delta"],
            "authorized_join_separator_characters": report["authorized_join_separator_characters"],
        },
        "counts": {
            "paragraphs": len(paragraphs),
            "blocks_in_scope": len(scope),
            "body_blocks": sum(1 for block in scope.values() if block.role == BODY_ROLE),
            "edits": len(plan.edits),
            "joins": len(plan.joins),
            "merges": sum(1 for join in decisions.values() if join.decision == "merge"),
            "keep_separate": sum(
                1 for join in decisions.values() if join.decision == "keep_separate"
            ),
            "relocated_footnotes": len(relocated),
            "preserved_furniture_blocks": len(furniture),
        },
        "checks": checks,
        "footnotes": footnotes,
        "warnings": warnings,
        "notes": [
            "known_body_deletions is 0 only because the conservation check passed.",
            "human_review_required stays true and pilot_approved stays false; "
            "a human reviewer must approve the pilot.",
        ],
    }

    document_payload = {
        "schema": CHAPTER_SCHEMA,
        "source": {"path": str(document_path.resolve()), "sha256": source_sha256},
        "plan": {
            "path": str(plan_path.resolve()),
            "sha256": plan_sha256,
            "schema": plan.schema_,
            "title": title,
            "pages": selected,
            "printed_pages": list(plan.printed_pages),
            "metadata": plan.metadata,
        },
        "chapter": {
            "title": title,
            "markdown_path": "chapter.md",
            "paragraph_count": len(paragraphs),
            "character_count": len(markdown),
        },
        "paragraphs": paragraphs,
        "blocks": blocks_out,
        "edits": edit_records,
        "joins": [
            {
                "left": join.left,
                "right": join.right,
                "decision": join.decision,
                "separator": join.separator,
                "reason": join.reason,
                "evidence": list(join.evidence),
                "review_status": join.review_status,
            }
            for join in plan.joins
        ],
        "transformations": transformations,
        "footnotes": footnotes,
    }

    existing = [name for name in ARTIFACT_NAMES if (output_dir / name).exists()]
    if existing and not overwrite:
        raise ChapterPlanError(
            "chapter artifacts already exist and were preserved: "
            + ", ".join(existing)
            + "; pass overwrite=True to replace them deliberately"
        )
    _write_artifacts(
        output_dir,
        {
            "chapter.md": markdown,
            "chapter.document.json": json.dumps(document_payload, ensure_ascii=False, indent=2)
            + "\n",
            "chapter.qa.json": json.dumps(qa, ensure_ascii=False, indent=2) + "\n",
        },
    )
    return {
        "output_dir": str(output_dir.resolve()),
        "chapter_markdown_path": str((output_dir / "chapter.md").resolve()),
        "chapter_document_path": str((output_dir / "chapter.document.json").resolve()),
        "chapter_qa_path": str((output_dir / "chapter.qa.json").resolve()),
        "qa": qa,
    }
