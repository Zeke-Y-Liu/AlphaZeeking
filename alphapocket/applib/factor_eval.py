"""P1 · 因子补齐验收工具（《黑箱框架对照与实施计划》P1）

两项落盘证据（对应 P1 验收栏）：

1. **增量价值关**（P1.1/P1.2）：新 alpha 因子与 ``mom_12_1`` 的截面相关性 |ρ| < 0.6，
   否则视为重复暴露（季度巡检对象）。截面 ρ = 月末观测 × 全标的的 Spearman 秩相关，
   逐月求均值（因子为截面量；与 walkforward 的截面 IC 同源算法，PIT 重算）。
2. **A/B 条件门对照**（P1.4）：引擎 A 买入需引擎 B 不持卖出信号（``ab_gate``）——
   增强组合（条件门）vs 基准组合（无条件门）：累计收益增强 > 1.5pct 且最大回撤不恶化。

产物：``sandbox/artifacts/p1_<ts>/{p1_eval.json, p1_eval.md, p1_eval.html}``
边界：α universe 仅 4–9 只且各属不同行业 → 截面相关性功效弱，结论以「机制成立 + 待全量
历史补强」表述（诚实原则），不静默放大。
"""
from __future__ import annotations

import json
import time
from pathlib import Path

from . import backtest as BT
from .engines.base import factor_entries
from .walkforward import _factor_value, _month_end_dates, _spearman

INCR_THRESHOLD = 0.60      # 增量价值关：|ρ| < 0.6
INCR_BASE = "mom_12_1"     # 对照基准因子
AB_MIN_GAIN = 0.015        # P1.4：累计收益增强阈值（1.5pct）
AB_MAX_DD_WORSEN = 1e-9    # P1.4：最大回撤不恶化（恶化容差）


# ── 增量价值关（P1.1/P1.2）──────────────────────────────
def cross_section_corr(fa: dict, fb: dict, secids: list[str], bars: dict, fund: dict,
                       start: str | None = None, end: str | None = None) -> dict:
    """两因子的逐月截面 Spearman 秩相关（PIT 重算）→ 均值 / 极值。"""
    all_dates = sorted({r["dt"] for s in secids for r in bars[s]})
    vdays = [d for d in all_dates
             if (not start or d >= start) and (not end or d <= end)]
    rhos = []
    for d in _month_end_dates(vdays):
        xa, xb = [], []
        for s in secids:
            fd = fund.get(s, {})
            va = _factor_value(fa, s, bars[s], fd, d)
            vb = _factor_value(fb, s, bars[s], fd, d)
            if va is not None and vb is not None:
                xa.append(va)
                xb.append(vb)
        r = _spearman(xa, xb)
        if r is not None:
            rhos.append({"dt": d, "rho": r, "n": len(xa)})
    if not rhos:
        return {"n_dates": 0, "mean_rho": None, "max_abs": None, "rows": []}
    mean = sum(r["rho"] for r in rhos) / len(rhos)
    return {"n_dates": len(rhos), "mean_rho": mean,
            "max_abs": max(abs(r["rho"]) for r in rhos), "rows": rhos}


def incremental_checks(base_meta: dict, factor_ids: list[str], registry: dict,
                       secids: list[str], bars: dict, fund: dict,
                       start: str | None = None, end: str | None = None) -> dict:
    """逐一核对新因子 vs 基准因子（默认 mom_12_1）的 |ρ| < 阈值。"""
    meta_by = {f["id"]: f for f in factor_entries(registry)}
    rows = []
    for fid in factor_ids:
        meta = meta_by.get(fid)
        if meta is None:
            rows.append({"factor": fid, "error": "因子未注册"})
            continue
        c = cross_section_corr(meta, base_meta, secids, bars, fund, start, end)
        mr = c["mean_rho"]
        rows.append({"factor": fid, "name": meta.get("name"), "base": base_meta["id"],
                     "n_dates": c["n_dates"], "mean_rho": mr, "max_abs": c["max_abs"],
                     "pass": (mr is not None and abs(mr) < INCR_THRESHOLD),
                     "rows": c["rows"]})
    n_ok = sum(1 for r in rows if r.get("pass"))
    return {"base": base_meta["id"], "threshold": INCR_THRESHOLD, "n": len(rows), "n_pass": n_ok,
            "all_pass": n_ok == len(rows) and len(rows) > 0, "rows": rows}


