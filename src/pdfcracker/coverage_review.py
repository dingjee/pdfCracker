"""Cross-check page OCR against an independent line-level OCR cache.

The main OCR is primary, but an independent line cache (a different local
engine) is cheap evidence against dropped lines.  A line whose normalized text
is contained in the page text is covered; a line that is neither contained nor
similar (measured by bigram overlap) is flagged for review.  A discrepancy is
review evidence, not proof of omission -- the main OCR has inserted correct
text where the independent cache misread it, so every flag goes to a human or
to the page render, never to automatic rewriting.
"""

from __future__ import annotations

from dataclasses import dataclass
import unicodedata


def normalize_text(text: str) -> str:
    """Casefolded alphanumeric characters only, for containment comparison."""
    return "".join(
        character.casefold()
        for character in unicodedata.normalize("NFKC", text)
        if character.isalnum()
    )


def _bigram_overlap(value: str, page_value: str) -> float:
    if len(value) < 2:
        return 1.0 if value in page_value else 0.0
    bigrams = {page_value[i : i + 2] for i in range(len(page_value) - 1)}
    hits = sum(value[i : i + 2] in bigrams for i in range(len(value) - 1))
    return hits / (len(value) - 1)


@dataclass(frozen=True)
class FlaggedLine:
    """An independent line that may be missing from the page OCR."""

    text: str
    bigram_overlap: float


def flag_missing_lines(
    page_text: str,
    lines: list[str] | list[tuple[str, float]],
    *,
    min_characters: int = 16,
    min_confidence: float = 0.65,
    bigram_threshold: float = 0.55,
) -> list[FlaggedLine]:
    """Flag independent lines that the page text may be missing.

    ``lines`` holds either plain line texts or ``(text, confidence)`` pairs.
    Short lines and low-confidence lines are skipped: they are dominated by
    headers, folios and noise, and are not evidence of dropped body text.
    """
    page_value = normalize_text(page_text)
    flagged: list[FlaggedLine] = []
    for entry in lines:
        text, confidence = (entry, 1.0) if isinstance(entry, str) else entry
        value = normalize_text(text)
        if len(value) < min_characters or confidence < min_confidence:
            continue
        if value and value in page_value:
            continue
        overlap = _bigram_overlap(value, page_value)
        if overlap < bigram_threshold:
            flagged.append(FlaggedLine(text, round(overlap, 3)))
    return flagged
