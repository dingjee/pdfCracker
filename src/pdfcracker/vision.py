from __future__ import annotations

import base64
import json
import os
import time
from pathlib import Path
from typing import Any

import httpx
from dotenv import load_dotenv
from PIL import Image


FEATURE_KEYS = (
    "heading",
    "red_text",
    "image",
    "bold_text",
    "italic_text",
    "underlined_text",
    "boxed_callout",
    "multi_region_flow",
    "running_header",
    "running_footer",
    "standalone_page_number",
)

PAGE_AUDIT_SCHEMA: dict[str, Any] = {
    "name": "pdf_page_visual_audit",
    "strict": True,
    "schema": {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "page_type": {
                "type": "string",
                "enum": [
                    "cover",
                    "toc",
                    "chapter_opening",
                    "body",
                    "exercise",
                    "reference",
                    "unknown",
                ],
            },
            "small_body_text_readability": {"type": "integer", "minimum": 0, "maximum": 4},
            "style_discrimination": {"type": "integer", "minimum": 0, "maximum": 4},
            "features": {
                "type": "object",
                "additionalProperties": False,
                "properties": {key: {"type": "boolean"} for key in FEATURE_KEYS},
                "required": list(FEATURE_KEYS),
            },
            "reading_order": {
                "type": "array",
                "maxItems": 8,
                "items": {"type": "string", "maxLength": 100},
            },
            "issues": {
                "type": "array",
                "maxItems": 5,
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "properties": {
                        "type": {"type": "string", "maxLength": 50},
                        "severity": {"type": "integer", "minimum": 1, "maximum": 3},
                        "evidence": {"type": "string", "maxLength": 160},
                    },
                    "required": ["type", "severity", "evidence"],
                },
            },
            "confidence": {"type": "number", "minimum": 0, "maximum": 1},
        },
        "required": [
            "page_type",
            "small_body_text_readability",
            "style_discrimination",
            "features",
            "reading_order",
            "issues",
            "confidence",
        ],
    },
}


AUDIT_PROMPT = """You are auditing one rendered PDF page for a deterministic text-extraction pipeline.
Do not transcribe the page. Inspect only what is visibly present. Return the required JSON.

Readability scale: 0 unreadable, 1 major text unreadable, 2 usable with uncertainty,
3 clearly readable, 4 crisp. Style discrimination uses the same scale for distinguishing
bold, italic, underline, and semantic color.

Definitions:
- multi_region_flow: text wraps around an image or uses multiple independently ordered regions.
- boxed_callout: a visibly bounded or shaded text callout.
- running_header/footer: repeated book furniture, not a chapter heading or body continuation.
- standalone_page_number: a page-number element standing alone in a margin. Page numbers printed
  inside TOC entries do not count.
- issues: only visual ambiguities likely to affect extraction; use an empty array if none.
- reading_order: short region labels, not copied prose.
"""


def _image_data_url(path: Path) -> tuple[str, int, int]:
    mime = "image/png" if path.suffix.casefold() == ".png" else "image/jpeg"
    encoded = base64.b64encode(path.read_bytes()).decode("ascii")
    with Image.open(path) as image:
        width, height = image.size
    return f"data:{mime};base64,{encoded}", width, height


def _provider_for_model(model: str) -> str | None:
    if model.startswith("google/"):
        return "google-ai-studio"
    if model.startswith("openai/"):
        return "openai"
    if model.startswith("anthropic/"):
        return "anthropic"
    return None


def call_page_audit(image_path: Path, model: str, timeout_seconds: float = 120) -> dict[str, Any]:
    load_dotenv(".env")
    api_key = os.environ.get("OPENROUTER_API_KEY")
    if not api_key:
        raise RuntimeError("OPENROUTER_API_KEY is missing")
    data_url, width, height = _image_data_url(image_path)
    provider = _provider_for_model(model)
    payload: dict[str, Any] = {
        "model": model,
        "messages": [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": AUDIT_PROMPT},
                    {"type": "image_url", "image_url": {"url": data_url}},
                ],
            }
        ],
        "seed": 0,
        "max_tokens": 650,
        "response_format": {"type": "json_schema", "json_schema": PAGE_AUDIT_SCHEMA},
        "provider": {
            "allow_fallbacks": False,
            "require_parameters": True,
        },
    }
    if provider:
        payload["provider"]["order"] = [provider]

    started = time.perf_counter()
    response = httpx.post(
        "https://openrouter.ai/api/v1/chat/completions",
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
            "HTTP-Referer": "https://github.com/local/pdfcracker",
            "X-Title": "PDFCracker visual benchmark",
        },
        json=payload,
        timeout=timeout_seconds,
    )
    elapsed = time.perf_counter() - started
    if not response.is_success:
        raise RuntimeError(f"OpenRouter HTTP {response.status_code}: {response.text[:600]}")
    body = response.json()
    content = body["choices"][0]["message"]["content"]
    result = {
        "image": str(image_path),
        "width": width,
        "height": height,
        "model_requested": model,
        "model_returned": body.get("model"),
        "provider_returned": body.get("provider"),
        "generation_id": body.get("id"),
        "elapsed_seconds": round(elapsed, 3),
        "usage": body.get("usage", {}),
    }
    try:
        result["audit"] = json.loads(content)
    except json.JSONDecodeError as error:
        result["error"] = f"JSONDecodeError: {error}"
        result["raw_content"] = content[:4000]
    return result


def score_audit(audit: dict[str, Any], gold: dict[str, Any]) -> dict[str, Any]:
    expected_features = gold["features"]
    actual_features = audit["features"]
    feature_matches = {
        key: actual_features.get(key) == expected
        for key, expected in expected_features.items()
    }
    return {
        "page_type_match": audit.get("page_type") == gold["page_type"],
        "feature_matches": feature_matches,
        "feature_accuracy": round(sum(feature_matches.values()) / len(feature_matches), 4),
    }


def run_benchmark(
    images: list[Path],
    models: list[str],
    output_path: Path,
    gold: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    rows: list[dict[str, Any]] = []
    for image_path in images:
        for model in models:
            try:
                row = call_page_audit(image_path, model)
                if gold and "audit" in row:
                    row["score"] = score_audit(row["audit"], gold)
            except Exception as error:  # preserve partial benchmark evidence
                row = {
                    "image": str(image_path),
                    "model_requested": model,
                    "error": f"{type(error).__name__}: {error}",
                }
            rows.append(row)
            output_path.write_text(json.dumps(rows, indent=2), encoding="utf-8")
    return rows
