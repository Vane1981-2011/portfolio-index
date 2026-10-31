#!/usr/bin/env python3
"""交付物断言器：直接检查「作品展示」里的 PPTX / PDF 成品，而不是源数据。

verify_numbers.py 验的是「输入数据 → 复算」；本脚本验「复算结论 → 真的印在交付物上」。
两者合起来才闭环：数据对、公式对、成品也对。

设计原则:
1. 不依赖 python-pptx / PyMuPDF：PPTX 用 zipfile 读 XML，PDF 用系统 pdftotext。
   这样在只有标准库的机器上也能跑（换电脑可直接复核）。
2. 断言写的是「口径」而不是「排版」：列名必须叫经营现金流、ROE 不得作为指标列出现。
3. 找不到文件时明确报 FAIL，不静默跳过——交付物缺失本身就是缺陷。

用法:
    python3 scripts/verify_deck.py
    python3 scripts/verify_deck.py --pptx x.pptx --pdf y.pdf
"""

from __future__ import annotations

import argparse
import html
import re
import shutil
import subprocess
import sys
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DECK_DIR = ROOT.parent / "作品展示"
DEFAULT_PPTX = DECK_DIR / "李霞-财务负责人作品展示-20261031.pptx"
DEFAULT_PDF = DECK_DIR / "李霞-作品展示-20261031.pdf"

results: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    results.append((name, bool(ok), detail))


def _clean(s: str) -> str:
    return html.unescape(re.sub(r"<[^>]+>", "", s)).strip()


def slide_xmls(pptx: Path) -> list[tuple[int, str]]:
    with zipfile.ZipFile(pptx) as z:
        names = [n for n in z.namelist() if re.fullmatch(r"ppt/slides/slide\d+\.xml", n)]
        out = []
        for n in names:
            idx = int(re.search(r"slide(\d+)\.xml", n).group(1))
            out.append((idx, z.read(n).decode("utf-8")))
    return sorted(out)


def slide_text(xml: str) -> str:
    return "\n".join(_clean(t) for t in re.findall(r"<a:t>(.*?)</a:t>", xml, re.S))


def has(body: str, kw: str) -> bool:
    """跨行/跨 run 匹配：PPTX 里一个词常被拆成多个 <a:t>，因此忽略换行与空格再比一次。"""
    if kw in body:
        return True
    return kw.replace(" ", "") in body.replace("\n", "").replace(" ", "")


def ordered(body: str, labels: list[str]) -> tuple[bool, str]:
    """按出现位置校验标签顺序（P3 表头是文本框拼的，没有表格对象可解析）。"""
    flat = body.replace("\n", "")
    pos, last = [], -1
    for lb in labels:
        i = flat.find(lb, last + 1)          # 允许同名标签重复出现（如两个「同比」）
        if i < 0:
            return False, f"缺「{lb}」"
        pos.append(i)
        last = i
    return True, "顺序正确"


def pdf_text(pdf: Path) -> tuple[int, str]:
    exe = shutil.which("pdftotext")
    if exe is None:
        return 0, ""
    proc = subprocess.run([exe, "-layout", "-enc", "UTF-8", str(pdf), "-"],
                          capture_output=True, text=True, check=False)
    text = proc.stdout
    pages = text.count("\f") + (0 if text.endswith("\f") else 1) if text else 0
    return pages, text


# ---------------------------------------------------------------- 断言清单
# P3 表头：ROE 整列下线，改为现金流量表披露的经营现金流净额
P3_HEADER = ["公司", "报告期", "营业收入", "同比", "归母净利", "同比", "毛利率", "经营现金流", "负债率"]
# P3 经营现金流列（亿元），与 verify_numbers.py G 组同源
P3_OCF = ["-22.22", "77.19", "401.87", "240.63", "106.70", "80.08", "-284.61"]

REQUIRED = {
    3: ["经营现金流", "1,549.56", "23.32", "不设 ROE 列"],
    5: ["1,260.75", "27.03%", "13.70%", "未年化"],
    6: ["ROA", "17.14%", "1,297.55", "-284.61"],
    7: ["1,055.5", "0.25×2,016", "模型边界"],
    9: ["72,053", "资料库合计", "575", "28", "69"],
}

FORBIDDEN = {
    3: ["2026年第3号", "第 3 号"],
    6: ["ROE 44.26%", "ROE 4.61%"],
    9: ["第 3 号"],
}


def check_pptx(pptx: Path) -> list[tuple[int, str]]:
    if not pptx.exists():
        check("PPTX 存在", False, str(pptx))
        return []
    check("PPTX 存在", True, f"{pptx.name} {pptx.stat().st_size:,} 字节")
    slides = slide_xmls(pptx)
    check("PPTX 页数 = 12", len(slides) == 12, f"{len(slides)} 页")
    texts = {i: slide_text(x) for i, x in slides}

    # P3：表头口径（ROE 整列下线，替换为经营现金流），且 7 家现金流取值到位
    b3 = texts.get(3, "")
    ok, why = ordered(b3, P3_HEADER)
    check("P3 表头 9 列口径与顺序", ok, why)
    check("P3 指标区无独立 ROE 列", "ROE" not in [ln.strip() for ln in b3.split("\n")])
    for v in P3_OCF:
        check(f"P3 含经营现金流 {v}", has(b3, v))

    for page, must in REQUIRED.items():
        body = texts.get(page, "")
        for kw in must:
            check(f"P{page} 含「{kw}」", has(body, kw))
    for page, must_not in FORBIDDEN.items():
        body = texts.get(page, "")
        for kw in must_not:
            check(f"P{page} 不含「{kw}」", not has(body, kw))

    # P3 表注必须给出「为何下线 ROE」的理由，避免读者误以为漏算
    check("P3 表注说明 ROE 下线理由",
          has(texts.get(3, ""), "不做估算") and has(texts.get(3, ""), "不设 ROE 列"))
    return slides


def check_pdf(pdf: Path, slide_count: int) -> None:
    if not pdf.exists():
        check("PDF 存在", False, str(pdf))
        return
    check("PDF 存在", True, f"{pdf.name} {pdf.stat().st_size:,} 字节")
    pages, text = pdf_text(pdf)
    if not text:
        check("PDF 可抽取文本", False, "pdftotext 不可用或输出为空")
        return
    check("PDF 可抽取文本", True, f"{len(text):,} 字符")
    check("PDF 页数与 PPTX 一致", pages == slide_count, f"PDF {pages} 页 vs PPTX {slide_count} 页")
    for kw in ("1,549.56", "23.32", "72,053", "1,055.5", "1,297.55", "17.14%", "27.03%"):
        check(f"PDF 含「{kw}」", kw in text)
    for kw in ("2026年第3号", "第 3 号"):
        check(f"PDF 不含「{kw}」", kw not in text)
    # 交付物里不应残留排版残留物
    for kw in ("{", "}", "None"):
        check(f"PDF 无占位残留「{kw}」", kw not in text)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--pptx", default=str(DEFAULT_PPTX))
    ap.add_argument("--pdf", default=str(DEFAULT_PDF))
    args = ap.parse_args()

    slides = check_pptx(Path(args.pptx))
    check_pdf(Path(args.pdf), len(slides))

    failed = [r for r in results if not r[1]]
    for name, ok, detail in results:
        if not ok:
            print(f"[FAIL] {name}" + (f" —— {detail}" if detail else ""))
    for name, ok, detail in results:
        if ok and detail:
            print(f"[PASS] {name} —— {detail}")
    print(f"\n合计 {len(results)} 项：通过 {len(results) - len(failed)}，失败 {len(failed)}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