# ── A/B 条件门对照（P1.4）───────────────────────────────
def ab_compare(bars: dict, pf: dict, *, rebalance: str = "monthly", cost_bps: float = 2.5,
               weighting: str = "equal", start: str | None = None, end: str | None = None,
               gate_damp: float = BT.GATE_DAMP_DEFAULT,
               gate_redistribute: bool = False) -> dict:
    """同一组合加/不加条件门回测对照 → 增强 / 回撤恶化判定（软衰减口径）。"""
    keys = ("total_return", "cagr", "ann_vol", "sharpe", "max_dd", "calmar")
    base = BT.run_backtest(bars, pf, start=start, end=end, rebalance=rebalance,
                           cost_bps=cost_bps, weighting=weighting, ab_gate=False)
    enh = BT.run_backtest(bars, pf, start=start, end=end, rebalance=rebalance,
                          cost_bps=cost_bps, weighting=weighting, ab_gate=True,
                          gate_damp=gate_damp, gate_redistribute=gate_redistribute)
    bm, em = base["strategy_metrics"], enh["strategy_metrics"]
    gain = em["total_return"] - bm["total_return"]
    dd_improvement = em["max_dd"] - bm["max_dd"]      # > 0 表示回撤改善（更接近 0）
    gain_ok = gain > AB_MIN_GAIN
    dd_ok = dd_improvement >= -AB_MAX_DD_WORSEN
    return {
        "portfolio": pf["id"], "portfolio_name": pf["name"],
        "rebalance": rebalance, "cost_bps": cost_bps, "weighting": weighting,
        "gate_damp": gate_damp, "gate_redistribute": gate_redistribute,
        "base": {k: bm[k] for k in keys}, "enhanced": {k: em[k] for k in keys},
        "gain_total_return": gain, "dd_improvement": dd_improvement,
        "gate": enh.get("gate"),
        "threshold": {"min_gain": AB_MIN_GAIN},
        "dod": {"gain_ok": gain_ok, "dd_ok": dd_ok, "ok": gain_ok and dd_ok,
                "detail": (f"累计收益增强 {gain * 100:+.2f}pct（阈值 +{AB_MIN_GAIN * 100:.1f}pct）；"
                           f"最大回撤 {'改善' if dd_improvement > 0 else '持平' if abs(dd_improvement) < 1e-9 else '恶化'}"
                           f" {abs(dd_improvement) * 100:.2f}pct")},
    }


# ── 编排 ────────────────────────────────────────────────
P1_ALPHA_FACTORS = ["rev_20d", "rev_60d", "qua_leverage", "qua_accrual", "qua_fcf"]


