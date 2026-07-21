from __future__ import annotations

import json
import math
from pathlib import Path


AS_OF = "2026-07-21"
WIDTH = 816
HEIGHT = 1056
OUTPUT_TOKENS = 200


def cost(tokens: int, usd_per_million: float) -> float:
    return tokens * usd_per_million / 1_000_000


models = [
    {
        "model": "gemini-2.5-flash-lite",
        "setting": "medium",
        "image_tokens": 256,
        "input_usd_per_million": 0.10,
        "output_usd_per_million": 0.40,
        "source": "https://ai.google.dev/gemini-api/docs/pricing",
        "token_source": "https://ai.google.dev/gemini-api/docs/media-resolution",
    },
    {
        "model": "gemini-3.1-flash-lite",
        "setting": "medium",
        "image_tokens": 560,
        "input_usd_per_million": 0.25,
        "output_usd_per_million": 1.50,
        "source": "https://ai.google.dev/gemini-api/docs/pricing",
        "token_source": "https://ai.google.dev/gemini-api/docs/media-resolution",
    },
    {
        "model": "gpt-5.6-luna",
        "setting": "original",
        "image_tokens": math.ceil(WIDTH / 32) * math.ceil(HEIGHT / 32),
        "input_usd_per_million": 1.00,
        "output_usd_per_million": 6.00,
        "source": "https://developers.openai.com/api/docs/models",
        "token_source": "https://developers.openai.com/api/docs/guides/images-vision",
    },
    {
        "model": "claude-haiku-4.5",
        "setting": "standard-resolution",
        "image_tokens": math.ceil(WIDTH / 28) * math.ceil(HEIGHT / 28),
        "input_usd_per_million": 1.00,
        "output_usd_per_million": 5.00,
        "source": "https://platform.claude.com/docs/en/about-claude/models/overview",
        "token_source": "https://platform.claude.com/docs/en/build-with-claude/vision",
    },
    {
        "model": "gemini-3.5-flash",
        "setting": "medium",
        "image_tokens": 560,
        "input_usd_per_million": 1.50,
        "output_usd_per_million": 9.00,
        "source": "https://ai.google.dev/gemini-api/docs/pricing",
        "token_source": "https://ai.google.dev/gemini-api/docs/media-resolution",
    },
    {
        "model": "gpt-5.6-terra",
        "setting": "original",
        "image_tokens": math.ceil(WIDTH / 32) * math.ceil(HEIGHT / 32),
        "input_usd_per_million": 2.50,
        "output_usd_per_million": 15.00,
        "source": "https://developers.openai.com/api/docs/models",
        "token_source": "https://developers.openai.com/api/docs/guides/images-vision",
    },
]

for row in models:
    row["estimated_image_input_usd"] = round(
        cost(row["image_tokens"], row["input_usd_per_million"]), 9
    )
    row["estimated_output_usd"] = round(
        cost(OUTPUT_TOKENS, row["output_usd_per_million"]), 9
    )
    row["estimated_total_usd"] = round(
        row["estimated_image_input_usd"] + row["estimated_output_usd"], 9
    )

result = {
    "as_of": AS_OF,
    "assumptions": {
        "image_width_px": WIDTH,
        "image_height_px": HEIGHT,
        "output_tokens": OUTPUT_TOKENS,
        "excluded": [
            "prompt/system text tokens",
            "reasoning tokens",
            "cache effects",
            "batch/flex discounts",
            "retries",
            "taxes",
        ],
    },
    "models": sorted(models, key=lambda row: row["estimated_total_usd"]),
}

output = Path(__file__).parents[1] / "benchmarks" / "vision_cost_estimates.json"
output.parent.mkdir(parents=True, exist_ok=True)
output.write_text(json.dumps(result, indent=2), encoding="utf-8")
print(json.dumps(result, indent=2))
