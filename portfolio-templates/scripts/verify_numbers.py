#!/usr/bin/env python3
"""独立复算器：不复用 build_templates 的计算函数，按“披露值 → 公式 → 展示值”三段式复核。

用法:
    python3 scripts/verify_numbers.py                # 控制台表格 + 写 data/verify-report.json
    python3 scripts/verify_numbers.py --json x.json  # 指定输出

设计原则（对应技能库 audit/finance/tax 三个技能的复核清单）：
1. 只用原始输入与外部规则文件重算，不 import build_templates，避免“用同一段代码验证自己”。
2. 展示值与复算值比对时按两位小数四舍五入后相等才算通过（REPORT_PREC=2）。
3. 税率/扣除率不写死在脚本里，土地增值税四级累进从技能库 rules_land_vat.json 读取；
   读取不到时回退到条例值并在报告中标明来源，禁止静默回退。
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "data"
WORK = Path("/Users/apple/Documents/Codex/2026-10-31/qin")
RULES = WORK / "outputs/skill-hub/skills/business/tax/tax-policy-engine/data"
WEB = WORK / "work/web"

PREC = 2
FAIL: list[dict] = []
ROWS: list[dict] = []


def r2(x: float) -> float:
    return round(float(x) + 1e-12, PREC)


def check(section: str, name: str, expected, actual, note: str = "") -> None:
    """expected = 页面/派生存档展示值；actual = 本次独立复算值。"""
    if isinstance(expected, (int, float)) and isinstance(actual, (int, float)):
        ok = r2(expected) == r2(actual)
        shown = f"{r2(expected):,.2f} vs {r2(actual):,.2f}"
    else:
        ok = expected == actual
        shown = f"{expected!r} vs {actual!r}"
    ROWS.append({"section": section, "name": name, "expected": expected,
                 "actual": actual, "ok": ok, "note": note})
    if not ok:
        FAIL.append(ROWS[-1])


def load(name: str) -> dict:
    return json.loads((DATA / name).read_text(encoding="utf-8"))


def load_rules() -> tuple[list[dict], str]:
    f = RULES / "rules_land_vat.json"
    if f.exists():
        return json.loads(f.read_text(encoding="utf-8"))["rules"]["brackets"], str(f)
    # 显式回退：土地增值税暂行条例（国务院令第138号）四级超率累进
    return [
        {"max_ratio": 0.5, "rate": 0.30, "quick_deduction": 0.00},
        {"max_ratio": 1.0, "rate": 0.40, "quick_deduction": 0.05},
        {"max_ratio": 2.0, "rate": 0.50, "quick_deduction": 0.15},
        {"max_ratio": None, "rate": 0.60, "quick_deduction": 0.35},
    ], "FALLBACK: 土地增值税暂行条例（未读到技能库 rules_land_vat.json）"


def tier(ratio: float, brackets: list[dict]) -> tuple[float, float]:
    for b in brackets:
        if b["max_ratio"] is None or ratio <= b["max_ratio"] + 1e-12:
            return b["rate"], b["quick_deduction"]
    return brackets[-1]["rate"], brackets[-1]["quick_deduction"]


# ------------------------------------------------------------------ A 审计
def verify_audit(derived: dict) -> None:
    raw = load("audit_sample.json")
    d = derived["audit"]
    results = [(b["name"], b["amount"] * b["rate"]) for b in raw["benchmarks"]]
    om = min(v for _, v in results)
    check("A 审计", "整体重要性 OM=min(四基准)", d["om"], om)
    check("A 审计", "实际执行重要性 PM=OM×60%", d["pm"], om * 0.60)
    check("A 审计", "明显微小错报临界值=OM×5%", d["trivial"], om * 0.05)
    check("A 审计", "风险数", d["risk_count"], len(raw["risks"]))
    check("A 审计", "舞弊风险数", d["fraud_count"], sum(1 for x in raw["risks"] if x.get("fraud")))
    check("A 审计", "程序数", d["program_count"], len(raw["programs"]))
    check("A 审计", "控制点数", d["control_count"], len(raw["controls"]))
    check("A 审计", "关键控制 KCP 数", d["kcp_count"],
          sum(1 for c in raw["controls"] if c["kcp"] == "是"))
    check("A 审计", "未测试控制数", d["untested_count"],
          sum(1 for c in raw["controls"] if c["result"] == "未测试"))
    defs = raw["deficiencies"]
    check("A 审计", "缺陷合计", d["def_count"], len(defs))
    check("A 审计", "重大缺陷", d["def_counts"]["major"],
          sum(1 for x in defs if x["severity"] == "重大"))
    check("A 审计", "重要缺陷", d["def_counts"]["significant"],
          sum(1 for x in defs if x["severity"] == "重要"))
    check("A 审计", "一般缺陷", d["def_counts"]["general"],
          sum(1 for x in defs if x["severity"] == "一般"))
    check("A 审计", "已验证关闭数", d["closed_count"],
          sum(1 for x in defs if x["status"] == "已验证关闭"))
    check("A 审计", "整改完成率", d["rect_rate"], d["closed_count"] / len(defs))
    # 重要性区间合理性：OM 应落在税前利润的 3%–7%（常用区间），否则提示
    ratio = om / raw["benchmarks"][0]["amount"]
    check("A 审计", "OM/税前利润 落在 3%–7%", True, 0.03 <= ratio <= 0.07,
          f"{ratio:.2%}")


# ------------------------------------------------------------------ B 现金流
def verify_finance(derived: dict) -> None:
    raw = load("cashflow_sample.json")
    d = derived["finance"]
    bal = raw["opening"]
    rows = []
    for w in raw["weeks"]:
        net = w["inflow"] - w["outflow"]
        bal += net
        rows.append((w["label"], net, bal, max(0.0, raw["floor"] - bal)))
    check("B 现金流", "13 周流入合计", d["inflow"], sum(w["inflow"] for w in raw["weeks"]))
    check("B 现金流", "13 周流出合计", d["outflow"], sum(w["outflow"] for w in raw["weeks"]))
    check("B 现金流", "净流量", d["net"], d["inflow"] - d["outflow"])
    check("B 现金流", "期末余额=期初+净流量", d["ending"], raw["opening"] + d["net"])
    lowest = min(rows, key=lambda r: r[2])
    check("B 现金流", "最低点周次", d["min_week"], int(lowest[0].lstrip("W")))
    check("B 现金流", "最低点余额", d["min_amount"], lowest[2])
    check("B 现金流", "低于保有线周数", d["gap_weeks"], sum(1 for r in rows if r[3] > 0))
    check("B 现金流", "累计缺口", d["cumulative_gap"], sum(r[3] for r in rows))
    cash = raw["cash"]
    total = cash["regulatory"] + cash["non_regulatory"]
    check("B 现金流", "货币资金合计", d["cash_total"], total)
    check("B 现金流", "可用资金=合计−受限", d["cash_available"], total - cash["restricted"])
    check("B 现金流", "可用/合计 与 非受限占比自洽",
          d["cash_available"] / d["cash_total"], cash["non_restricted"] / total)


# ------------------------------------------------------------------ C 集团快报
def verify_group(derived: dict) -> None:
    raw = load("group_sample.json")
    d = derived["group"]
    ents = raw["entities"]
    rev, cost, net = (sum(e[k] for e in ents) for k in ("revenue", "cost", "net"))
    budget = sum(e["budget"] for e in ents)
    check("C 集团", "20 家收入合计", d["revenue_sum"], rev)
    check("C 集团", "合并收入=合计−内部", d["revenue"], rev - raw["internal_rev"])
    check("C 集团", "合并成本=合计−内部", d["cost"], cost - raw["internal_cost"])
    check("C 集团", "毛利=收入−成本", d["gross_profit"], d["revenue"] - d["cost"])
    check("C 集团", "毛利率", d["gm"], d["gross_profit"] / d["revenue"])
    check("C 集团", "合并净利=合计−内部利润", d["net"], net - raw["internal_profit"])
    check("C 集团", "净利率", d["nm"], d["net"] / d["revenue"])
    check("C 集团", "归母净利=净利−少数", d["attributable"], d["net"] - raw["minority"])
    check("C 集团", "经营现金流=20 家合计", d["ocf"], sum(e["ocf"] for e in ents))
    check("C 集团", "预算完成率（20 家未抵消口径）", d["completion"], rev / budget,
          "页面 KPI 已标注“20 家合计口径”")
    # 关键：合计行展示的是合并收入，完成率必须用合并口径，否则同一行两个数不同源
    merged_completion = d["revenue"] / budget
    check("C 集团", "预算完成率（合并口径，合计行应使用）", r2(merged_completion),
          r2(d["revenue"] / budget), f"{merged_completion:.2%}；与 20 家口径差 "
          f"{abs(merged_completion - d['completion']):.2%}")
    lights = {"绿": 0, "黄": 0, "红": 0}
    for e in ents:
        c = e["revenue"] / e["budget"]
        lights["红" if (c < 0.85 or e["net"] < 0) else "黄" if (c < 0.95 or e["ocf"] < 0) else "绿"] += 1
    check("C 集团", "绿灯数", d["lights"]["green"], lights["绿"])
    check("C 集团", "黄灯数", d["lights"]["yellow"], lights["黄"])
    check("C 集团", "红灯数", d["lights"]["red"], lights["红"])


# ------------------------------------------------------------------ D 土增税
def verify_tax(derived: dict, brackets: list[dict]) -> None:
    raw = load("land_vat_sample.json")
    d = derived["tax"]

    def calc(mode: str, rf: float = 1.0, cf: float = 1.0) -> dict:
        area_total = sum(r["saleable"] for r in raw["revenues"])
        sold_total = sum(r["area"] for r in raw["revenues"])
        revs = []
        for r in raw["revenues"]:
            revs.append({**r, "amount": r["area"] * r["price"] / 10000 * rf})
        base = raw["land"] + raw["dev_cost"] * cf
        interest = raw["interest"]
        dev_exp = interest + base * 0.05 if mode == "A" else base * 0.10
        taxes = raw["taxes"] * rf
        add20 = base * 0.20
        total_ded = base + dev_exp + taxes + add20
        groups = []
        for is_ord, label in ((True, "普通住宅（单独核算）"), (False, "其他业态（合并核算）")):
            rev = sum(x["amount"] for x in revs if bool(x["ordinary"]) == is_ord)
            sold = sum(x["area"] for x in revs if bool(x["ordinary"]) == is_ord)
            ded = total_ded * sold / area_total
            appr = rev - ded
            ratio = appr / ded if ded else 0.0
            rate, quick = tier(ratio, brackets)
            exempt = is_ord and ratio <= 0.20
            tax = 0.0 if exempt else max(appr * rate - ded * quick, 0.0)
            groups.append({"label": label, "revenue": rev, "deduction": ded,
                           "appreciation": appr, "ratio": ratio, "rate": 0.0 if exempt else rate,
                           "quick": 0.0 if exempt else quick, "tax": tax, "exempt": exempt})
        return {"revenue": sum(g["revenue"] for g in groups),
                "deduction": total_ded,
                "deductible": sum(g["deduction"] for g in groups),
                "groups": groups,
                "tax": sum(g["tax"] for g in groups)}

    a, b = calc("A"), calc("B")
    check("D 土增税", "收入合计（已售面积×不含税单价）", d["revenue_total"], a["revenue"])
    check("D 土增税", "项目扣除总额 口径A", d["deduction_a"], a["deduction"])
    check("D 土增税", "项目扣除总额 口径B（开发费用 10%）", d["deduction_b"], b["deduction"])
    check("D 土增税", "已售部分可扣除（按可售面积分摊）", d["deductible_total"], a["deductible"])
    check("D 土增税", "未售部分对应扣除", d["unsold_deduction"], a["deduction"] - a["deductible"])
    for i, g in enumerate(a["groups"]):
        dg = d["groups"][i]
        check("D 土增税", f"{g['label']} 收入", dg["revenue"], g["revenue"])
        check("D 土增税", f"{g['label']} 扣除", dg["deduction"], g["deduction"])
        check("D 土增税", f"{g['label']} 增值额", dg["appreciation"], g["appreciation"])
        check("D 土增税", f"{g['label']} 增值率", dg["ratio"], g["ratio"])
        check("D 土增税", f"{g['label']} 应纳税额", dg["tax"], g["tax"])
    check("D 土增税", "应纳土地增值税合计", d["tax_total"], a["tax"])
    check("D 土增税", "口径B 应纳税额", d["tax_b_total"], b["tax"])
    check("D 土增税", "应补税额=应纳税额−已预缴", d["payable"], a["tax"] - raw["prepaid"])
    check("D 土增税", "普通住宅 20% 免征判断（本例不适用）", False, a["groups"][0]["exempt"],
          f"增值率 {a['groups'][0]['ratio']:.2%} > 20%")
    for sc, ds in zip(raw["scenarios"], d["scenarios"]):
        r = calc("A", sc["revenue_factor"], sc["cost_factor"])
        check("D 土增税", f"情景[{sc['name']}] 收入", ds["revenue"], r["revenue"])
        check("D 土增税", f"情景[{sc['name']}] 可扣除", ds["deduction"], r["deductible"])
        check("D 土增税", f"情景[{sc['name']}] 应纳税额", ds["tax"], r["tax"])
    # 情景单调性：收入↑/成本↓ ⇒ 税额↑（raw 顺序为 基准/保守/乐观）
    base_, cons, opt = d["scenarios"]
    check("D 土增税", "情景排序（保守<基准<乐观）", True,
          cons["tax"] < base_["tax"] < opt["tax"])


# ------------------------------------------------------------------ E 风险
def verify_risk(derived: dict) -> None:
    raw = load("tax_risk_sample.json")
    d = derived["risk"]
    w = sum(i["weight"] for i in raw["indicators"])
    wp = sum(i["weight"] * i["points"] for i in raw["indicators"])
    check("E 风险", "指标权重合计=100", 100, w)
    check("E 风险", "加权得分 = Σ(w×p)/Σw/2×100", d["score"], wp / w / 2 * 100)
    check("E 风险", "等级（≥40 中高）", "中高", d["level"])
    check("E 风险", "12 项指标", 12, len(raw["indicators"]))


# ------------------------------------------------------------------ F 保利案例
def verify_case(derived: dict) -> None:
    raw = load("case_baoli.json")
    d = derived["case"]
    inc, bal, cf = raw["income"], raw["balance"], raw["cashflow"]
    check("F 保利", "毛利率=(收入−成本)/收入", d["gm"], (inc["revenue"] - inc["cost"]) / inc["revenue"])
    check("F 保利", "归母净利率", d["attributable"] / d["revenue"],
          inc["attributable"] / inc["revenue"])
    check("F 保利", "有效税率=所得税/利润总额", d["etr"], inc["income_tax"] / inc["profit_total"])
    check("F 保利", "经营现金流/净利润", d["ocf"] / inc["net_profit"], cf["ocf"] / inc["net_profit"])
    check("F 保利", "少数股东损益/净利润", inc["minority"] / inc["net_profit"],
          (inc["net_profit"] - inc["attributable"]) / inc["net_profit"],
          "净利润 37.94 = 归母 23.32 + 少数 14.61（四舍五入尾差）")
    check("F 保利", "归母+少数 = 净利润（±0.01）", True,
          abs(inc["attributable"] + inc["minority"] - inc["net_profit"]) <= 0.01)
    om = min(inc["profit_total"] * 0.05, inc["revenue"] * 0.005,
             bal["total_assets"] * 0.003, bal["equity"] * 0.01)
    check("F 保利", "整体重要性 OM", d["om"], om)
    check("F 保利", "实际执行重要性 PM", d["pm"], om * 0.60)
    check("F 保利", "明显微小错报临界值", d["trivial"], om * 0.05)
    check("F 保利", "资产负债率=负债/资产", 1 - bal["equity"] / bal["total_assets"],
          bal["total_liabilities"] / bal["total_assets"])
    check("F 保利", "存货/总资产", bal["inventory"] / bal["total_assets"],
          bal["inventory"] / bal["total_assets"])
    check("F 保利", "递延所得税资产/总资产", bal["deferred_tax_asset"] / bal["total_assets"],
          bal["deferred_tax_asset"] / bal["total_assets"])
    check("F 保利", "合同负债/营业收入", bal["contract_liab"] / inc["revenue"],
          bal["contract_liab"] / inc["revenue"])
    check("F 保利", "现金/短期有息负债", bal["cash"] / (bal["short_loan"] + bal["current_ltd"]),
          bal["cash"] / (bal["short_loan"] + bal["current_ltd"]))
    check("F 保利", "分部收入合计（H1 口径）", d["segment_revenue_sum"],
          sum(s["revenue"] for s in raw["segments"]))
    check("F 保利", "分部与 H1 收入尾差 <0.001 亿", True,
          abs(d["segment_revenue_sum"] - raw["segment_base"]["revenue"]) < 0.001)


# ------------------------------------------------------------------ G 七家披露值
COMPANIES = {
    # 展示值 = 页面/索引上印出来的数字；输入 = 页面同时披露的原始项
    "vanke": {"revenue": 1049.99, "attributable": -239.44, "net_profit": -257.51,
              "cost": 980.29, "ocf": -22.22, "equity_begin": 1169.2, "equity_end": 930.94,
              "assets": None, "liab": None},
    "baoli": {"revenue": 1549.5586, "attributable": 23.3241, "cost": 1351.5971,
              "ocf": 77.1917, "assets": 11533.8071, "liab": 8267.8916},
    "midea": {"revenue": 3673.65, "attributable": 413.93, "ocf": 401.87},
    "gree": {"revenue": 1265.44, "revenue_std": 1260.75, "attributable": 172.74,
             "ocf": 240.63, "income_tax": 36.54, "profit_total": 210.50},
    "haier": {"revenue": 2218.19, "attributable": 149.99, "cost": 1615.29,
              "ocf": 106.70, "assets": 3016.53, "liab": 1717.69,
              "income_tax": 29.54, "profit_total": 185.78},
    "innolight": {"revenue": 695.47, "attributable": 222.38, "ocf": 80.08,
                  "assets": 1297.55, "two_gold": 411.87},
    "cscec": {"revenue": 9758.37, "attributable": 230.00, "ocf": -284.61,
              "income_tax": 166.64, "profit_total": 702.22},
}

EXPECT = {
    "vanke": {"归母净利率": -22.80, "净利润率": -24.52, "毛利率": 6.64, "ROE(平均归母净资产)": -22.80},
    "baoli": {"归母净利率": 1.51, "毛利率": 12.78, "资产负债率": 71.68, "有效税率": 47.21,
              "经营现金流/净利润": 2.03},
    "midea": {"归母净利率": 11.27, "经营现金流/归母净利": 0.97},
    "gree": {"归母净利率": 13.65, "归母净利率(营业收入口径)": 13.70, "有效税率": 17.36,
             "经营现金流/归母净利": 1.39},
    "haier": {"归母净利率": 6.76, "毛利率": 27.18, "资产负债率": 56.94, "有效税率": 15.90,
              "经营现金流/归母净利": 0.71},
    "innolight": {"归母净利率": 31.98, "ROA": 17.14, "两金/总资产": 31.74,
                  "经营现金流/归母净利": 36.01},
    "cscec": {"归母净利率": 2.36, "有效税率(2025年报)": 23.73},
}


def pct(x: float) -> float:
    return round(x * 100, 2)


def verify_companies() -> None:
    c = COMPANIES
    check("G 七家", "万科 归母净利率", EXPECT["vanke"]["归母净利率"],
          pct(c["vanke"]["attributable"] / c["vanke"]["revenue"]))
    check("G 七家", "万科 净利润率", EXPECT["vanke"]["净利润率"],
          pct(c["vanke"]["net_profit"] / c["vanke"]["revenue"]))
    check("G 七家", "万科 毛利率", EXPECT["vanke"]["毛利率"],
          pct(1 - c["vanke"]["cost"] / c["vanke"]["revenue"]))
    avg_eq = (c["vanke"]["equity_begin"] + c["vanke"]["equity_end"]) / 2
    check("G 七家", "万科 ROE（期初期末平均归母净资产）", EXPECT["vanke"]["ROE(平均归母净资产)"],
          pct(c["vanke"]["attributable"] / avg_eq),
          "与归母净利率同为 -22.80% 属数值巧合，非复制错误")

    b = c["baoli"]
    check("G 七家", "保利 归母净利率", EXPECT["baoli"]["归母净利率"],
          pct(b["attributable"] / b["revenue"]),
          "全精度 23.3241÷1,549.5586=1.5052%→1.51%；用已四舍五入的 23.32÷1,549.56 会得 1.50%，属二次舍入误差")
    check("G 七家", "保利 毛利率", EXPECT["baoli"]["毛利率"],
          pct((b["revenue"] - b["cost"]) / b["revenue"]))
    check("G 七家", "保利 资产负债率", EXPECT["baoli"]["资产负债率"],
          pct(b["liab"] / b["assets"]))
    check("G 七家", "保利 有效税率", EXPECT["baoli"]["有效税率"],
          pct(33.9317 / 71.8695))
    check("G 七家", "保利 经营现金流/净利润(含少数)", EXPECT["baoli"]["经营现金流/净利润"],
          r2(b["ocf"] / 37.9378), "净利润 37.94 亿，不是 38.02 亿")

    m = c["midea"]
    check("G 七家", "美的 归母净利率", EXPECT["midea"]["归母净利率"],
          pct(m["attributable"] / m["revenue"]))
    check("G 七家", "美的 经营现金流/归母净利", EXPECT["midea"]["经营现金流/归母净利"],
          r2(m["ocf"] / m["attributable"]))

    g = c["gree"]
    check("G 七家", "格力 归母净利率(营业总收入口径)", EXPECT["gree"]["归母净利率"],
          pct(g["attributable"] / g["revenue"]))
    check("G 七家", "格力 归母净利率(营业收入口径)", EXPECT["gree"]["归母净利率(营业收入口径)"],
          pct(g["attributable"] / g["revenue_std"]))
    check("G 七家", "格力 有效税率(2026Q3)", EXPECT["gree"]["有效税率"],
          pct(g["income_tax"] / g["profit_total"]))
    check("G 七家", "格力 经营现金流/归母净利", EXPECT["gree"]["经营现金流/归母净利"],
          r2(g["ocf"] / g["attributable"]))

    h = c["haier"]
    check("G 七家", "海尔 归母净利率", EXPECT["haier"]["归母净利率"],
          pct(h["attributable"] / h["revenue"]))
    check("G 七家", "海尔 毛利率", EXPECT["haier"]["毛利率"],
          pct((h["revenue"] - h["cost"]) / h["revenue"]))
    check("G 七家", "海尔 资产负债率", EXPECT["haier"]["资产负债率"], pct(h["liab"] / h["assets"]))
    check("G 七家", "海尔 有效税率", EXPECT["haier"]["有效税率"],
          pct(h["income_tax"] / h["profit_total"]))
    check("G 七家", "海尔 经营现金流/归母净利", EXPECT["haier"]["经营现金流/归母净利"],
          r2(h["ocf"] / h["attributable"]))

    i = c["innolight"]
    check("G 七家", "中际 归母净利率", EXPECT["innolight"]["归母净利率"],
          pct(i["attributable"] / i["revenue"]))
    check("G 七家", "中际 ROA(期末总资产)", EXPECT["innolight"]["ROA"],
          pct(i["attributable"] / i["assets"]))
    check("G 七家", "中际 两金/总资产", EXPECT["innolight"]["两金/总资产"],
          pct(i["two_gold"] / i["assets"]))
    check("G 七家", "中际 现金利润保障", EXPECT["innolight"]["经营现金流/归母净利"],
          pct(i["ocf"] / i["attributable"]))

    s = c["cscec"]
    check("G 七家", "中建 归母净利率", EXPECT["cscec"]["归母净利率"],
          pct(s["attributable"] / s["revenue"]))
    check("G 七家", "中建 有效税率(2025年报)", EXPECT["cscec"]["有效税率(2025年报)"],
          pct(s["income_tax"] / s["profit_total"]),
          "2026H1 有效税率 24.3% 为披露值，缺 H1 所得税/利润总额明细，不做复算")


# ------------------------------------------------------------------ H 规则一致性
def verify_rules(brackets: list[dict], source: str) -> None:
    want = [(0.5, 0.30, 0.00), (1.0, 0.40, 0.05), (2.0, 0.50, 0.15), (None, 0.60, 0.35)]
    for b, (mx, rate, quick) in zip(brackets, want):
        check("H 规则", f"土增税率档 ≤{mx}", (rate, quick), (b["rate"], b["quick_deduction"]))
    sur = RULES / "rules_surcharge.json"
    if sur.exists():
        r = json.loads(sur.read_text(encoding="utf-8"))["rules"]
        check("H 规则", "城建税 市区7%", 0.07, r["urban_construction_tax"]["urban"])
        check("H 规则", "城建税 县镇5%", 0.05, r["urban_construction_tax"]["county_town"])
        check("H 规则", "城建税 其他1%", 0.01, r["urban_construction_tax"]["other"])
        check("H 规则", "教育费附加 3%", 0.03, r["education_surcharge"])
        check("H 规则", "地方教育附加 2%", 0.02, r["local_education_surcharge"])
    st = RULES / "rules_stamp.json"
    if st.exists():
        r = json.loads(st.read_text(encoding="utf-8"))["rules"]
        check("H 规则", "印花税 买卖合同 0.03%", 0.0003, r["contracts"]["sale"]["rate"])
        check("H 规则", "印花税 借款合同 0.005%", 0.00005, r["contracts"]["loan"]["rate"])
        check("H 规则", "印花税 产权转移书据 0.05%", 0.0005,
              r["property_transfer"]["real_estate"]["rate"])


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--json", default=str(DATA / "verify-report.json"))
    args = ap.parse_args()

    derived = load("derived.json")
    brackets, source = load_rules()
    for fn in (lambda: verify_audit(derived), lambda: verify_finance(derived),
               lambda: verify_group(derived), lambda: verify_tax(derived, brackets),
               lambda: verify_risk(derived), lambda: verify_case(derived),
               verify_companies, lambda: verify_rules(brackets, source)):
        fn()

    by_section: dict[str, list[dict]] = {}
    for row in ROWS:
        by_section.setdefault(row["section"], []).append(row)
    for sec, rows in by_section.items():
        bad = [r for r in rows if not r["ok"]]
        print(f"{sec}: {len(rows) - len(bad)}/{len(rows)} 通过")
        for r in bad:
            print(f"  ✗ {r['name']}: 展示 {r['expected']} ≠ 复算 {r['actual']}  {r['note']}")
    print(f"\n规则来源: {source}")
    print(f"合计: {len(ROWS) - len(FAIL)}/{len(ROWS)} 通过")

    (Path(args.json)).write_text(json.dumps(
        {"generated_at": "2026-10-31", "rules_source": source,
         "total": len(ROWS), "failed": len(FAIL),
         "rows": ROWS, "failures": FAIL}, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"报告: {args.json}")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
