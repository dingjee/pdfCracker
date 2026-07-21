from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

from pdfcracker.images import refresh_image_metadata
from pdfcracker.models import BlockType, Document
from pdfcracker.tables import materialize_table, refresh_table_metadata


class MergeOverride(BaseModel):
    decision: Literal["merge", "review", "keep_separate"]
    reason: str


class BlockOverride(BaseModel):
    type: BlockType | None = None
    heading_level: int | None = Field(default=None, ge=1, le=6)
    reason: str


class ImageOverride(BaseModel):
    role: Literal["content", "decorative", "review"]
    include_in_markdown: bool
    reason: str


class TableOverride(BaseModel):
    include_in_markdown: bool
    column_split: int | None = Field(default=None, ge=1)
    reason: str


class DocumentProfile(BaseModel):
    schema_version: str = "0.1.0"
    source_sha256: str
    merge_overrides: dict[str, MergeOverride] = Field(default_factory=dict)
    block_overrides: dict[str, BlockOverride] = Field(default_factory=dict)
    image_overrides: dict[str, ImageOverride] = Field(default_factory=dict)
    table_overrides: dict[str, TableOverride] = Field(default_factory=dict)


def apply_profile(document: Document, profile: DocumentProfile) -> dict[str, int]:
    if profile.source_sha256 != document.source_sha256:
        raise ValueError("Document profile source_sha256 does not match the input PDF.")

    counts = {"merge": 0, "block": 0, "image": 0, "table": 0}
    decisions = {
        f"{decision.from_block}->{decision.to_block}": decision
        for decision in document.merges
    }
    for key, override in profile.merge_overrides.items():
        decision = decisions.get(key)
        if decision is None:
            raise ValueError(f"Profile merge override not found: {key}")
        decision.decision = override.decision
        decision.reasons.append(f"profile_override:{override.reason}")
        counts["merge"] += 1

    blocks = {block.id: block for page in document.pages for block in page.blocks}
    for key, override in profile.block_overrides.items():
        block = blocks.get(key)
        if block is None:
            raise ValueError(f"Profile block override not found: {key}")
        if override.type is not None:
            block.type = override.type
            if override.type != BlockType.HEADING:
                block.heading_level = None
        if override.heading_level is not None:
            block.type = BlockType.HEADING
            block.heading_level = override.heading_level
        block.evidence.append(f"profile_override:{override.reason}")
        counts["block"] += 1

    images_by_key = {image.id: image for image in document.images}
    for image in document.images:
        images_by_key.setdefault(image.sha256, image)
    for key, override in profile.image_overrides.items():
        targets = [
            image
            for image in document.images
            if image.id == key or image.sha256 == key
        ]
        if not targets:
            raise ValueError(f"Profile image override not found: {key}")
        for image in targets:
            image.role = override.role
            image.include_in_markdown = override.include_in_markdown
            image.confidence = 1.0
            image.evidence.append(f"profile_override:{override.reason}")
            counts["image"] += 1

    tables = {table.id: table for table in document.tables}
    for key, override in profile.table_overrides.items():
        table = tables.get(key)
        if table is None:
            raise ValueError(f"Profile table override not found: {key}")
        if override.include_in_markdown:
            if override.column_split is None:
                raise ValueError(f"Included table requires column_split: {key}")
            materialize_table(document, table, override.column_split)
        else:
            table.role = "layout"
            table.include_in_markdown = False
            table.confidence = 1.0
        table.evidence.append(f"profile_override:{override.reason}")
        counts["table"] += 1

    document.metadata["profile"] = {
        "schema_version": profile.schema_version,
        "applied_overrides": counts,
    }
    refresh_image_metadata(document)
    refresh_table_metadata(document)
    return counts
