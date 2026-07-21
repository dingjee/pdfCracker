# Vision benchmark retrospective — 2026-07-21

## What failed

1. Two OpenAI requests returned 404 before inference because provider display names were used where OpenRouter expected canonical slugs, and an unsupported `temperature` parameter was required.
2. The first TOC schema used `page_number`, which models reasonably interpreted as the destination numbers inside TOC entries rather than standalone page furniture.
3. One Gemini 2.5 response was truncated/malformed; the first implementation discarded its response metadata while handling the JSON error.
4. Models reported readability/style confidence of 4/4 while still missing a thin underline.

## Corrections

- Resolve and pin canonical provider slugs (`google-ai-studio`, `openai`, `anthropic`), disable fallback, and require only parameters supported by the selected endpoint.
- Rename the field to `standalone_page_number` and define TOC destination numbers as out of scope.
- Return raw content, generation ID, and usage even when JSON parsing fails.
- Use field-level human gold scoring as the acceptance signal; keep self-reported confidence as telemetry only.
- Pin `seed=0` for endpoints that support it.

## Durable consequence

The final two rules were added to `AGENTS.md`; provider and schema details remain implementation/tests rather than durable agent prose.