def run_p1_eval(pids: list[str], bars: dict, fund: dict, registry: dict,
                pf_loader, start: str | None = None, end: str | None = None,
                gate_damp: float = BT.GATE_DAMP_DEFAULT,
                gate_redistribute: bool = False) -> dict:
    """P1 验收总装：增量相关（新 alpha 因子 vs mom_12_1）+ 组合级 A/B 条件门对照。"""
    meta_by = {f["id"]: f for f in factor_entries(registry)}
    secids = [s for s, arr in bars.items() if arr]
    incr = incremental_checks(meta_by[INCR_BASE], P1_ALPHA_FACTORS, registry,
                              secids, bars, fund, start, end)
    ab = []
    for pid in pids:
        pf = pf_loader(pid)
        if pf is None:
            ab.append({"portfolio": pid, "error": "组合不存在"})
            continue
        ab.append(ab_compare(bars, pf, start=start, end=end,
                             gate_damp=gate_damp, gate_redistribute=gate_redistribute))
    ab_ok = bool(ab) and all(a.get("dod", {}).get("ok") for a in ab)
    return {
        "generated": time.strftime("%Y-%m-%dT%H:%M:%S"), "code_version": "P1",
        "start": start, "end": end, "portfolios": pids,
        "gate_config": {"gate_damp": gate_damp, "gate_redistribute": gate_redistribute},
        "incremental": incr,
        "ab": ab,
        "dod": {"incremental": incr["all_pass"], "ab_gate": ab_ok,
                "ok": incr["all_pass"] and ab_ok},
        "notes": [
            "增量价值关：新 alpha 因子与 mom_12_1 的逐月截面 Spearman 秩相关均值 |ρ| < 0.6。",
            ("A/B 条件门：引擎 A 买入需引擎 B 不持卖出信号（PIT 重算、多信号一致触发），"
            "触发标的软衰减（权重 ×gate_damp，释放额度留现金）；增强累计收益 > 1.5pct 且回撤不恶化。"),
            "因子取值按 as_of PIT 重算（与 walkforward 同源口径），月末观测日对齐。",
            (f"截面样本：{len(secids)} 只标的（α universe），各属不同行业 → 相关性功效弱，"
            "结论以「机制成立 + 待 2016 起全量历史补强」表述（诚实原则）。"),
        ],
    }


# ── 渲染 ────────────────────────────────────────────────
def _pct(v, d=2):
    return "—" if v is None else f"{v * 100:+.{d}f}%"


def _num(v, d=3):
    return "—" if v is None else f"{v:.{d}f}"


def _cls(v):
    return "up" if (v or 0) > 0 else "down" if (v or 0) < 0 else ""


def render_md(rep: dict) -> str:
    inc, ab = rep["incremental"], rep["ab"]
    L = ["# P1 因子补齐 · 验收报告", ""]
    L.append(f"生成 `{rep['generated']}` · 区间 `{rep.get('start') or '全样本'} ~ {rep.get('end') or '最新'}` · "
             f"组合 {', '.join(rep['portfolios'])}")
    L.append("")
    L.append("**DoD 自检**：增量价值关 " + ("✅" if rep["dod"]["incremental"] else "❌")
             + " · A/B 条件门 " + ("✅" if rep["dod"]["ab_gate"] else "❌"))
    L.append("")
    L.append(f"## ① 增量价值关（vs `{inc['base']}`，阈值 |ρ| < {inc['threshold']}）")
    L.append("")
    L.append("| 新因子 | 名称 | 观测月数 | 均值 ρ | 最大 |ρ| | 判定 |")
    L.append("| --- | --- | --- | --- | --- | --- |")
    for r in inc["rows"]:
        if r.get("error"):
            L.append(f"| {r['factor']} | — | — | — | — | ❌ {r['error']} |")
            continue
        L.append(f"| {r['factor']} | {r['name']} | {r['n_dates']} | {_num(r['mean_rho'])} "
                 f"| {_num(r['max_abs'])} | {'✅ 通过' if r['pass'] else '❌ 超阈'} |")
    L.append("")
    L.append("## ② A/B 条件门对照（引擎 A 买入需引擎 B 不持卖出信号）")
    L.append("")
    for a in ab:
        if a.get("error"):
            L.append(f"- `{a['portfolio']}`：{a['error']}")
            continue
        b, e = a["base"], a["enhanced"]
        L.append(f"### {a['portfolio_name']}（{a['portfolio']}）")
        L.append("")
        L.append("| 指标 | 基准（无条件门） | 增强（条件门） |")
        L.append("| --- | --- | --- |")
        for lab, k, f in [("累计收益", "total_return", _pct), ("年化收益", "cagr", _pct),
                          ("年化波动", "ann_vol", _pct), ("夏普", "sharpe", _num),
                          ("最大回撤", "max_dd", _pct), ("卡玛", "calmar", _num)]:
            L.append(f"| {lab} | {f(b[k])} | {f(e[k])} |")
        g = a["gate"] or {}
        L.append("")
        L.append(f"- 累计收益增强 **{a['gain_total_return'] * 100:+.2f}pct**（阈值 +{a['threshold']['min_gain'] * 100:.1f}pct）"
                 f" · 回撤{'改善' if a['dd_improvement'] > 0 else '持平' if abs(a['dd_improvement']) < 1e-9 else '恶化'} "
                 f"{abs(a['dd_improvement']) * 100:.2f}pct → {'✅ 达标' if a['dod']['ok'] else '❌ 未达标'}")
        if g:
            L.append(f"- 门事件：{g['n_rebalances']} 个再平衡时点 · 平均持仓比 {g['avg_invested_ratio']:.2f}"
                     f"（衰减×{g['gate_damp']:g}{'，再分配' if g['redistribute'] else ''}）"
                     f" · 累计触发 {g['n_flagged']} 次 · 全降仓时点 {g['n_fully_derisked']}")
        L.append("")
    L.append("## ③ 说明与边界")
    L.append("")
    for n in rep["notes"]:
        L.append(f"- {n}")
    L.append("")
    return "\n".join(L)


