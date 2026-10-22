#!/usr/bin/env python3
"""测量每份模板两个 .page 在打印媒体下的实际高度（mm），用于定位溢出。

Chrome 打印时内容盒宽度 = 210mm − 2×11mm = 188mm = 710px（96dpi）。
把视口设为 710px 宽并模拟 print 媒体，测出的高度与 PDF 分页近似。
"""
from pathlib import Path
from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parent.parent

with sync_playwright() as p:
    browser = p.chromium.launch(channel="chrome")
    for name in ("audit", "finance", "tax"):
        html = ROOT / name / "index.html"
        page = browser.new_page(viewport={"width": 710, "height": 1047})
        page.goto(html.resolve().as_uri())
        page.emulate_media(media="print")
        heights = page.eval_on_selector_all(
            ".page", "els => els.map(e => e.getBoundingClientRect().height)"
        )
        print(f"[{name}]")
        for i, h in enumerate(heights, 1):
            print(f"  page {i}: {h / 96 * 25.4:.1f} mm  (可用 277.0 mm)")
        page.close()
    browser.close()
