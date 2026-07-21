from __future__ import annotations

from pathlib import Path

import pypdfium2 as pdfium


def render_pages(pdf_path: Path, output_dir: Path, pages: list[int], dpi: int) -> list[Path]:
    output_dir.mkdir(parents=True, exist_ok=True)
    document = pdfium.PdfDocument(pdf_path)
    outputs: list[Path] = []
    scale = dpi / 72
    try:
        for page_number in pages:
            page = document[page_number - 1]
            image = page.render(scale=scale).to_pil()
            output = output_dir / f"page-{page_number:04d}-{dpi}dpi.png"
            image.save(output, format="PNG", optimize=True)
            outputs.append(output)
            page.close()
    finally:
        document.close()
    return outputs
