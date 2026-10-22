#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""三份总监岗作品模板 · 构建脚本

读取 data/*.json 的原始输入，计算全部派生值（重要性、13 周现金流、合并桥、
土增税分类计算、风险加权得分），渲染 Jinja2 模板为 HTML，并调用 Chrome
headless 输出 A4 PDF。

用法:
    python3 scripts/build_templates.py            # 构建 HTML + PDF
    python3 scripts/build_templates.py --no-pdf   # 只构建 HTML
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

from jinja2 import Environment, FileSystemLoader, select_autoescape

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"
TEMPLATES = ROOT / "templates"
CSS_REL = "../assets/report.css"

CHROME_CANDIDATES = [
    "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
    "/Applications/Chromium.app/Contents/MacOS/Chromium",
    "/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge",
]


# ---------------------------------------------------------------- formatting
def fmt_num(value) -> str:
    if value is None:
        return "—"
    if isinstance(value, str):
        return value
    if abs(value - round(value)) < 1e-9:
        return f"{int(round(value)):,}"
    return f"{value:,.1f}"


def fmt_num2(value) -> str:
    if value is None:
        return "—"
    if isinstance(value, str):
        return value
    return f"{value:,.2f}"


def fmt_pct(value) -> str:
    if value is None:
        return "—"
    if isinstance(value, str):
        return value
    return f"{value * 100:.2f}%"


# ---------------------------------------------------------------- audit
def build_audit(raw: dict) -> dict:
    ctx = dict(raw)
    benchmarks = []
    for b in raw["benchmarks"]:
        benchmarks.append({**b, "result": b["amount"] * b["rate"]})
    om = min(b["result"] for b in benchmarks)
    ctx.update(
        benchmarks=benchmarks,
        om=om,
        pm=om * 0.60,
        trivial=om * 0.05,
        risk_count=len(raw["risks"]),
        fraud_count=sum(1 for r in raw["risks"] if r.get("fraud")),
        program_count=len(raw["programs"]),
        control_count=len(raw["controls"]),
        kcp_count=sum(1 for c in raw["controls"] if c["kcp"] == "是"),
        untested_count=sum(1 for c in raw["controls"] if c["result"] == "未测试"),
    )
    defs = raw["deficiencies"]
    ctx["def_count"] = len(defs)
    ctx["def_counts"] = {
        "major": sum(1 for d in defs if d["severity"] == "重大"),
        "significant": sum(1 for d in defs if d["severity"] == "重要"),
        "general": sum(1 for d in defs if d["severity"] == "一般"),
    }
    ctx["closed_count"] = sum(1 for d in defs if d["status"] == "已验证关闭")
    ctx["rect_rate"] = ctx["closed_count"] / len(defs) if defs else 0.0
    return ctx


# ---------------------------------------------------------------- finance
def build_finance(raw: dict) -> dict:
    opening = raw["opening"]
    floor = raw["floor"]
    actual = raw.get("actual_weeks", 2)
    weeks = []
    balance = opening
    for i, w in enumerate(raw["weeks"]):
        net = w["inflow"] - w["outflow"]
        ending = balance + net
        gap = max(0, floor - ending)
        if gap == 0:
            light = "绿"
        elif ending >= floor * 0.7:
            light = "黄"
        else:
            light = "红"
        weeks.append(
            {
                "label": w["label"],
                "status": "实际" if i < actual else "预测",
                "opening": balance,
                "inflow": w["inflow"],
                "outflow": w["outflow"],
                "net": net,
                "ending": ending,
                "floor": floor,
                "gap": gap,
                "light": light,
            }
        )
        balance = ending

    inflow = sum(w["inflow"] for w in weeks)
    outflow = sum(w["outflow"] for w in weeks)
    min_week = min(weeks, key=lambda w: w["ending"])
    gap_weeks = sum(1 for w in weeks if w["gap"] > 0)
    cumulative_gap = sum(w["gap"] for w in weeks)

    cash_raw = raw["cash"]
    total_cash = cash_raw["regulatory"] + cash_raw["non_regulatory"]
    avail = total_cash - cash_raw["restricted"]
    cash = {
        "regulatory": cash_raw["regulatory"],
        "non_regulatory": cash_raw["non_regulatory"],
        "total": total_cash,
        "reg_pct": cash_raw["regulatory"] / total_cash,
        "non_reg_pct": cash_raw["non_regulatory"] / total_cash,
        "restricted": cash_raw["restricted"],
        "non_restricted": cash_raw["non_restricted"],
        "restr_pct": cash_raw["restricted"] / total_cash,
        "non_restr_pct": cash_raw["non_restricted"] / total_cash,
        "available": avail,
        "avail_pct": avail / total_cash,
    }

    fin = {
        "period": raw["period"],
        "scope": raw["scope"],
        "unit": raw["unit"],
        "version": raw["version"],
        "floor": floor,
        "opening": opening,
        "weeks": weeks,
        "totals": {
            "inflow": inflow,
            "outflow": outflow,
            "net": inflow - outflow,
            "ending": balance,
            "min_week": int(min_week["label"].lstrip("W")),
            "min_amount": min_week["ending"],
            "gap_weeks": gap_weeks,
            "cumulative_gap": cumulative_gap,
            "coverage": balance / floor if floor else 0.0,
        },
        "cash": cash,
        "assumptions": raw["assumptions"],
        "chart_svg": make_chart(weeks, floor, actual),
    }
    fin["conclusions"] = [
        f"最低点 {min_week['label']}（期末 {fmt_num(min_week['ending'])} 万元），"
        f"{gap_weeks} 周低于保有线，累计缺口 {fmt_num(cumulative_gap)} 万元。",
        "把 W4–W6 的 3 笔分包付款（2,800 万元）与回款节点挂钩，争取延后 2 周。",
        "启用 3,000 万元授信覆盖 W5–W7 缺口；对逾期 60 天应收 1,900 万元专项催收。",
    ]
    return fin


def make_chart(weeks, floor, actual) -> str:
    n = len(weeks)
    W, H = 720, 176
    L, R, T, B = 42, 12, 22, 24
    plot_w, plot_h = W - L - R, H - T - B
    peak = max(
        [w["inflow"] for w in weeks]
        + [w["outflow"] for w in weeks]
        + [w["ending"] for w in weeks]
        + [floor]
    )
    top_val = peak * 1.12

    def y(v):
        return T + plot_h * (1 - v / top_val)

    slot = plot_w / n
    bar_w = slot * 0.27
    out = [
        f'<svg viewBox="0 0 {W} {H}" width="100%" role="img" '
        f'aria-label="13 周现金流入流出与期末现金曲线" '
        f'style="display:block;font-family:Inter,PingFang SC,sans-serif">'
    ]

    # legend
    out.append(f'<rect x="{L}" y="6" width="7" height="7" fill="#1565c0"/>')
    out.append(f'<text x="{L + 11}" y="13" font-size="8" fill="#4a5568">流入</text>')
    out.append(f'<rect x="{L + 52}" y="6" width="7" height="7" fill="#c62828"/>')
    out.append(f'<text x="{L + 63}" y="13" font-size="8" fill="#4a5568">流出</text>')
    out.append(f'<line x1="{L + 104}" y1="9.5" x2="{L + 124}" y2="9.5" stroke="#b8944c" stroke-width="2"/>')
    out.append(f'<text x="{L + 128}" y="13" font-size="8" fill="#4a5568">期末现金</text>')
    out.append(f'<line x1="{L + 190}" y1="9.5" x2="{L + 210}" y2="9.5" stroke="#e65100" stroke-width="1.5" stroke-dasharray="3 3"/>')
    out.append(f'<text x="{L + 214}" y="13" font-size="8" fill="#4a5568">最低保有 {fmt_num(floor)}</text>')

    # grid + y labels
    for frac in (0, 0.25, 0.5, 0.75, 1.0):
        val = top_val * frac
        yy = y(val)
        out.append(f'<line x1="{L}" y1="{yy:.1f}" x2="{W - R}" y2="{yy:.1f}" stroke="#e8edf3" stroke-width="1"/>')
        out.append(
            f'<text x="{L - 6}" y="{yy + 3:.1f}" text-anchor="end" font-size="7.5" fill="#94a3b8">'
            f"{fmt_num(round(val))}</text>"
        )

    # bars
    for i, w in enumerate(weeks):
        cx = L + slot * (i + 0.5)
        opacity = "1" if i < actual else "0.42"
        x_in = cx - bar_w - 1
        x_out = cx + 1
        h_in = plot_h * w["inflow"] / top_val
        h_out = plot_h * w["outflow"] / top_val
        out.append(
            f'<rect x="{x_in:.1f}" y="{y(w["inflow"]):.1f}" width="{bar_w:.1f}" '
            f'height="{h_in:.1f}" fill="#1565c0" opacity="{opacity}" rx="1"/>'
        )
        out.append(
            f'<rect x="{x_out:.1f}" y="{y(w["outflow"]):.1f}" width="{bar_w:.1f}" '
            f'height="{h_out:.1f}" fill="#c62828" opacity="{opacity}" rx="1"/>'
        )
        out.append(
            f'<text x="{cx:.1f}" y="{H - 12}" text-anchor="middle" font-size="7" fill="#718096">'
            f"{w['label']}</text>"
        )

    # floor line
    yf = y(floor)
    out.append(
        f'<line x1="{L}" y1="{yf:.1f}" x2="{W - R}" y2="{yf:.1f}" '
        f'stroke="#e65100" stroke-width="1.4" stroke-dasharray="4 3"/>'
    )

    # ending cash line
    points = " ".join(
        f"{L + slot * (i + 0.5):.1f},{y(w['ending']):.1f}" for i, w in enumerate(weeks)
    )
    out.append(f'<polyline points="{points}" fill="none" stroke="#b8944c" stroke-width="2" stroke-linejoin="round"/>')
    for i, w in enumerate(weeks):
        cx = L + slot * (i + 0.5)
        out.append(f'<circle cx="{cx:.1f}" cy="{y(w["ending"]):.1f}" r="2.1" fill="#fff" stroke="#b8944c" stroke-width="1.6"/>')

    out.append("</svg>")
    return "".join(out)


# ---------------------------------------------------------------- group
def build_group(raw: dict) -> dict:
    ents = raw["entities"]
    rev_sum = sum(e["revenue"] for e in ents)
    cost_sum = sum(e["cost"] for e in ents)
    net_sum = sum(e["net"] for e in ents)
    ocf_sum = sum(e["ocf"] for e in ents)
    budget_sum = sum(e["budget"] for e in ents)

    revenue = rev_sum - raw["internal_rev"]
    cost = cost_sum - raw["internal_cost"]
    net = net_sum - raw["internal_profit"]
    attributable = net - raw["minority"]

    enriched = []
    for e in ents:
        completion = e["revenue"] / e["budget"] if e["budget"] else 0.0
        if completion < 0.85 or e["net"] < 0:
            light = "红"
        elif completion < 0.95 or e["ocf"] < 0:
            light = "黄"
        else:
            light = "绿"
        enriched.append(
            {
                **e,
                "gm": (e["revenue"] - e["cost"]) / e["revenue"] if e["revenue"] else 0.0,
                "nm": e["net"] / e["revenue"] if e["revenue"] else 0.0,
                "completion": completion,
                "variance": e["revenue"] - e["budget"],
                "light": light,
            }
        )

    ranked = sorted(enriched, key=lambda e: e["net"], reverse=True)
    for i, e in enumerate(ranked, 1):
        e["rank"] = i

    top = ranked[:10]
    max_top = max(abs(e["net"]) for e in top) or 1
    top_bars = [
        {"name": e["name"], "net": e["net"], "width": abs(e["net"]) / max_top * 100,
         "cls": "green" if e["net"] >= 0 else "red"}
        for e in top
    ]
    bottom_ents = sorted(enriched, key=lambda e: e["net"])[:5]
    max_bottom = max(abs(e["net"]) for e in bottom_ents) or 1
    bottom_bars = [
        {"name": e["name"], "net": e["net"], "width": abs(e["net"]) / max_bottom * 100, "cls": "red"}
        for e in bottom_ents
    ]

    variance = sorted(enriched, key=lambda e: abs(e["variance"]), reverse=True)[:5]
    max_var = max(abs(e["variance"]) for e in variance) or 1
    variance_bars = [
        {"name": e["name"], "variance": e["variance"], "width": abs(e["variance"]) / max_var * 100,
         "cls": "green" if e["variance"] >= 0 else "red"}
        for e in variance
    ]

    lights = {"green": 0, "yellow": 0, "red": 0}
    for e in enriched:
        lights[{"绿": "green", "黄": "yellow", "红": "red"}[e["light"]]] += 1

    neg_ocf = [e for e in enriched if e["ocf"] < 0]
    reds = [e for e in enriched if e["light"] == "红"]
    top3 = sum(e["net"] for e in ranked[:3])
    conclusions = [
        f"净利前 3 家（{ranked[0]['name']}、{ranked[1]['name']}、{ranked[2]['name']}）合计 "
        f"{fmt_num(top3)} 万元，占 20 家净利合计的 {fmt_pct(top3 / net_sum)}。",
        f"收入完成率 {fmt_pct(rev_sum / budget_sum)}，但有 {len(neg_ocf)} 家经营现金流为负，"
        f"合计 {fmt_num(sum(e['ocf'] for e in neg_ocf))} 万元；红灯 {len(reds)} 家"
        + (f"（{'、'.join(e['name'] for e in reds)}）" if reds else "") + "。",
        f"内部交易抵消 {fmt_num(raw['internal_rev'])} 万元，月结前须完成对账；"
        f"归母净利 {fmt_num(attributable)} 万元，少数股东损益 {fmt_num(raw['minority'])} 万元。",
    ]
    conclusions = [c.rstrip("。") for c in conclusions]

    return {
        "period": raw["period"],
        "entity_count": raw["entity_count"],
        "unit": raw["unit"],
        "budget_version": raw["budget_version"],
        "totals": {
            "revenue": revenue,
            "cost": cost,
            "gross_profit": revenue - cost,
            "gm": (revenue - cost) / revenue if revenue else 0.0,
            "net": net,
            "nm": net / revenue if revenue else 0.0,
            "attributable": attributable,
            "ocf": ocf_sum,
            "cash": raw["cash"],
            "completion": rev_sum / budget_sum if budget_sum else 0.0,
            "revenue_sum": rev_sum,
            "cost_sum": cost_sum,
            "internal_rev": raw["internal_rev"],
            "internal_cost": raw["internal_cost"],
            "net_sum": net_sum,
            "internal_profit": raw["internal_profit"],
            "minority": raw["minority"],
        },
        "lights": lights,
        "top": top_bars,
        "bottom": bottom_bars,
        "variance_top": variance_bars,
        "entities": ranked,
        "conclusions": conclusions,
    }


# ---------------------------------------------------------------- land VAT
def vat_tier(ratio: float):
    if ratio <= 0.5:
        return "≤50%", 0.30, 0.0
    if ratio <= 1.0:
        return "50%–100%", 0.40, 0.05
    if ratio <= 2.0:
        return "100%–200%", 0.50, 0.15
    return ">200%", 0.60, 0.35


def land_vat(raw: dict, mode: str, revenue_factor: float = 1.0, cost_factor: float = 1.0):
    """按分类计算应纳土增税。mode: 'A' 或 'B'。"""
    revenues = []
    # 收入按已售面积；扣除分摊按可售面积（国税发〔2006〕187号：共同成本按可转让建筑面积分摊）
    area_total = sum(r["saleable"] for r in raw["revenues"])
    sold_total = sum(r["area"] for r in raw["revenues"])
    project = raw["project"]
    if area_total != project["saleable_area"]:
        raise ValueError(f"分业态可售面积合计 {area_total} ≠ 项目可售面积 {project['saleable_area']}")
    if sold_total != project["sold_area"]:
        raise ValueError(f"分业态已售面积合计 {sold_total} ≠ 项目已售面积 {project['sold_area']}")
    for r in raw["revenues"]:
        amount = r["area"] * r["price"] / 10000 * revenue_factor
        revenues.append({**r, "amount": amount, "share": r["saleable"] / area_total})

    land = raw["land"]
    dev_cost = raw["dev_cost"] * cost_factor
    interest = raw["interest"]
    taxes = raw["taxes"] * revenue_factor
    base = land + dev_cost
    dev_exp = interest + base * 0.05 if mode == "A" else base * 0.10
    add20 = base * 0.20
    deduction_total = base + dev_exp + taxes + add20

    ordinary_rev = sum(r["amount"] for r in revenues if r["ordinary"])
    other_rev = sum(r["amount"] for r in revenues if not r["ordinary"])
    ordinary_share = sum(r["share"] for r in revenues if r["ordinary"])
    ordinary_sold = sum(r["area"] for r in revenues if r["ordinary"])
    other_sold = sum(r["area"] for r in revenues if not r["ordinary"])

    groups = []
    for name, rev, share, sold, is_ordinary in (
        ("普通住宅（单独核算）", ordinary_rev, ordinary_share, ordinary_sold, True),
        ("其他业态（合并核算）", other_rev, 1 - ordinary_share, other_sold, False),
    ):
        # 成本按可售面积均摊，仅扣除已售部分；未售部分待尾盘销售时处理
        ded = deduction_total * sold / area_total
        appr = rev - ded
        ratio = appr / ded if ded else 0.0
        tier, rate, quick = vat_tier(ratio)
        exempt = bool(is_ordinary and ratio <= 0.20)
        tax = 0.0 if exempt else max(appr * rate - ded * quick, 0.0)
        groups.append(
            {
                "name": name,
                "revenue": rev,
                "deduction": ded,
                "appreciation": appr,
                "ratio": ratio,
                "tier": "普通住宅 ≤20%（免征）" if exempt else tier,
                "rate": 0.0 if exempt else rate,
                "quick": 0.0 if exempt else quick,
                "tax": tax,
                "exempt": exempt,
                "area_share": share,
            }
        )

    total_rev = sum(g["revenue"] for g in groups)
    total_ded = sum(g["deduction"] for g in groups)       # 已售部分可扣除
    total_appr = total_rev - total_ded
    return {
        "revenues": revenues,
        "area_total": area_total,
        "revenue_total": total_rev,
        "land": land,
        "dev_cost": dev_cost,
        "interest": interest,
        "taxes": taxes,
        "dev_exp": dev_exp,
        "add20": add20,
        "deduction_total": total_ded,                 # 已售部分可扣除（分类合计）
        "project_deduction": deduction_total,         # 项目扣除总额（含未售对应）
        "unsold_deduction": deduction_total - total_ded,
        "appreciation_total": total_appr,
        "ratio_total": total_appr / total_ded if total_ded else 0.0,
        "groups": groups,
        "tax_total": sum(g["tax"] for g in groups),
        "ordinary": groups[0],
    }


def build_tax(raw: dict) -> dict:
    a = land_vat(raw, "A")
    b = land_vat(raw, "B")

    project = dict(raw["project"])
    project["sold_ratio"] = project["sold_area"] / project["saleable_area"]

    scenarios = []
    for s in raw["scenarios"]:
        r = land_vat(raw, "A", s["revenue_factor"], s["cost_factor"])
        scenarios.append(
            {
                "name": s["name"],
                "revenue": r["revenue_total"],
                "dev_cost": r["dev_cost"],
                "deduction": r["deduction_total"],
                "appreciation": r["appreciation_total"],
                "ratio": r["ratio_total"],
                "tax": r["tax_total"],
                "net_profit": r["revenue_total"] - r["dev_cost"] - r["land"] - r["taxes"] - r["tax_total"],
            }
        )

    ordinary = dict(a["ordinary"])
    ordinary["gap_to_20"] = ordinary["ratio"] - 0.20

    tax = {
        "project": project,
        "revenues": a["revenues"],
        "area_total": a["area_total"],
        "sold_total": sum(r["area"] for r in a["revenues"]),
        "revenue_total": a["revenue_total"],
        "land": a["land"],
        "dev_cost": a["dev_cost"],
        "interest": a["interest"],
        "taxes": a["taxes"],
        "dev_exp_a": a["dev_exp"],
        "dev_exp_b": b["dev_exp"],
        "add20": a["add20"],
        "deduction_a": a["project_deduction"],
        "deduction_b": b["project_deduction"],
        "deductible_total": a["deduction_total"],
        "unsold_deduction": a["unsold_deduction"],
        "a_vs_b": b["project_deduction"] - a["project_deduction"],
        "groups": a["groups"],
        "appreciation_total": a["appreciation_total"],
        "ratio_total": a["ratio_total"],
        "tax_total": a["tax_total"],
        "tax_b_total": b["tax_total"],
        "a_minus_b": a["tax_total"] - b["tax_total"],
        "prepaid": raw["prepaid"],
        "payable_total": a["tax_total"] - raw["prepaid"],
        "ordinary": ordinary,
        "scenarios": scenarios,
    }

    base, cons, opt = scenarios
    tax["conclusions"] = [
        f"按分类计算应纳土地增值税 {fmt_num(a['tax_total'])} 万元，已预缴 {fmt_num(raw['prepaid'])} 万元，"
        f"应补 {fmt_num(a['tax_total'] - raw['prepaid'])} 万元；普通住宅增值率 {fmt_pct(ordinary['ratio'])}，"
        f"{'未超过' if ordinary['exempt'] else '超过'} 20% 免征线"
        + ("" if ordinary["exempt"] else f" {fmt_pct(ordinary['gap_to_20'])}"),
        f"口径 A 比 B 少扣除 {fmt_num(b['project_deduction'] - a['project_deduction'])} 万元（项目总额），税额反而高 "
        f"{fmt_num(a['tax_total'] - b['tax_total'])} 万元；但本例可提供利息证明，按现行口径必须适用 A，"
        f"B 不能作为可选择方案。",
        f"收入下降 5% 且成本上升 5% 时，税额降至 {fmt_num(cons['tax'])} 万元（较基准 "
        f"{fmt_pct(cons['tax'] / a['tax_total'] - 1)}）；收入上升 5% 时升至 {fmt_num(opt['tax'])} 万元。"
        f"税负对收入与建安成本高度敏感，清算前应先锁定收入确认与成本归集口径。",
    ]
    tax["conclusions"] = [c.rstrip("。") for c in tax["conclusions"]]
    return tax


# ---------------------------------------------------------------- risk
def build_risk(raw: dict) -> dict:
    inds = raw["indicators"]
    weight_sum = sum(i["weight"] for i in inds)
    score = sum(i["weight"] * i["points"] for i in inds) / weight_sum / 2 * 100
    if score >= 70:
        level = "高"
    elif score >= 40:
        level = "中高"
    elif score >= 20:
        level = "中"
    else:
        level = "低"

    pending = sum(1 for p in raw["policies"] if p["status"] == "待核")
    kpis = []
    for k in raw["kpis"]:
        k = dict(k)
        if k["value"] == "AUTO":
            k["value"] = str(pending)
        kpis.append(k)

    overall = dict(raw["overall"])
    overall["score"] = round(score, 1)
    overall["level"] = level

    return {
        "period": raw["period"],
        "entity": raw["entity"],
        "unit": raw["unit"],
        "kpis": kpis,
        "indicators": inds,
        "high_risks": raw["high_risks"],
        "policies": raw["policies"],
        "overall": overall,
        "actions": raw["actions"],
    }


# ---------------------------------------------------------------- public-data case
def build_case(raw: dict) -> dict:
    inc, bal, cf = raw["income"], raw["balance"], raw["cashflow"]
    revenue = inc["revenue"]
    gross_profit = revenue - inc["cost"]
    gm = gross_profit / revenue
    net = inc["net_profit"]
    attrib = inc["attributable"]
    ocf = cf["ocf"]

    def pct(v):
        return v

    c = {
        "revenue": revenue,
        "cost": inc["cost"],
        "gross_profit": gross_profit,
        "gm": gm,
        "gm_prior": inc["gm_prior"],
        "net_profit": net,
        "attributable": attrib,
        "minority": inc["minority"],
        "ocf": ocf,
        "cash": bal["cash"],
        "inventory": bal["inventory"],
        "contract_liab": bal["contract_liab"],
        "revenue_yoy": revenue / inc["revenue_prior"] - 1,
        "attrib_yoy": attrib / inc["attributable_prior"] - 1,
        "ocf_yoy": ocf / cf["ocf_prior"] - 1,
        "net_margin": net / revenue,
        "attrib_margin": attrib / revenue,
        "leverage": bal["total_liabilities"] / bal["total_assets"],
        "inventory_pct": bal["inventory"] / bal["total_assets"],
        "cash_to_debt": bal["cash"] / (bal["short_loan"] + bal["current_ltd"]),
        "ocf_to_net": ocf / net,
        "ocf_to_attrib": ocf / attrib,
        "tax_surcharge": inc["tax_surcharge"],
        "tax_surcharge_ratio": inc["tax_surcharge"] / revenue,
        "income_tax": inc["income_tax"],
        "etr": inc["income_tax"] / inc["profit_total"],
        "dta": bal["deferred_tax_asset"],
        "dta_pct": bal["deferred_tax_asset"] / bal["total_assets"],
        "contract_ratio": bal["contract_liab"] / revenue,
        "minority_ratio": inc["minority"] / net,
        "ar_ratio": bal["accounts_rece"] / revenue,
        "sales_cash_ratio": cf["sales_cash"] / revenue,
        "taxes_paid_ratio": cf["taxes_paid"] / revenue,
        "financing": cf["financing"],
        "investing": cf["investing"],
    }
    c["etr_gap"] = c["etr"] - 0.25
    td = raw.get("tax_detail", {})
    c["land_vat_current"] = td.get("land_vat_current", 0)
    c["land_vat_payable"] = td.get("land_vat_payable", 0)
    c["income_tax_current"] = td.get("income_tax_current", inc["income_tax"])
    c["deferred_tax_expense"] = td.get("deferred_tax_expense", 0)
    c["tax_reconciliation"] = td.get("reconciliation", [])
    c["etr_main_driver"] = next(
        (x["amount"] for x in c["tax_reconciliation"] if x["item"].startswith("本期未确认")), 0
    )
    c["etr_main_driver_pct"] = c["etr_main_driver"] / inc["profit_total"]

    trend = []
    prev = None
    for t in raw.get("trend", []):
        row = dict(t)
        row["revenue_yoy"] = (t["revenue"] / prev["revenue"] - 1) if prev else None
        row["attrib_yoy"] = (t["attributable"] / prev["attributable"] - 1) if prev else None
        trend.append(row)
        prev = t
    c["trend"] = trend

    bm = [
        {"name": "税前利润", "amount": inc["profit_total"], "rate": 0.05, "note": "常用 5%，盈利稳定时适用"},
        {"name": "营业收入", "amount": revenue, "rate": 0.005, "note": "常用 0.5%，收入规模大时适用"},
        {"name": "资产总额", "amount": bal["total_assets"], "rate": 0.003, "note": "常用 0.3%"},
        {"name": "净资产", "amount": bal["equity"], "rate": 0.01, "note": "常用 1%"},
    ]
    for b in bm:
        b["result"] = b["amount"] * b["rate"]
    c["benchmarks"] = bm
    c["om"] = min(b["result"] for b in bm)
    c["pm"] = c["om"] * 0.6
    c["trivial"] = c["om"] * 0.05

    segs = []
    for s0 in raw["segments"]:
        gp = s0["revenue"] - s0["cost"]
        gm_s = gp / s0["revenue"] if s0["revenue"] else 0.0
        if gm_s >= 0.15:
            light = "绿"
        elif gm_s >= 0.10:
            light = "黄"
        else:
            light = "红"
        segs.append({**s0, "gross_profit": gp, "gm": gm_s, "share": s0["revenue"] / revenue, "light": light})
    ranked = sorted(segs, key=lambda x: x["gross_profit"], reverse=True)
    for i, x in enumerate(ranked, 1):
        x["rank"] = i
    c["segments"] = ranked
    c["segment_revenue_sum"] = sum(x["revenue"] for x in ranked)
    c["segment_gap"] = c["segment_revenue_sum"] - revenue

    top = ranked[:5]
    bottom = ranked[-5:]
    max_gp = max(x["gross_profit"] for x in top) or 1
    max_bp = max(x["gross_profit"] for x in bottom) or 1
    c["top"] = [{"name": x["name"], "gross_profit": x["gross_profit"], "width": x["gross_profit"] / max_gp * 100} for x in top]
    c["bottom"] = [{"name": x["name"], "gross_profit": x["gross_profit"], "width": x["gross_profit"] / max_bp * 100} for x in bottom]

    c["risks"] = [
        {"id": "R-01", "area": "收入与截止", "signal": f"营收 {revenue:,.2f} 亿（{c['revenue_yoy']*100:+.2f}%）、归母 {attrib:,.2f} 亿（{c['attrib_yoy']*100:+.2f}%）", "level": "高", "program": "收入截止测试 + 交付结转抽样 + 三维趋势分析", "assertion": "发生 / 截止"},
        {"id": "R-02", "area": "存货跌价", "signal": f"存货 {bal['inventory']:,.2f} 亿，占总资产 {c['inventory_pct']*100:.2f}%；毛利率 {gm*100:.2f}%", "level": "高", "program": "项目 NRV 测试 + 跌价准备复核", "assertion": "计价与分摊"},
        {"id": "R-03", "area": "合同负债", "signal": f"合同负债 {bal['contract_liab']:,.2f} 亿，为营收 {c['contract_ratio']:.2f}x", "level": "中高", "program": "预收与交付台账匹配 + 结转重算", "assertion": "完整性 / 截止"},
        {"id": "R-04", "area": "合作项目", "signal": f"少数股东权益 {bal['minority_equity']:,.2f} 亿（占净资产 {bal['minority_equity']/bal['equity']*100:.2f}%）", "level": "高", "program": "合作项目穿透 + 明股实债识别 + 资金往来", "assertion": "列报 / 完整性"},
        {"id": "R-05", "area": "有息负债", "signal": f"短期借款 {bal['short_loan']:,.2f} 亿 + 一年内到期 {bal['current_ltd']:,.2f} 亿", "level": "中高", "program": "债务契约检查 + 受限资金函证", "assertion": "列报 / 计价"},
        {"id": "R-06", "area": "税务", "signal": f"有效税率 {c['etr']*100:.2f}%；税金及附加 {inc['tax_surcharge']:,.2f} 亿", "level": "中高", "program": "所得税调整表 + 递延所得税 + 土增税预提", "assertion": "准确性 / 合规"},
    ]
    c["programs"] = [
        {"id": "P-01", "risk": "R-01", "type": "分析性程序", "desc": "收入、毛利、回款三维趋势与季度拆解", "sample": "12 个季度", "wp": "A-01"},
        {"id": "P-02", "risk": "R-01", "type": "实质性程序", "desc": "交付结转截止测试，检查验收/交付单据", "sample": "截止日前后各 15 天", "wp": "A-02"},
        {"id": "P-03", "risk": "R-02", "type": "实质性程序", "desc": "存货项目 NRV 测试，比较售价与开发成本", "sample": "前 20 大项目", "wp": "B-01"},
        {"id": "P-04", "risk": "R-03", "type": "实质性程序", "desc": "合同负债与交付台账匹配，重算结转", "sample": "前 15 大项目", "wp": "B-02"},
        {"id": "P-05", "risk": "R-04", "type": "穿透测试", "desc": "合作项目协议、资金往来与少数股东穿透", "sample": "全部重大合作项目", "wp": "C-01"},
        {"id": "P-06", "risk": "R-05/R-06", "type": "实质性程序", "desc": "有息负债与受限资金函证；所得税调整复核", "sample": "全部账户 + 税审表", "wp": "D-01"},
    ]

    c["tax_indicators"] = [
        {"name": "有效税率", "formula": "所得税费用 ÷ 利润总额", "value": f"{c['etr']*100:.2f}%", "threshold": "法定 25%", "note": f"高 {c['etr_gap']*100:.2f}pp，须拆分递延与调整"},
        {"name": "税金及附加 / 收入", "formula": "税金及附加 ÷ 营业收入", "value": f"{c['tax_surcharge_ratio']*100:.2f}%", "threshold": "1%–2%（房企参考）", "note": "含土增税、附加税等"},
        {"name": "所得税费用 / 利润总额", "formula": "同有效税率", "value": f"{c['etr']*100:.2f}%", "threshold": "25%", "note": "含递延所得税"},
        {"name": "递延所得税资产 / 总资产", "formula": "DTA ÷ 总资产", "value": f"{c['dta_pct']*100:.2f}%", "threshold": "< 3%", "note": "关注可转回性"},
        {"name": "合同负债 / 收入", "formula": "合同负债 ÷ 营业收入", "value": f"{c['contract_ratio']:.2f}x", "threshold": "1.5–3x（年化）", "note": "预收与结转时点；本期用半年收入"},
        {"name": "少数股东损益 / 净利润", "formula": "少数股东损益 ÷ 净利润", "value": f"{c['minority_ratio']*100:.2f}%", "threshold": "< 30%", "note": "合作项目占比偏高"},
        {"name": "应收账款 / 收入", "formula": "应收账款 ÷ 营业收入", "value": f"{c['ar_ratio']*100:.2f}%", "threshold": "< 10%", "note": "地产以预收为主"},
        {"name": "存货 / 总资产", "formula": "存货 ÷ 总资产", "value": f"{c['inventory_pct']*100:.2f}%", "threshold": "40%–60%", "note": "去化与减值压力"},
        {"name": "经营现金流 / 净利润", "formula": "OCF ÷ 净利润", "value": f"{c['ocf_to_net']:.2f}x", "threshold": "> 1", "note": "利润有现金支撑"},
        {"name": "本期土地增值税", "formula": "税金及附加附注", "value": f"{c['land_vat_current']:.2f} 亿", "threshold": "—", "note": "附注已披露；项目级清算表未公开"},
        {"name": "应交土地增值税", "formula": "应交税费附注", "value": f"{c['land_vat_payable']:.2f} 亿", "threshold": "—", "note": "期末应交余额"},
        {"name": "递延所得税费用", "formula": "所得税费用附注", "value": f"{c['deferred_tax_expense']:.2f} 亿", "threshold": "—", "note": f"当期所得税 {c['income_tax_current']:.2f} 亿"},
    ]
    c["tax_risks"] = [
        {"risk": "有效税率偏高", "evidence": f"所得税 {inc['income_tax']:,.2f} 亿（当期 {c['income_tax_current']:,.2f} + 递延 {c['deferred_tax_expense']:,.2f}）；调整表中本期未确认 DTA +{c['etr_main_driver']:,.2f} 亿", "action": "核对未确认 DTA 的项目构成与转回假设"},
        {"risk": "合同负债预缴", "evidence": f"合同负债 {bal['contract_liab']:,.2f} 亿，为营收 {c['contract_ratio']:.2f}x", "action": "核对预缴与交付结转"},
        {"risk": "合作项目少数股东", "evidence": f"少数股东权益 {bal['minority_equity']:,.2f} 亿，损益占比 {c['minority_ratio']*100:.2f}%", "action": "穿透协议与关联定价"},
    ]
    return {"case": raw, "c": c}


# ---------------------------------------------------------------- render
def render(env: Environment, template: str, context: dict, out_dir: Path, title: str) -> Path:
    out_dir.mkdir(parents=True, exist_ok=True)
    html = env.get_template(template).render(css_path=CSS_REL, title=title, **context)
    path = out_dir / "index.html"
    path.write_text(html, encoding="utf-8")
    return path


def to_pdf(html: Path, pdf: Path) -> bool:
    chrome = next((c for c in CHROME_CANDIDATES if Path(c).exists()), None)
    if not chrome:
        print("  ! 未找到 Chrome/Chromium，跳过 PDF", file=sys.stderr)
        return False
    cmd = [
        chrome,
        "--headless=new",
        "--disable-gpu",
        "--no-sandbox",
        "--no-pdf-header-footer",
        "--run-all-compositor-stages-before-draw",
        "--virtual-time-budget=4000",
        f"--print-to-pdf={pdf}",
        html.resolve().as_uri(),
    ]
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
    if proc.returncode != 0 or not pdf.exists():
        print(f"  ! PDF 生成失败: {proc.stderr[-400:]}", file=sys.stderr)
        return False
    return True


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--no-pdf", action="store_true", help="只生成 HTML")
    args = parser.parse_args()

    env = Environment(
        loader=FileSystemLoader(str(TEMPLATES)),
        autoescape=select_autoescape(["html", "xml"]),
        trim_blocks=True,
        lstrip_blocks=True,
    )
    env.filters["num"] = fmt_num
    env.filters["num2"] = fmt_num2
    env.filters["pct"] = fmt_pct

    audit_raw = json.loads((DATA / "audit_sample.json").read_text(encoding="utf-8"))
    cf_raw = json.loads((DATA / "cashflow_sample.json").read_text(encoding="utf-8"))
    group_raw = json.loads((DATA / "group_sample.json").read_text(encoding="utf-8"))
    vat_raw = json.loads((DATA / "land_vat_sample.json").read_text(encoding="utf-8"))
    risk_raw = json.loads((DATA / "tax_risk_sample.json").read_text(encoding="utf-8"))
    case_raw = json.loads((DATA / "case_baoli.json").read_text(encoding="utf-8"))

    audit = build_audit(audit_raw)
    fin = build_finance(cf_raw)
    group = build_group(group_raw)
    tax = build_tax(vat_raw)
    risk = build_risk(risk_raw)
    case_ctx = build_case(case_raw)

    if abs(group["totals"]["cash"] - fin["totals"]["ending"]) > 1e-6:
        print(
            f"  ! 集团期末现金 {fmt_num(group['totals']['cash'])} 与 13 周末现金 "
            f"{fmt_num(fin['totals']['ending'])} 不一致",
            file=sys.stderr,
        )
        return 2

    jobs = [
        ("audit", "audit.html.j2", {"audit": audit}, "风险导向内审方案（示例）· 作品模板"),
        ("finance", "finance.html.j2", {"fin": fin, "group": group}, "13 周现金流预测 + 集团快报（示例）· 作品模板"),
        ("tax", "tax.html.j2", {"tax": tax, "risk": risk}, "土地增值税清算模型 + 税务风险看板（示例）· 作品模板"),
        ("case-baoli", "case.html.j2", case_ctx, "保利发展 600048 · 2026 半年报公开数据案例"),
    ]

    rc = 0
    for name, tpl, ctx, title in jobs:
        out_dir = ROOT / name
        html = render(env, tpl, ctx, out_dir, title)
        print(f"[{name}] HTML → {html.relative_to(ROOT)}")
        if not args.no_pdf:
            pdf = out_dir / f"{name}.pdf"
            ok = to_pdf(html, pdf)
            print(f"[{name}] PDF  → {pdf.relative_to(ROOT)}" if ok else f"[{name}] PDF 失败")
            rc |= 0 if ok else 1

    # 关键数字同时写入 data/derived.json，供验证脚本复用
    derived = {
        "audit": {
            "om": audit["om"], "pm": audit["pm"], "trivial": audit["trivial"],
            "risk_count": audit["risk_count"], "fraud_count": audit["fraud_count"],
            "program_count": audit["program_count"],
            "control_count": audit["control_count"], "kcp_count": audit["kcp_count"],
            "untested_count": audit["untested_count"],
            "def_count": audit["def_count"], "def_counts": audit["def_counts"],
            "closed_count": audit["closed_count"], "rect_rate": audit["rect_rate"],
        },
        "finance": {
            "opening": fin["opening"], "ending": fin["totals"]["ending"],
            "inflow": fin["totals"]["inflow"], "outflow": fin["totals"]["outflow"],
            "net": fin["totals"]["net"], "min_week": fin["totals"]["min_week"],
            "min_amount": fin["totals"]["min_amount"],
            "gap_weeks": fin["totals"]["gap_weeks"],
            "cumulative_gap": fin["totals"]["cumulative_gap"],
            "cash_total": fin["cash"]["total"],
            "cash_available": fin["cash"]["available"],
        },
        "group": {
            "revenue": group["totals"]["revenue"],
            "cost": group["totals"]["cost"],
            "gross_profit": group["totals"]["gross_profit"],
            "gm": group["totals"]["gm"], "net": group["totals"]["net"],
            "nm": group["totals"]["nm"], "attributable": group["totals"]["attributable"],
            "ocf": group["totals"]["ocf"], "cash": group["totals"]["cash"],
            "completion": group["totals"]["completion"],
            "revenue_sum": group["totals"]["revenue_sum"],
            "internal_rev": group["totals"]["internal_rev"],
            "internal_cost": group["totals"]["internal_cost"],
            "net_sum": group["totals"]["net_sum"],
            "internal_profit": group["totals"]["internal_profit"],
            "minority": group["totals"]["minority"],
            "lights": group["lights"],
        },
        "tax": {
            "revenue_total": tax["revenue_total"],
            "deduction_a": tax["deduction_a"], "deduction_b": tax["deduction_b"],
            "deductible_total": tax["deductible_total"],
            "unsold_deduction": tax["unsold_deduction"],
            "groups": [
                {k: g[k] for k in ("name", "revenue", "deduction", "appreciation", "ratio", "tax")}
                for g in tax["groups"]
            ],
            "tax_total": tax["tax_total"], "tax_b_total": tax["tax_b_total"],
            "prepaid": tax["prepaid"], "payable": tax["payable_total"],
            "ordinary_ratio": tax["ordinary"]["ratio"],
            "ordinary_exempt": tax["ordinary"]["exempt"],
            "scenarios": tax["scenarios"],
        },
        "risk": {"score": risk["overall"]["score"], "level": risk["overall"]["level"]},
        "case": {
            "revenue": case_ctx["c"]["revenue"],
            "gm": case_ctx["c"]["gm"],
            "attributable": case_ctx["c"]["attributable"],
            "ocf": case_ctx["c"]["ocf"],
            "etr": case_ctx["c"]["etr"],
            "om": case_ctx["c"]["om"],
            "pm": case_ctx["c"]["pm"],
            "trivial": case_ctx["c"]["trivial"],
            "segment_revenue_sum": case_ctx["c"]["segment_revenue_sum"],
            "segment_gap": case_ctx["c"]["segment_gap"],
        },
    }
    (DATA / "derived.json").write_text(
        json.dumps(derived, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print("[data] 派生值 → data/derived.json")
    return rc


if __name__ == "__main__":
    raise SystemExit(main())
