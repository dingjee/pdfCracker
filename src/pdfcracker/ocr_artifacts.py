"""Screen OCR text for raw markup that would leak into Markdown or EPUB.

Local OCR models sometimes emit pseudo-markup for layout they recognize --
``<fcel>`` table cells, ``<nl>`` line breaks -- instead of plain text.  Parsers
and converters pass such fragments through as raw HTML, which produced invalid
XHTML and crashed a full-book EPUB packaging step.  Every OCR page should be
screened before assembly: any tag-like fragment is review evidence, and the
affected text is corrected against independent evidence (never regex-deleted
in bulk).
"""

from __future__ import annotations

from dataclasses import dataclass
import re

#: A tag-like fragment: an opening or closing angle bracket immediately
#: followed by a tag name.  It matches complete tags (``<fcel>``, ``</b>``,
#: ``<img alt="x">``) and truncated ones (``<fc``), but not comparisons such
#: as ``a < b`` or ``<3``: real markup never has whitespace after ``<``.
_MARKUP_TAG_RE = re.compile(r"</?[A-Za-z][A-Za-z0-9-]*(?:[^<>\n]*>)?")


@dataclass(frozen=True)
class MarkupArtifact:
    """A tag-like fragment found in OCR text."""

    text: str
    start: int
    end: int

    @property
    def tag(self) -> str:
        """The tag name, e.g. ``fcel`` for ``<fcel>``."""
        inner = self.text.lstrip("</ ").rstrip(">")
        return inner.split()[0] if inner.split() else self.text


def find_markup_artifacts(text: str) -> list[MarkupArtifact]:
    """Every tag-like fragment in ``text``, in order of appearance."""
    return [
        MarkupArtifact(match.group(0), match.start(), match.end())
        for match in _MARKUP_TAG_RE.finditer(text)
    ]


def has_markup_artifacts(text: str) -> bool:
    """True when ``text`` contains any tag-like fragment."""
    return _MARKUP_TAG_RE.search(text) is not None
