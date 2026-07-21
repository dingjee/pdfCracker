from __future__ import annotations

import hashlib
import re
from collections import Counter, defaultdict, deque
from pathlib import Path
from typing import Any

import pdfplumber
from pypdf import PdfReader

from pdfcracker.models import BBox, Block, BlockType, Document, EmbeddedImage, Page


CAPTION_PREFIX_RE = re.compile(
    r"^(?:fig(?:ure)?|photo|image|illustration|source)\s*[.:#\-]?\s*\d*",
    re.IGNORECASE,
)


def _object_key(name: str) -> str:
    return Path(name.lstrip("/")).stem.casefold()


def _bbox_from_plumber(image: dict[str, Any]) -> BBox:
    return BBox(
        x0=float(image["x0"]),
        y0=float(image["top"]),
        x1=float(image["x1"]),
        y1=float(image["bottom"]),
    )


def _classify_image(
    image: dict[str, Any],
    bbox: BBox,
    page: Page,
    pixel_width: int,
    pixel_height: int,
    decoded_image: Any,
) -> tuple[str, bool, float, list[str]]:
    display_width = max(0.0, bbox.x1 - bbox.x0)
    display_height = max(0.0, bbox.y1 - bbox.y0)
    area_ratio = (display_width * display_height) / max(1.0, page.width * page.height)
    aspect_ratio = max(
        display_width / max(display_height, 0.001),
        display_height / max(display_width, 0.001),
    )
    evidence = [
        f"display_area_ratio={area_ratio:.5f}",
        f"display_aspect_ratio={aspect_ratio:.3f}",
        f"pixels={pixel_width}x{pixel_height}",
    ]
    entropy = float(decoded_image.entropy())
    evidence.append(f"pixel_entropy={entropy:.3f}")
    if "A" in decoded_image.getbands():
        alpha = decoded_image.getchannel("A")
        alpha_min, alpha_max = alpha.getextrema()
        histogram = alpha.histogram()
        nonopaque_fraction = sum(histogram[:-1]) / max(1, sum(histogram))
        evidence.extend(
            [
                f"alpha_range={alpha_min}-{alpha_max}",
                f"nonopaque_fraction={nonopaque_fraction:.5f}",
            ]
        )
        if alpha_max == 0:
            return "decorative", False, 0.99, [*evidence, "fully_transparent_layer"]
        if nonopaque_fraction == 1.0 and alpha_max < 240 and entropy < 6.0:
            return "decorative", False, 0.95, [
                *evidence,
                "translucent_low_information_layer",
            ]
    if image.get("imagemask"):
        return "decorative", False, 0.98, [*evidence, "pdf_image_mask"]
    if pixel_width < 32 or pixel_height < 32 or area_ratio < 0.002:
        return "decorative", False, 0.96, [*evidence, "too_small_for_content_image"]
    if aspect_ratio >= 5.0:
        return "decorative", False, 0.90, [*evidence, "extreme_aspect_ratio"]
    if area_ratio >= 0.02 and aspect_ratio <= 4.0:
        return "content", True, 0.86, [*evidence, "substantial_page_area"]
    return "review", False, 0.55, [*evidence, "ambiguous_embedded_image"]


def _horizontal_overlap(first: BBox, second: BBox) -> float:
    overlap = max(0.0, min(first.x1, second.x1) - max(first.x0, second.x0))
    denominator = max(1.0, min(first.x1 - first.x0, second.x1 - second.x0))
    return overlap / denominator


def _caption_candidate(image: EmbeddedImage, page: Page, body_font_size: float) -> Block | None:
    scored: list[tuple[float, Block]] = []
    image_center = (image.bbox.x0 + image.bbox.x1) / 2
    for block in page.blocks:
        if block.removed_as or block.type not in {BlockType.PARAGRAPH, BlockType.CAPTION}:
            continue
        text = block.text.strip()
        if not text or len(text) > 240:
            continue
        distance = block.bbox.y0 - image.bbox.y1
        if not 0 <= distance <= 48:
            continue
        overlap = _horizontal_overlap(image.bbox, block.bbox)
        if overlap < 0.35:
            continue
        block_center = (block.bbox.x0 + block.bbox.x1) / 2
        font_size = block.primary_font.size
        score = 0.30 + min(0.25, overlap * 0.25)
        reasons = 0
        if CAPTION_PREFIX_RE.match(text):
            score += 0.25
            reasons += 1
        if 0 < font_size <= body_font_size * 0.92:
            score += 0.15
            reasons += 1
        if abs(block_center - image_center) <= page.width * 0.08:
            score += 0.10
            reasons += 1
        if reasons and score >= 0.65:
            scored.append((score, block))
    return max(scored, key=lambda item: item[0])[1] if scored else None


