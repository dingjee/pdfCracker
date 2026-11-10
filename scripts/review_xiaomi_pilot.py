"""Build an offline, side-by-side scan/OCR/layout review artifact.

Usage: uv run scripts/review_xiaomi_pilot.py runs/<id>/pilot/document.json
Layout JSON files may be placed in the pilot's ``layouts/`` directory.
"""
from __future__ import annotations

import argparse
import base64
from collections import Counter
from html import escape
import json
from pathlib import Path


def build_review(document_path: Path) -> Path:
    root = document_path.parent
    document = json.loads(document_path.read_text())
    style_path = root / "style-evidence.json"
    style_checks = json.loads(style_path.read_text()).get("regions", []) if style_path.exists() else []
    sections = []
    colors = {"heading": "#ab4d00", "footnote": "#a13578", "paragraph": "#087b65",
              "header": "#626262", "footer": "#626262", "page_number": "#626262"}
    for page in document["pages"]:
        number = page["page"]
        image_path = root / page["render_path"]
        image_data = base64.b64encode(image_path.read_bytes()).decode()
        layout_path = root / "layouts" / f"page-{number:04d}.json"
        layout = json.loads(layout_path.read_text()) if layout_path.exists() else {}
        boxes = []
        counts = Counter()
        for region in layout.get("regions", []):
            role = region["role"]
            counts[role] += 1
            bbox = region.get("bbox_norm")
            if not isinstance(bbox, list) or len(bbox) != 4:
                continue
            x0, y0, x1, y1 = bbox
            if not all(isinstance(v, (int, float)) and 0 <= v <= 1 for v in bbox):
                continue
            caption = f'{role} · {region.get("confidence", 0):.3f} · {region.get("label", "")}'
            color = colors.get(role, "#495bba")
            boxes.append(f'<div class="box" title="{escape(caption)}" '
                         f'style="left:{x0*100}%;top:{y0*100}%;width:{(x1-x0)*100}%;'
                         f'height:{(y1-y0)*100}%;border-color:{color}"></div>')
        badges = " ".join(f'<span>{escape(role)} {count}</span>' for role, count in counts.items())
        blocks = "".join(
            f'<div class="block"><small>{escape(block["role"])} · '
            f'{block["role_confidence"]:.2f}</small><pre>{escape(block["markdown"])}</pre></div>'
            for block in page.get("structured_blocks", page.get("blocks", []))
        )
        if not blocks:
            blocks = f'<pre>{escape(page.get("partial_text_retained", ""))}</pre>'
        checks = []
        for check in style_checks:
            if check["page"] != number:
                continue
            matched = check["exact_normalized_match_in_page"]
            label = "与整页文字一致" if matched else "区域 / 整页存在差异，待核对"
            checks.append(f'<div class="block"><small>{escape(check["region"]["role"])} · '
                          f'{label}</small><pre>{escape(check["text"])}</pre></div>')
        if checks:
            blocks += '<hr><h3>标题 / 脚注区域复核</h3>' + "".join(checks)
        sections.append(f'''<section id="p{number}"><h2>PDF 第 {number} 页
          <small>{escape(page["status"])}</small></h2><div class="badges">{badges}</div>
          <div class="pair"><div class="scan"><img alt="PDF 第 {number} 页"
          src="data:image/png;base64,{image_data}">{''.join(boxes)}</div>
          <div class="text">{blocks}</div></div></section>''')
    navigation = " ".join(f'<a href="#p{p["page"]}">{p["page"]}</a>' for p in document["pages"])
    html = f'''<!doctype html><html lang="zh-CN"><meta charset="utf-8">
      <title>Xiaomi OCR 样本核对</title><style>
      *{{box-sizing:border-box}}body{{margin:0;background:#f5f3ed;color:#202a2a;
      font:16px/1.65 system-ui,sans-serif}}header,main{{max-width:1450px;margin:auto;padding:24px}}
      header{{position:sticky;top:0;z-index:3;background:#f5f3edf5;border-bottom:1px solid #ccc}}
      h1{{font-size:24px;margin:0}}h2{{font-size:22px}}p{{margin:8px 0}}nav a{{margin-right:12px}}
      section{{scroll-margin-top:200px;margin:32px 0 64px}}.pair{{display:grid;grid-template-columns:1fr 1fr;
      gap:24px;align-items:start}}.scan{{position:relative;background:white}}.scan img{{display:block;width:100%}}
      .box{{position:absolute;border:1.5px solid;opacity:.7}}.box:hover{{background:#ffdb8350;opacity:1}}
      .text{{background:white;padding:20px}}pre{{font:17px/1.9 Georgia,"Songti SC",serif;
      white-space:pre-wrap;overflow-wrap:anywhere;margin:4px 0 22px}}small{{font-size:12px;color:#657274}}
      .badges span{{display:inline-block;background:#e3e8e2;padding:2px 9px;margin:0 5px 10px 0;border-radius:5px}}
      .notice{{color:#8d3d0b}}@media(max-width:800px){{.pair{{grid-template-columns:1fr}}}}
      </style><header><h1>Xiaomi OCR · 试解析核对</h1>
      <p class="notice">待人工验收。左侧为扫描图与检测框，右侧为 OCR Markdown 候选。
      置信度用于定位疑点，不能代表识字准确率；请重点检查漏字、脚注、标题及段落边界。</p>
      <nav>{navigation} · <a href="book.md">原始 OCR</a> ·
      <a href="book.structured.md">结构化候选</a></nav></header><main>{''.join(sections)}</main></html>'''
    target = root / "review.html"
    target.write_text(html, encoding="utf-8")
    return target


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("document", type=Path)
    args = parser.parse_args()
    print(build_review(args.document))