_CSS = """body{font-family:-apple-system,"PingFang SC","Microsoft YaHei",sans-serif;background:#FAF7F0;color:#2C2820;
max-width:1000px;margin:0 auto;padding:28px 20px;line-height:1.6}
h1{font-size:22px;border-bottom:2px solid #A93226;padding-bottom:8px}
h2{font-size:16px;margin-top:26px;color:#8C2F24}h3{font-size:13.5px;margin:18px 0 6px}
.meta{font-size:12px;color:#7A7060;margin:6px 0}
.card{background:#fff;border:1px solid #E5DECF;border-radius:10px;padding:14px 16px;margin:10px 0}
table{border-collapse:collapse;width:100%;font-size:12px}
th,td{border:1px solid #E5DECF;padding:5px 8px;text-align:right}
th{background:#F3EEE2;text-align:center}td:first-child,th:first-child{text-align:left}
.mono{font-variant-numeric:tabular-nums}.up{color:#A93226}.down{color:#1E8449}
.flag{color:#A93226;font-weight:600}.ok{color:#1E8449;font-weight:600}
.warn{background:#FCF3E8;border-left:3px solid #D68910;padding:8px 12px;font-size:12px;border-radius:6px;margin:10px 0}
.disc{margin-top:26px;font-size:11px;color:#9C9585;border-top:1px solid #E5DECF;padding-top:12px}"""


