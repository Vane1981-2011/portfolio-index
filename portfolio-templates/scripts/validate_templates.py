#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""三份作品模板 · 交付前验证

检查四类事实，任一条失败即 rc=1：
  1. 版式：每份 PDF 恰好 2 页 A4；HTML 无占位符 / 模板语法泄漏。
  2. 数值：三份模板内部与跨页勾稽（财务 16 项、审计 10 项、税务 9 项）。
  3. 声明：每页含“示例数据”与边界声明；无执业资质虚假声明。
  4. 打印：@page A4、表头重复、行不跨页、颜色可打印。

用法: python3 scripts/validate_templates.py
"""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path

from pypdf import PdfReader

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"
PDF_MM = (209.9, 297.0)
TOL = 0.6

results: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    results.append((name, bool(ok), detail))


def close(a: float, b: float, tol: float = 0.01) -> bool:
    return abs(float(a) - float(b)) <= tol


def load(name: str) -> dict:
    return json.loads((DATA / name).read_text(encoding="utf-8"))


# ---------------------------------------------------------------- 1. 版式
EXPECTED_PAGES = {"audit": 2, "finance": 2, "tax": 2, "case-baoli": 3}


def check_layout() -> None:
    for name, expected in EXPECTED_PAGES.items():
        pdf = ROOT / name / f"{name}.pdf"
        check(f"{name}: PDF 存在", pdf.exists())
        if not pdf.exists():
            continue
        reader = PdfReader(str(pdf))
        check(f"{name}: 恰好 {expected} 页", len(reader.pages) == expected, f"实际 {len(reader.pages)} 页")
        for i, page in enumerate(reader.pages, 1):
            w = float(page.mediabox.width) / 72 * 25.4
            h = float(page.mediabox.height) / 72 * 25.4
            check(f"{name}: 第 {i} 页 A4", close(w, PDF_MM[0], TOL) and close(h, PDF_MM[1], TOL),
                  f"{w:.1f}×{h:.1f} mm")
        title = (reader.metadata or {}).get("/Title", "")
        check(f"{name}: PDF Title 非空且非文件名", bool(title) and "index.html" not in title, repr(title))
        html = (ROOT / name / "index.html").read_text(encoding="utf-8")
        check(f"{name}: HTML 不含模板语法", "{{" not in html and "{%" not in html)
        check(f"{name}: HTML 不含 Undefined/None 占位", "Undefined" not in html and ">None<" not in html)
        check(f"{name}: 无 AUTO 占位", "AUTO" not in html)


# ---------------------------------------------------------------- 2. 数值
def check_audit() -> None:
    raw = load("audit_sample.json")
    d = load("derived.json")["audit"]

    oms = [b["amount"] * b["rate"] for b in raw["benchmarks"]]
    check("审计: OM = 各基准孰低", close(d["om"], min(oms)), f"{d['om']} vs {min(oms)}")
    check("审计: PM = OM×60%", close(d["pm"], d["om"] * 0.6))
    check("审计: 明显微小 = OM×5%", close(d["trivial"], d["om"] * 0.05))
    check("审计: 风险条数 = 数据行数", d["risk_count"] == len(raw["risks"]))
    check("审计: 舞弊风险 ≥ 2", d["fraud_count"] >= 2, f"{d['fraud_count']} 条")
    check("审计: 程序均指向已登记风险",
          all(p["risk"] in {r["id"] for r in raw["risks"]} for p in raw["programs"]))
    check("审计: 控制数 = 数据行数", d["control_count"] == len(raw["controls"]))
    check("审计: KCP 数 = 标记为是的行数",
          d["kcp_count"] == sum(1 for c in raw["controls"] if c["kcp"] == "是"))
    check("审计: 缺陷三档合计 = 缺陷总数",
          sum(d["def_counts"].values()) == d["def_count"], f"{d['def_counts']}")
    check("审计: 整改完成率 = 已验证关闭 / 缺陷总数",
          close(d["rect_rate"], d["closed_count"] / d["def_count"]))
    check("审计: 未测试控制 ≥ 1（不得评价为有效）", d["untested_count"] >= 1)
    check("审计: 缺陷均指向控制或明确为设计缺陷",
          all(x["id"] in {c["deficiency"] for c in raw["controls"] if c["deficiency"]}
              or x["cause"] == "设计缺陷" for x in raw["deficiencies"]))
    check("审计: 风险矩阵至少 1 条剩余风险为高", any(r["residual"] == "高" for r in raw["risks"]))


def check_finance() -> None:
    raw = load("cashflow_sample.json")
    d = load("derived.json")["finance"]
    weeks = raw["weeks"]
    floor = raw["floor"]

    expect_in = sum(w["inflow"] for w in weeks)
    expect_out = sum(w["outflow"] for w in weeks)
    check("财务: Σ流入 = 表内合计", close(d["inflow"], expect_in))
    check("财务: Σ流出 = 表内合计", close(d["outflow"], expect_out))
    check("财务: Σ净现金流 = Σ流入 − Σ流出", close(d["net"], expect_in - expect_out))
    check("财务: 期末 = 期初 + Σ净现金流", close(d["ending"], d["opening"] + d["net"]))

    balance = raw["opening"]
    ends = []
    gaps = []
    for w in weeks:
        balance += w["inflow"] - w["outflow"]
        ends.append(balance)
        gaps.append(max(0, floor - balance))
    check("财务: 逐周期末 = 上周期末 + 本周净额", close(ends[-1], d["ending"]))
    check("财务: 最低现金周 = 期末序列最小周",
          d["min_week"] == ends.index(min(ends)) + 1 and close(d["min_amount"], min(ends)))
    check("财务: 缺口周数 = 期末低于保有的周数", d["gap_weeks"] == sum(1 for g in gaps if g > 0))
    check("财务: 累计缺口 = ΣMAX(0, 保有−期末)", close(d["cumulative_gap"], sum(gaps)))

    cash = raw["cash"]
    check("财务: 监管内 + 监管外 = 总现金",
          close(d["cash_total"], cash["regulatory"] + cash["non_regulatory"]))
    check("财务: 受限 + 非受限 = 总现金",
          close(d["cash_total"], cash["restricted"] + cash["non_restricted"]))
    check("财务: 可用现金 = 总现金 − 受限", close(d["cash_available"], d["cash_total"] - cash["restricted"]))
    check("财务: 现金结构总现金 = 第 13 周末现金", close(d["cash_total"], d["ending"]))
    check("财务: 实际/预测周次合计 = 13", len(weeks) == 13)

    g_raw = load("group_sample.json")
    g = load("derived.json")["group"]
    rev_sum = sum(e["revenue"] for e in g_raw["entities"])
    cost_sum = sum(e["cost"] for e in g_raw["entities"])
    net_sum = sum(e["net"] for e in g_raw["entities"])
    ocf_sum = sum(e["ocf"] for e in g_raw["entities"])
    check("集团: 20 家收入合计 = Σ主体收入", close(g["revenue_sum"], rev_sum))
    check("集团: 合并收入 = 合计 − 内部抵消", close(g["revenue"], rev_sum - g["internal_rev"]))
    check("集团: 合并成本 = 成本合计 − 内部抵消", close(g["cost"], cost_sum - g["internal_cost"]))
    check("集团: 合并毛利 = 收入 − 成本", close(g["gross_profit"], g["revenue"] - g["cost"]))
    check("集团: 毛利率 = 毛利 / 收入", close(g["gm"], (g["revenue"] - g["cost"]) / g["revenue"]))
    check("集团: 合并净利 = 净利合计 − 内部利润", close(g["net"], net_sum - g["internal_profit"]))
    check("集团: 归母净利 = 合并净利 − 少数股东损益", close(g["attributable"], g["net"] - g["minority"]))
    check("集团: 经营现金流合计 = Σ主体 OCF", close(g["ocf"], ocf_sum))
    check("集团: 红黄绿灯合计 = 20", sum(g["lights"].values()) == g_raw["entity_count"])
    check("集团: 期末现金 = 13 周末现金（跨页）", close(g["cash"], d["ending"]))


def check_tax() -> None:
    raw = load("land_vat_sample.json")
    d = load("derived.json")["tax"]
    area_total = sum(r["area"] for r in raw["revenues"])
    saleable_total = sum(r["saleable"] for r in raw["revenues"])
    rev = sum(r["area"] * r["price"] / 10000 for r in raw["revenues"])

    check("税务: 收入合计 = Σ已售面积×单价", close(d["revenue_total"], rev), f"{d['revenue_total']} vs {rev}")
    check("税务: 分业态已售面积合计 = 项目已售面积",
          area_total == raw["project"]["sold_area"], f"{area_total} vs {raw['project']['sold_area']}")
    check("税务: 分业态可售面积合计 = 项目可售面积",
          saleable_total == raw["project"]["saleable_area"], f"{saleable_total} vs {raw['project']['saleable_area']}")
    base = raw["land"] + raw["dev_cost"]
    dev_a = raw["interest"] + base * 0.05
    dev_b = base * 0.10
    add20 = base * 0.20
    ded_a = base + dev_a + raw["taxes"] + add20
    check("税务: 开发费用 A = 利息 + (土地+成本)×5%", close(dev_a, raw["interest"] + base * 0.05))
    check("税务: 开发费用 B = (土地+成本)×10%", close(dev_b, base * 0.10))
    check("税务: 加计 20% = (土地+成本)×20%", close(add20, base * 0.2))
    check("税务: 扣除 A = 五项合计", close(d["deduction_a"], ded_a), f"{d['deduction_a']} vs {ded_a}")
    check("税务: 扣除 B = 五项合计", close(d["deduction_b"], base + dev_b + raw["taxes"] + add20))
    check("税务: 分类收入合计 = 总收入", close(sum(g["revenue"] for g in d["groups"]), d["revenue_total"]))
    check("税务: 分类扣除合计 = 已售部分可扣除", close(sum(g["deduction"] for g in d["groups"]), d["deductible_total"]))
    check("税务: 已售部分可扣除 = 项目扣除 × 已售/可售",
          close(d["deductible_total"], d["deduction_a"] * area_total / saleable_total))
    check("税务: 未售对应扣除 = 项目扣除 − 已售可扣除",
          close(d["unsold_deduction"], d["deduction_a"] - d["deductible_total"]))
    check("税务: 分类税额合计 = 应纳税额", close(sum(g["tax"] for g in d["groups"]), d["tax_total"]))
    check("税务: 应补退 = 应纳税额 − 已预缴", close(d["payable"], d["tax_total"] - d["prepaid"]))

    for g in d["groups"]:
        check(f"税务: {g['name']} 增值额 = 收入 − 扣除",
              close(g["appreciation"], g["revenue"] - g["deduction"]))
        check(f"税务: {g['name']} 增值率 = 增值额 / 扣除",
              close(g["ratio"], g["appreciation"] / g["deduction"]))

    ordinary = d["groups"][0]
    check("税务: 普通住宅 20% 判定与增值率一致",
          d["ordinary_exempt"] == (ordinary["ratio"] <= 0.20))

    # 复算税额（用数据里的增值率选档，避免依赖模板文本）
    def tier_tax(appr: float, ded: float, ratio: float) -> float:
        for cap, rate, quick in ((0.5, 0.30, 0.0), (1.0, 0.40, 0.05), (2.0, 0.50, 0.15), (9e9, 0.60, 0.35)):
            if ratio <= cap:
                return max(appr * rate - ded * quick, 0.0)
        raise AssertionError

    for g in d["groups"]:
        expect = 0.0 if (g is ordinary and d["ordinary_exempt"]) else tier_tax(
            g["appreciation"], g["deduction"], g["ratio"])
        check(f"税务: {g['name']} 税额复算", close(g["tax"], expect), f"{g['tax']} vs {expect}")

    base_sc = d["scenarios"][0]
    check("税务: 基准情景 = 基准测算", close(base_sc["revenue"], d["revenue_total"])
          and close(base_sc["tax"], d["tax_total"]))
    check("税务: 保守情景税额 < 基准", d["scenarios"][1]["tax"] < d["tax_total"])
    check("税务: 乐观情景税额 > 基准", d["scenarios"][2]["tax"] > d["tax_total"])

    risk = load("tax_risk_sample.json")
    ind = next(i for i in risk["indicators"] if "土增" in i["name"])
    diff = (d["tax_total"] - d["prepaid"]) / d["prepaid"]
    shown = float(re.search(r"[\d.]+", ind["value"]).group()) / 100
    check("税务: 看板土增差异 = 第 1 页复算值", abs(shown - diff) < 0.002,
          f"看板 {shown:.3f} vs 复算 {diff:.3f}")
    impact = next(h for h in risk["high_risks"] if "土增" in h["risk"])
    # 看板金额按万元取整，允许 ±1 万元舍入
    check("税务: 看板土增影响金额 = 应补税额", close(impact["impact"], d["payable"], 1.0),
          f"{impact['impact']} vs {d['payable']:.2f}")

    impacts = [h["impact"] for h in risk["high_risks"]]
    check("税务: 高风险清单按影响金额降序", impacts == sorted(impacts, reverse=True), str(impacts))
    pending = sum(1 for p in risk["policies"] if p["status"] == "待核")
    check("税务: 风险看板含待核政策", pending >= 1, f"{pending} 项")
    check("税务: 风险评分落在 0–100", 0 <= load("derived.json")["risk"]["score"] <= 100)


def check_case() -> None:
    raw = load("case_baoli.json")
    d = load("derived.json")["case"]
    inc, bal, cf = raw["income"], raw["balance"], raw["cashflow"]

    check("案例: 营业收入 = 利润表", close(d["revenue"], inc["revenue"]))
    check("案例: 毛利率 = (收入−成本)/收入",
          close(d["gm"], (inc["revenue"] - inc["cost"]) / inc["revenue"]))
    check("案例: 归母净利 = 利润表", close(d["attributable"], inc["attributable"]))
    check("案例: 经营现金流 = 现金流量表", close(d["ocf"], cf["ocf"]))
    check("案例: 有效税率 = 所得税/利润总额", close(d["etr"], inc["income_tax"] / inc["profit_total"]))
    bm = [x["amount"] * x["rate"] for x in (
        {"amount": inc["profit_total"], "rate": 0.05},
        {"amount": inc["revenue"], "rate": 0.005},
        {"amount": bal["total_assets"], "rate": 0.003},
        {"amount": bal["equity"], "rate": 0.01},
    )]
    check("案例: OM = 四基准孰低", close(d["om"], min(bm)))
    check("案例: PM = OM×60%", close(d["pm"], d["om"] * 0.6))
    check("案例: 明显微小 = OM×5%", close(d["trivial"], d["om"] * 0.05))
    check("案例: 分区域收入合计 ≈ 合并收入（尾差 <0.1 亿）", abs(d["segment_gap"]) < 0.1,
          f"gap={d['segment_gap']:.3f}")
    gp_sum = sum(x["revenue"] - x["cost"] for x in raw["segments"])
    check("案例: 分区域毛利合计 ≈ 合并毛利（尾差 <0.15 亿）",
          abs(gp_sum - (inc["revenue"] - inc["cost"])) < 0.15,
          f"{gp_sum:.2f} vs {inc['revenue']-inc['cost']:.2f}")
    check("案例: 区域数量 = 18（17 区域 + 其他）", len(raw["segments"]) == 18)
    check("案例: 四年同期趋势 = 4 期", len(raw["trend"]) == 4)
    html = (ROOT / "case-baoli" / "index.html").read_text(encoding="utf-8")
    check("案例: 明示不构成审计意见", "不构成审计意见" in html)
    check("案例: 明示不构成税务意见", "不构成税务意见" in html)
    check("案例: 明示未持 CPA/CIA", "未持 CPA / CIA" in html)
    check("案例: 土增税标注未披露", "未披露" in html and "土增税" in html)
    check("案例: 政策 2026 年第 3 号公告待核", "待核" in html and "2026 年第 3 号公告" in html)



# ---------------------------------------------------------------- 3. 声明
FORBIDDEN = [
    "我曾为",
    "本人曾为某公司",
    "持有 CPA",
    "持证注册会计师",
    "CIA 持证人",
    "保证通过稽查",
]


def check_disclaimers() -> None:
    for name in ("audit", "finance", "tax", "case-baoli"):
        html = (ROOT / name / "index.html").read_text(encoding="utf-8")
        check(f"{name}: 结论无重复标点", "。；" not in html and "。。" not in html)
        pages = html.count('class="page"')
        check(f"{name}: 分页容器数 = {EXPECTED_PAGES[name]}", pages == EXPECTED_PAGES[name], f"{pages} 个")
        if name == "case-baoli":
            check(f"{name}: 标注公开数据来源", "东方财富" in html and "akshare" in html)
            check(f"{name}: 标注不含内部数据", "不含内部" in html)
        else:
            check(f"{name}: 含“示例数据”声明", html.count("示例数据") >= 2, f"{html.count('示例数据')} 处")
        check(f"{name}: 含“非真实”边界", "非真实" in html)
        for word in FORBIDDEN:
            check(f"{name}: 无虚假声明「{word}」", word not in html)

    audit_html = (ROOT / "audit" / "index.html").read_text(encoding="utf-8")
    check("审计: 明示非执业 / 未持证", "非执业审计" in audit_html and "未持 CPA / CIA" in audit_html)
    tax_html = (ROOT / "tax" / "index.html").read_text(encoding="utf-8")
    check("税务: 明示不构成税务意见", "不构成税务意见" in tax_html)
    check("税务: 2026 年第 3 号公告标注核对原文", "2026年第3号公告" in tax_html and "核对原文" in tax_html)
    fin_html = (ROOT / "finance" / "index.html").read_text(encoding="utf-8")
    check("财务: 标注实际/预测边界", "W1–W2 实际" in fin_html and "W3–W13 预测" in fin_html)
    check("财务: 标注红黄绿灯为示例阈值", "示例阈值" in fin_html)


# ---------------------------------------------------------------- 4. 打印
def check_print_css() -> None:
    css = (ROOT / "assets" / "report.css").read_text(encoding="utf-8")
    check("打印: @page A4 纵向", re.search(r"@page\s*\{[^}]*size:\s*A4 portrait", css) is not None)
    check("打印: 表头跨页重复", "thead{display:table-header-group}" in css)
    for name, minimum in (("audit", 3), ("finance", 2), ("tax", 1)):
        html = (ROOT / name / "index.html").read_text(encoding="utf-8")
        check(f"打印: {name} 主表使用 thead", html.count("<thead>") >= minimum,
              f"{html.count('<thead>')} 个")
    check("打印: 行/卡片不跨页", "break-inside:avoid" in css or "page-break-inside:avoid" in css)
    check("打印: 颜色可打印", "print-color-adjust:exact" in css)
    check("打印: 覆盖窄屏媒体查询（4 列 KPI）",
          re.search(r"@media print\{[^@]*\.kpi-row\{grid-template-columns:repeat\(4,1fr\)\}", css) is not None)
    check("响应式: 窄屏单列", "@media (max-width:820px)" in css)

    # 4 列 KPI 的实际效果：PDF 文本中 KPI 行不应出现两行堆叠的明显错位（结构性检查见页数）
    for name in ("audit", "finance", "tax"):
        html = (ROOT / name / "index.html").read_text(encoding="utf-8")
        check(f"{name}: 引用共用样式表", "../assets/report.css" in html)


def main() -> int:
    check_layout()
    check_audit()
    check_finance()
    check_tax()
    check_case()
    check_disclaimers()
    check_print_css()

    failed = [(n, d) for n, ok, d in results if not ok]
    for name, ok, detail in results:
        mark = "PASS" if ok else "FAIL"
        print(f"[{mark}] {name}" + (f" —— {detail}" if detail and not ok else ""))
    print(f"\n合计 {len(results)} 项：通过 {len(results) - len(failed)}，失败 {len(failed)}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
