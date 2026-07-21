from __future__ import annotations

import html
import re

from pdfcracker.models import Block, BlockType, Document, FontInfo, Span


def _is_bold(font: FontInfo) -> bool:
    name = font.name.casefold()
    return font.weight >= 600 or any(token in name for token in ("bold", "black", "semibold", "demi"))


def _is_italic(font: FontInfo) -> bool:
    name = font.name.casefold()
    return any(token in name for token in ("italic", "oblique"))


def _style_key(span: Span) -> tuple:
    return (
        span.bold or _is_bold(span.font),
        span.italic or _is_italic(span.font),
        span.underline,
        span.semantic_color,
        span.color_rgb,
    )


def _styled(text: str, span: Span) -> str:
    if not text or not text.strip():
        return text
    leading = text[: len(text) - len(text.lstrip())]
    trailing = text[len(text.rstrip()) :]
    core = text.strip()
    bold = span.bold or _is_bold(span.font)
    italic = span.italic or _is_italic(span.font)
    if span.underline or span.semantic_color:
        core = html.escape(core)
        if span.semantic_color:
            core = f'<span class="text-{span.semantic_color}">{core}</span>'
        if span.underline:
            core = f"<u>{core}</u>"
        if italic:
            core = f"<em>{core}</em>"
        if bold:
            core = f"<strong>{core}</strong>"
    else:
        core = core.replace("\\", "\\\\").replace("*", "\\*").replace("_", "\\_")
        if bold and italic:
            core = f"***{core}***"
        elif bold:
            core = f"**{core}**"
        elif italic:
            core = f"*{core}*"
    return leading + core + trailing


def _render_line(line) -> str:
    runs: list[tuple[Span, str]] = []
    for span in line.spans:
        span_text = span.text.replace("\n", "")
        if not span_text:
            continue
        style = _style_key(span)
        if runs:
            previous = runs[-1][0]
            previous_style = _style_key(previous)
        else:
            previous_style = None
        if runs and previous_style == style:
            previous_span, previous_text = runs[-1]
            runs[-1] = (previous_span, previous_text + span_text)
        else:
            runs.append((span, span_text))
    return "".join(_styled(text, span) for span, text in runs).strip()


def render_inline(block: Block) -> str:
    runs: list[tuple[Span, str]] = []
    for line in block.lines:
        line_spans = [span for span in line.spans if span.text.replace("\n", "")]
        if not line_spans:
            continue
        first_text = line_spans[0].text.replace("\n", "")
        if runs:
            previous_span, previous_text = runs[-1]
            if previous_text.endswith("-") and first_text[:1].islower():
                runs[-1] = (previous_span, previous_text[:-1])
            else:
                runs[-1] = (previous_span, previous_text + " ")
        for span in line_spans:
            span_text = span.text.replace("\n", "")
            style = _style_key(span)
            if runs:
                previous = runs[-1][0]
                previous_style = _style_key(previous)
            else:
                previous_style = None
            if runs and previous_style == style:
                previous_span, previous_text = runs[-1]
                runs[-1] = (previous_span, previous_text + span_text)
            else:
                runs.append((span, span_text))
    collapsed: list[tuple[Span, str]] = []
    index = 0
    while index < len(runs):
        if (
            index + 2 < len(runs)
            and runs[index + 1][1].isspace()
            and _style_key(runs[index][0]) == _style_key(runs[index + 2][0])
        ):
            span, text = runs[index]
            collapsed.append((span, text + runs[index + 1][1] + runs[index + 2][1]))
            index += 3
        else:
            collapsed.append(runs[index])
            index += 1
    return re.sub(
        r"[ \t]+", " ", "".join(_styled(text, span) for span, text in collapsed)
    ).strip()


def _plain_markdown_text(text: str) -> str:
    return text.replace("\\", "\\\\").replace("*", "\\*").replace("_", "\\_").strip()


def render_markdown(document: Document, title: str | None = None, debug: bool = False) -> str:
    merge_targets = {
        decision.to_block: decision
        for decision in document.merges
        if decision.decision == "merge"
    }
    output: list[str] = []
    if title:
        output.extend([f"# {title}", ""])

    last_heading_key: str | None = None
    for page in document.pages:
        if debug:
            output.append(f"<!-- page:{page.number} kind:{page.kind.value} -->")
        for block in page.blocks:
            if block.removed_as:
                if debug:
                    output.append(
                        f"<!-- removed:{block.removed_as} id:{block.id} text:{block.text!r} -->"
                    )
                continue
            text = render_inline(block) or block.text
            if debug:
                output.append(
                    f"<!-- block:{block.id} type:{block.type.value} confidence:{block.confidence:.3f} -->"
                )
            if block.id in merge_targets and output:
                decision = merge_targets[block.id]
                if debug:
                    output.append(
                        f"<!-- merge:{decision.from_block}->{decision.to_block} score:{decision.score:.3f} -->"
                    )
                for index in range(len(output) - 1, -1, -1):
                    if output[index] and not output[index].startswith("<!--"):
                        output[index] = output[index].rstrip() + " " + text.lstrip()
                        break
                continue
            if block.type == BlockType.HEADING:
                heading_key = re.sub(r"\W+", "", block.text.casefold())
                if page.kind.value == "toc" and heading_key == last_heading_key:
                    continue
                level = block.heading_level or 2
                output.extend([f"{'#' * level} {text}", ""])
                last_heading_key = heading_key
            elif block.type == BlockType.TOC_ENTRY:
                for line in block.lines:
                    if line.toc_title:
                        indent = "  " * (line.toc_level or 0)
                        output.append(f"{indent}- {_plain_markdown_text(line.toc_title)}")
            elif block.type == BlockType.QUOTE and block.quote_kind == "line_quotes":
                for line in block.lines:
                    line_text = _render_line(line)
                    if line_text:
                        output.extend([f"> {line_text}", ""])
            elif block.type == BlockType.QUOTE:
                output.extend([f"> {text}", ""])
            elif block.type == BlockType.LIST_ITEM:
                clean = re.sub(r"^(?:[•●▪◦*-]|\(?\d+[.)]|[A-Za-z][.)])\s+", "", text)
                output.extend([f"- {clean}", ""])
            else:
                output.extend([text, ""])

    while output and not output[-1]:
        output.pop()
    return "\n".join(output) + "\n"