def render_html(rep: dict) -> str:
    inc, ab = rep["incremental"], rep["ab"]
    inc_rows = ""
    for r in inc["rows"]:
        if r.get("error"):
            inc_rows += (f'<tr><td>{r["factor"]}</td><td>—</td><td>—</td><td>—</td><td>—</td>'
                         f'<td><span class="flag">❌ {r["error"]}</span></td></tr>')
            continue
        inc_rows += (f'<tr><td class="mono">{r["factor"]}</td><td>{r["name"]}</td>'
                     f'<td class="mono">{r["n_dates"]}</td><td class="mono {_cls(r["mean_rho"])}">{_num(r["mean_rho"])}</td>'
                     f'<td class="mono">{_num(r["max_abs"])}</td>'
                     f'<td>{"<span class=ok>✅ 通过</span>" if r["pass"] else "<span class=flag>❌ 超阈</span>"}</td></tr>')
    ab_blocks = ""
    for a in ab:
        if a.get("error"):
            ab_blocks += f'<div class="card">{a["portfolio"]}：{a["error"]}</div>'
            continue
        b, e = a["base"], a["enhanced"]
        rows = "".join(f'<tr><td>{lab}</td><td class="mono">{f(b[k])}</td><td class="mono">{f(e[k])}</td></tr>'
                       for lab, k, f in [("累计收益", "total_return", _pct), ("年化收益", "cagr", _pct),
                                         ("年化波动", "ann_vol", _pct), ("夏普", "sharpe", _num),
                                         ("最大回撤", "max_dd", _pct), ("卡玛", "calmar", _num)])
        g = a["gate"] or {}
        gtxt = (f'门事件 {g["n_rebalances"]} 个再平衡时点 · 平均持仓比 {g["avg_invested_ratio"]:.2f}'
                f'（衰减×{g["gate_damp"]:g}{"，再分配" if g["redistribute"] else ""}）'
                f' · 累计触发 {g["n_flagged"]} 次 · 全降仓时点 {g["n_fully_derisked"]}'
                if g else '门事件：无')
        ab_blocks += (f'<h3>{a["portfolio_name"]}（{a["portfolio"]}）</h3><div class="card">'
                      f'<table><tr><th>指标</th><th>基准（无条件门）</th><th>增强（条件门）</th></tr>{rows}</table>'
                      f'<div class="meta">累计收益增强 <b>{a["gain_total_return"] * 100:+.2f}pct</b>'
                      f'（阈值 +{a["threshold"]["min_gain"] * 100:.1f}pct）· 回撤'
                      f'{"改善" if a["dd_improvement"] > 0 else "持平" if abs(a["dd_improvement"]) < 1e-9 else "恶化"}'
                      f' {abs(a["dd_improvement"]) * 100:.2f}pct → '
                      f'{"<span class=ok>✅ 达标</span>" if a["dod"]["ok"] else "<span class=flag>❌ 未达标</span>"}<br>{gtxt}</div></div>')
    dod = rep["dod"]
    return f"""<!DOCTYPE html><html lang="zh-CN"><head><meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>AlphaPocket · P1 因子补齐验收</title><style>{_CSS}</style></head><body>
<h1>P1 · 因子补齐验收报告</h1>
<div class="meta">生成 <b>{rep['generated']}</b> · 区间 <b>{rep.get('start') or '全样本'} ~ {rep.get('end') or '最新'}</b> ·
组合 {', '.join(rep['portfolios'])}<br>
DoD：增量价值关 {'✅' if dod['incremental'] else '❌'} · A/B 条件门 {'✅' if dod['ab_gate'] else '❌'}</div>

<h2>① 增量价值关 <em>Incremental Value vs {inc['base']}</em></h2>
<div class="card"><div class="meta">阈值 |ρ| &lt; {inc['threshold']}（与 {inc['base']} 的逐月截面 Spearman 秩相关均值）</div>
<table><tr><th>新因子</th><th>名称</th><th>观测月数</th><th>均值 ρ</th><th>最大 |ρ|</th><th>判定</th></tr>{inc_rows}</table></div>

<h2>② A/B 条件门对照 <em>Conditional Gate</em></h2>
{ab_blocks}

<h2>③ 说明与边界 <em>Notes</em></h2>
<div class="card"><ul style="font-size:12px;margin:0;padding-left:18px">
{''.join(f'<li>{n}</li>' for n in rep['notes'])}</ul></div>
<div class="disc"><b>合规与复现</b>：数据来自公开来源（AKShare 聚合），仅供研究参考，不构成投资建议。
复现：<code>ap p1-eval --portfolio all</code></div></body></html>"""


def write_all(rep: dict, art_dir: Path) -> dict:
    art_dir.mkdir(parents=True, exist_ok=True)
    jp, mp, hp = art_dir / "p1_eval.json", art_dir / "p1_eval.md", art_dir / "p1_eval.html"
    jp.write_text(json.dumps(rep, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    mp.write_text(render_md(rep), encoding="utf-8")
    hp.write_text(render_html(rep), encoding="utf-8")
    return {"json": str(jp), "md": str(mp), "html": str(hp)}
