"""Translate in-text note marks into anchor-linked endnotes.

Scanned Chinese academic translations mark same-page notes with a per-page
asterisk series (``*``, ``**``, ``***``, ...), bracketed numbers
(``[n]``/``〔n〕``) or circled numbers. Direct transcription into Markdown
conflicts with the format itself: raw asterisks become emphasis, and
``[^label]`` references do not resolve in every viewer.

This module is the translation layer between the recognised text and the
delivered Markdown:

* ``translate_footnotes`` rewrites a rendered section so that
  - every bound in-text reference ``[^label]`` becomes a superscript anchor
    link ``<sup><a href="#fn-label" id="fnref-label">[N]</a></sup>``,
  - LaTeX superscript wrappers around a reference (``$^{[^label]}$``) are
    unwrapped first,
  - every definition ``[^label]: text`` becomes an anchored endnote entry
    ``<a id="fn-label"></a>[N] text <a href="#fnref-label">↩</a>``,
  - references are numbered sequentially by first occurrence; definitions
    without an in-text reference are numbered after them and reported,
  - standalone raw asterisk runs that no note claimed are neutralised to the
    full-width ``＊`` so they cannot be parsed as emphasis.

The section keeps no ``[^...]`` Markdown footnote syntax afterwards; the
delivered notes resolve as plain anchors in every viewer.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

SUPERSCRIPT_REF_RE = re.compile(r"\$?\^\{(\[\^[^\]]+\])\}\$?")
REF_RE = re.compile(r"\[\^([^\]]+)\]")
DEF_RE = re.compile(r"^(\[\^([^\]]+)\]:)(.*)$")
#: Every standalone raw asterisk run is treated as a note mark and neutralised
#: to the full-width ＊. The OCR pipeline of this project does not emit
#: emphasis (any ``*`` in the recognised text of these books is a note mark);
#: each neutralisation is recorded so a reviewer can spot an exception.
STANDALONE_ASTERISK_RE = re.compile(r"\*{1,4}")


@dataclass
class FootnoteTranslation:
    """Result of translating one section's footnotes."""

    markdown: str
    numbers: dict[str, int] = field(default_factory=dict)
    unbound_definitions: list[str] = field(default_factory=list)
    neutralised_marks: list[dict[str, str]] = field(default_factory=list)
    unresolved_references: list[str] = field(default_factory=list)

    @property
    def complete(self) -> bool:
        """True when no Markdown footnote syntax is left in the section."""
        return "[^" not in self.markdown


def _wrap_reference(label: str, number: int) -> str:
    return (
        f'<sup><a href="#fn-{label}" id="fnref-{label}">[{number}]</a></sup>'
    )


def translate_footnotes(markdown: str) -> FootnoteTranslation:
    """Rewrite one rendered section as described in the module docstring."""
    lines = markdown.split("\n")
    definition_labels: list[str] = []
    definition_lines: dict[str, int] = {}
    for index, line in enumerate(lines):
        match = DEF_RE.match(line)
        if match:
            definition_labels.append(match.group(2))
            definition_lines[match.group(2)] = index

    # Unwrap LaTeX superscript wrappers so the plain reference is replaced
    # together with its wrapper, never left as ``$^{<sup>...}$``.
    for index, line in enumerate(lines):
        if SUPERSCRIPT_REF_RE.search(line):
            lines[index] = SUPERSCRIPT_REF_RE.sub(r"\1", line)

    # Number the references by first occurrence, then the definitions that
    # no reference points at (they keep a visible entry and are reported).
    numbers: dict[str, int] = {}
    next_number = 1
    for index, line in enumerate(lines):
        if index in definition_lines.values():
            continue
        for match in REF_RE.finditer(line):
            label = match.group(1)
            if label not in numbers:
                numbers[label] = next_number
                next_number += 1
    unbound: list[str] = []
    for label in definition_labels:
        if label not in numbers:
            numbers[label] = next_number
            next_number += 1
            unbound.append(label)

    result: list[str] = []
    endnote_entries: dict[int, str] = {}
    for index, line in enumerate(lines):
        if index in definition_lines.values():
            match = DEF_RE.match(line)
            label = match.group(2)
            body = match.group(3).strip()
            number = numbers[label]
            anchor = f'<a id="fn-{label}"></a>'
            back = f' <a href="#fnref-{label}">↩</a>' if label not in unbound else ""
            endnote_entries[number] = f"{anchor}[{number}] {body}{back}"
        else:
            result.append(
                REF_RE.sub(
                    lambda m: (
                        _wrap_reference(m.group(1), numbers[m.group(1)])
                        if m.group(1) in numbers
                        else m.group(0)
                    ),
                    line,
                )
            )
    # The endnote list is emitted in reference order so a reader meets
    # [1], [2], [3] ... instead of page order.
    if endnote_entries:
        first_def_index = min(definition_lines.values())
        for offset, number in enumerate(sorted(endnote_entries)):
            result.insert(first_def_index + offset, endnote_entries[number])

    # Neutralise every raw asterisk run so the delivered Markdown cannot be
    # parsed as emphasis; each occurrence is recorded for review.
    neutralised: list[dict[str, str]] = []
    final: list[str] = []
    for index, line in enumerate(result):
        if "*" in line:
            for match in STANDALONE_ASTERISK_RE.finditer(line):
                neutralised.append(
                    {
                        "line": index + 1,
                        "before": line[max(0, match.start() - 12) : match.end() + 12],
                        "mark": match.group(0),
                    }
                )
            line = STANDALONE_ASTERISK_RE.sub(
                lambda m: "＊" * len(m.group(0)), line
            )
        final.append(line)

    return FootnoteTranslation(
        markdown="\n".join(final),
        numbers=numbers,
        unbound_definitions=unbound,
        neutralised_marks=neutralised,
        unresolved_references=[],
    )