def _assign_anchor(image: EmbeddedImage, page: Page, body_font_size: float) -> None:
    caption = _caption_candidate(image, page, body_font_size)
    if caption is not None:
        caption.type = BlockType.CAPTION
        caption.confidence = max(caption.confidence, 0.90)
        caption.evidence.append(f"caption_for={image.id}")
        image.caption_block_id = caption.id
        image.caption_text = caption.text
        image.anchor_before_block_id = caption.id
        image.evidence.append("anchored_before_caption")
        return

    active = [block for block in page.blocks if not block.removed_as]
    following = [block for block in active if block.bbox.y0 >= image.bbox.y1]
    if following:
        target = min(following, key=lambda block: block.bbox.y0 - image.bbox.y1)
        image.anchor_before_block_id = target.id
        image.evidence.append("anchored_before_nearest_following_block")
        return
    preceding = [block for block in active if block.bbox.y1 <= image.bbox.y0]
    if preceding:
        target = min(preceding, key=lambda block: image.bbox.y0 - block.bbox.y1)
        image.anchor_after_block_id = target.id
        image.evidence.append("anchored_after_nearest_preceding_block")
    else:
        image.evidence.append("no_text_anchor_on_page")


def extract_embedded_images(
    pdf_path: Path,
    document: Document,
    asset_dir: Path,
    asset_prefix: Path = Path("assets/images"),
) -> list[EmbeddedImage]:
    """Extract PDF image objects only; never rasterize a page or region as fallback."""
    asset_dir.mkdir(parents=True, exist_ok=True)
    reader = PdfReader(str(pdf_path))
    page_models = {page.number: page for page in document.pages}
    images: list[EmbeddedImage] = []
    unmatched: list[dict[str, Any]] = []

    with pdfplumber.open(pdf_path) as plumber:
        for page_number in document.selected_pages:
            model_page = page_models[page_number]
            plumber_page = plumber.pages[page_number - 1]
            decoded_by_name: dict[str, deque[Any]] = defaultdict(deque)
            for decoded in reader.pages[page_number - 1].images:
                decoded_by_name[_object_key(decoded.name)].append(decoded)

            for index, source_image in enumerate(plumber_page.images, start=1):
                object_name = str(source_image.get("name", f"image-{index}"))
                candidates = decoded_by_name[_object_key(object_name)]
                if not candidates:
                    unmatched.append(
                        {"page": page_number, "object_name": object_name, "reason": "decode_not_found"}
                    )
                    continue
                decoded = candidates.popleft()
                data = decoded.data
                digest = hashlib.sha256(data).hexdigest()
                suffix = Path(decoded.name).suffix.casefold() or ".bin"
                asset_name = f"{digest[:20]}{suffix}"
                output = asset_dir / asset_name
                if not output.exists():
                    output.write_bytes(data)
                bbox = _bbox_from_plumber(source_image)
                pixel_width, pixel_height = decoded.image.size
                role, include, confidence, evidence = _classify_image(
                    source_image,
                    bbox,
                    model_page,
                    int(pixel_width),
                    int(pixel_height),
                    decoded.image,
                )
                image = EmbeddedImage(
                    id=f"p{page_number:04d}-img{index:03d}",
                    page=page_number,
                    object_name=object_name,
                    asset_path=(asset_prefix / asset_name).as_posix(),
                    sha256=digest,
                    bbox=bbox,
                    bbox_norm=bbox.normalized(model_page.width, model_page.height),
                    pixel_width=int(pixel_width),
                    pixel_height=int(pixel_height),
                    format=(decoded.image.format or suffix.lstrip(".")).casefold(),
                    role=role,
                    include_in_markdown=include,
                    confidence=confidence,
                    evidence=evidence,
                )
                _assign_anchor(image, model_page, document.body_font_size)
                images.append(image)

    repeated = Counter(image.sha256 for image in images)
    for image in images:
        if repeated[image.sha256] >= 3 and image.role != "content":
            image.role = "decorative"
            image.include_in_markdown = False
            image.confidence = max(image.confidence, 0.90)
            image.evidence.append(f"same_image_repeated_{repeated[image.sha256]}_times")

    document.images = images
    document.metadata["image_extraction"] = {
        "policy": "embedded_objects_only_no_raster_fallback",
        "unmatched": unmatched,
    }
    refresh_image_metadata(document)
    return images


def refresh_image_metadata(document: Document) -> None:
    metadata = document.metadata.setdefault(
        "image_extraction",
        {"policy": "embedded_objects_only_no_raster_fallback", "unmatched": []},
    )
    metadata.update(
        {
            "objects": len(document.images),
            "unique_assets": len({image.sha256 for image in document.images}),
            "included_in_markdown": sum(
                image.include_in_markdown for image in document.images
            ),
            "decorative": sum(image.role == "decorative" for image in document.images),
            "review": sum(image.role == "review" for image in document.images),
        }
    )
