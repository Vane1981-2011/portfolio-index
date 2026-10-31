#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""把落地页导出为 A4 单页 PDF，并把 Chrome 生成的本地 file 链接改成公开 HTTPS 链接。

背景：Chrome `--print-to-pdf` 会把页面里的相对链接保存成 `file:///...` 的
LINK_LAUNCH 注解；文件换一台机器就点不开。本脚本在导出后统一改写为 URI 注解。

用法:
    python3 scripts/export_landing_pdf.py \
        --out "/Users/apple/Desktop/保利发展 2026 三季报分析.pdf"

默认源文件为仓库根的 index.html，公开前缀为
https://vane1981-2011.github.io/portfolio-index/portfolio-templates/
"""
from __future__ import annotations

import argparse
import subprocess
import tempfile
from pathlib import Path

import fitz  # PyMuPDF

ROOT = Path(__file__).resolve().parent.parent
CHROME_CANDIDATES = [
    "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
    "/Applications/Chromium.app/Contents/MacOS/Chromium",
    "/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge",
]
DEFAULT_BASE = "https://vane1981-2011.github.io/portfolio-index/portfolio-templates/"


def chrome_pdf(html: Path, pdf: Path) -> None:
    chrome = next((c for c in CHROME_CANDIDATES if Path(c).exists()), None)
    if not chrome:
        raise SystemExit("未找到 Chrome/Chromium，无法导出 PDF")
    cmd = [
        chrome,
        "--headless=new",
        "--disable-gpu",
        "--no-pdf-header-footer",
        "--virtual-time-budget=3000",
        f"--print-to-pdf={pdf}",
        html.resolve().as_uri(),
    ]
    subprocess.run(cmd, check=True, capture_output=True, text=True, timeout=120)


def local_to_uri(target: str, base: str) -> str:
    """`/…/portfolio-templates/case-baoli/index.html#audit` → 公开 URL。"""
    marker = "portfolio-templates/"
    if marker in target:
        return base + target.split(marker, 1)[1]
    # 兜底：只取文件名，避免把本机绝对路径带进 PDF
    return base + Path(target.split("#", 1)[0]).name


def rewrite_links(pdf: Path, base: str) -> int:
    doc = fitz.open(str(pdf))
    changed = 0
    for page in doc:
        links = page.get_links()
        for link in links:
            if link.get("kind") == fitz.LINK_URI:
                continue
            target = link.get("file") or link.get("uri")
            if not target:
                continue
            uri = local_to_uri(target, base)
            page.delete_link(link)
            page.insert_link({"kind": fitz.LINK_URI, "from": link["from"], "uri": uri})
            changed += 1
    tmp = pdf.with_suffix(".links.pdf")
    doc.save(str(tmp), garbage=4, deflate=True)
    doc.close()
    tmp.replace(pdf)
    return changed


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--html", default=str(ROOT / "index.html"))
    ap.add_argument("--out", required=True)
    ap.add_argument("--base-url", default=DEFAULT_BASE)
    args = ap.parse_args()

    html = Path(args.html).resolve()
    out = Path(args.out).expanduser()
    if not html.exists():
        raise SystemExit(f"源文件不存在: {html}")
    out.parent.mkdir(parents=True, exist_ok=True)

    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td) / "landing.pdf"
        chrome_pdf(html, tmp)
        n = rewrite_links(tmp, args.base_url.rstrip("/") + "/")
        out.write_bytes(tmp.read_bytes())

    doc = fitz.open(str(out))
    links = [l for p in doc for l in p.get_links()]
    doc.close()
    print(f"已导出 {out} · {len(links)} 个链接（改写 {n} 个本地链接）")
    for l in links:
        print(" ", l.get("uri") or l.get("file"), l["from"])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
