from __future__ import annotations

import re
from itertools import zip_longest

import pdfplumber

from pdfcracker.models import BBox, Document, NativeTable


BULLET_RE = re.compile(r"^[\uf0b7•●▪◦]\s*")


def _consolidate_parallel_bullets(rows: list[list[str]]) -> list[list[str]]:
    if len(rows) < 3:
        return rows
    bullet_count = sum(bool(BULLET_RE.match(cell)) for row in rows[1:] for cell in row)
    if bullet_count < 3:
        return rows

    columns: list[list[str]] = []
    for column in range(2):
        items: list[str] = []
        for row in rows[1:]:
            text = row[column].strip()
            if not text:
                continue
            if BULLET_RE.match(text):
                items.append(BULLET_RE.sub("", text).strip())
            elif items:
                separator = "" if items[-1].endswith("-") else " "
                items[-1] = f"{items[-1]}{separator}{text}".strip()
            else:
                items.append(text)
        columns.append(items)
    paired = [
        [left or "", right or ""]
        for left, right in zip_longest(columns[0], columns[1])
    ]
    return [rows[0], *paired]


def extract_native_table_candidates(pdf_path, document: Document) -> list[NativeTable]:
    page_models = {page.number: page for page in document.pages}
    tables: list[NativeTable] = []
    with pdfplumber.open(pdf_path) as pdf:
        for page_number in document.selected_pages:
            page = pdf.pages[page_number - 1]
            model_page = page_models[page_number]
            for index, found in enumerate(page.find_tables(), start=1):
                data = found.extract()
                row_count = len(data)
                column_count = max((len(row) for row in data), default=0)
                raw_rows = [
                    [str(cell or "").strip() for cell in [*row, *([""] * (column_count - len(row)))]]
                    for row in data
                ]
                nonempty = sum(bool(cell) for row in raw_rows for cell in row)
                if row_count < 2 or column_count < 2 or nonempty < 4:
                    continue
                bbox = BBox(
                    x0=float(found.bbox[0]),
                    y0=float(found.bbox[1]),
                    x1=float(found.bbox[2]),
                    y1=float(found.bbox[3]),
                )
                tables.append(
                    NativeTable(
                        id=f"p{page_number:04d}-table{index:03d}",
                        page=page_number,
                        bbox=bbox,
                        bbox_norm=bbox.normalized(model_page.width, model_page.height),
                        raw_rows=raw_rows,
                        source_column_count=column_count,
                        confidence=0.55,
                        evidence=[
                            "pdf_vector_cell_topology",
                            f"shape={row_count}x{column_count}",
                            f"nonempty_cells={nonempty}",
                        ],
                    )
                )
    document.tables = tables
    refresh_table_metadata(document)
    return tables


def materialize_table(document: Document, table: NativeTable, column_split: int) -> None:
    if not 1 <= column_split < table.source_column_count:
        raise ValueError(f"Invalid column split for {table.id}: {column_split}")
    rows: list[list[str]] = []
    for raw in table.raw_rows:
        left = "\n".join(cell for cell in raw[:column_split] if cell).strip()
        right = "\n".join(cell for cell in raw[column_split:] if cell).strip()
        if left or right:
            rows.append([left, right])
    if not rows:
        raise ValueError(f"Table override produced no cells: {table.id}")
    table.rows = _consolidate_parallel_bullets(rows)
    table.role = "table"
    table.include_in_markdown = True
    table.confidence = 1.0
    table.evidence.append(f"profile_column_split={column_split}")

    page = next(page for page in document.pages if page.number == table.page)
    overlapping = []
    for block in page.blocks:
        center_x = (block.bbox.x0 + block.bbox.x1) / 2
        center_y = (block.bbox.y0 + block.bbox.y1) / 2
        if table.bbox.x0 <= center_x <= table.bbox.x1 and table.bbox.y0 <= center_y <= table.bbox.y1:
            overlapping.append(block)
    overlapping.sort(key=lambda block: (block.bbox.y0, block.bbox.x0))
    if overlapping:
        table.anchor_before_block_id = overlapping[0].id
        for block in overlapping:
            block.removed_as = "table_text"
            block.evidence.append(f"represented_by={table.id}")
    else:
        following = [block for block in page.blocks if block.bbox.y0 >= table.bbox.y1]
        if following:
            table.anchor_before_block_id = min(following, key=lambda block: block.bbox.y0).id


def refresh_table_metadata(document: Document) -> None:
    document.metadata["table_extraction"] = {
        "policy": "native_pdf_cell_topology_only",
        "candidates": len(document.tables),
        "included_in_markdown": sum(table.include_in_markdown for table in document.tables),
        "layout_not_table": sum(table.role == "layout" for table in document.tables),
        "review": sum(table.role == "review" for table in document.tables),
    }
