"""报告生成（V4-α 真实数据版）：HTML（纸质档案风）+ Markdown（供 agent 解读）

模板 cn4_review：①数据与验证 ②组合体检 ②·5宏观流动性(引擎C·不决策) ③中长期因子评估(引擎A)
④短期20日评估(引擎B) ⑤下一交易日指引 ⑥变更/执行/风险建议 —— 全部数字来自 AKShare 真实数据，可复现。
"""
from __future__ import annotations
import json
from pathlib import Path
from .ctx import Ctx

CSS = """
*{margin:0;padding:0;box-sizing:border-box}
:root{--paper:#EFEBE2;--card:#FBFAF6;--card2:#F5F2EA;--line:#DCD5C6;--ink:#1C1B18;--dim:#6E6859;
--mute:#9C9585;--seal:#A93226;--up:#C0392B;--down:#1E7A5E;--blue:#2C5F8A;
--serif:'Noto Serif SC','Songti SC',Georgia,serif;--sans:'Noto Sans SC','PingFang SC',sans-serif;
--mono:'JetBrains Mono',ui-monospace,Menlo,monospace}
body{background:var(--paper);color:var(--ink);font-family:var(--sans);padding:34px 26px 60px;max-width:920px;margin:0 auto}
h1{font-family:var(--serif);font-size:25px;font-weight:600;letter-spacing:-.01em}
.meta{font-family:var(--mono);font-size:9.5px;color:var(--mute);letter-spacing:.08em;margin:9px 0 4px;line-height:1.9}
.meta b{color:var(--seal);font-weight:500}
h2{font-family:var(--serif);font-size:16px;font-weight:600;margin:30px 0 11px;padding-left:11px;border-left:3px solid var(--seal)}
h2 em{font-style:normal;font-family:var(--mono);font-size:8.5px;color:var(--mute);letter-spacing:.14em;margin-left:8px;text-transform:uppercase}
.card{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:15px 17px;margin-bottom:11px}
.grid{display:grid;grid-template-columns:repeat(3,1fr);gap:1px;background:var(--line);border:1px solid var(--line);border-radius:9px;overflow:hidden}
.grid.g4{grid-template-columns:repeat(4,1fr)}
.grid.g6{grid-template-columns:repeat(6,1fr)}
.grid div{background:var(--card);padding:11px 10px}
.grid .k{font-size:9.5px;color:var(--mute);margin-bottom:5px}
.grid .v{font-family:var(--mono);font-size:14.5px;font-weight:500}
.up{color:var(--up)}.down{color:var(--down)}
table{width:100%;border-collapse:collapse;font-size:11px}
th{font-family:var(--mono);font-size:8.5px;color:var(--mute);letter-spacing:.06em;text-align:left;padding:6px 7px;border-bottom:1px solid var(--line);text-transform:uppercase}
td{padding:7.5px 7px;border-bottom:1px solid rgba(28,27,24,.07);vertical-align:top}
.mono{font-family:var(--mono)}
.flag{display:inline-block;font-family:var(--mono);font-size:7.5px;color:var(--seal);border:1px dashed rgba(169,50,38,.28);border-radius:3px;padding:1px 4px;margin:1px 2px}
.flag.gray{color:var(--mute);border-color:#C9C1B0}
.g{font-family:var(--mono);font-size:10px;display:inline-block;padding:2.5px 6px;border-radius:4px;margin-right:2.5px}
.gA{color:var(--down);background:rgba(30,122,94,.1)}.gB{color:#4C7A5E;background:rgba(30,122,94,.05)}
.gC{color:#8A6D2F;background:rgba(178,140,60,.09)}.gD{color:#B05A4A;background:rgba(196,105,92,.1)}
.sig{display:flex;gap:11px;align-items:flex-start;padding:10px 0;border-bottom:1px solid rgba(28,27,24,.07)}
.sig .tag{font-family:var(--mono);font-size:8px;border:1px solid;border-radius:3px;padding:2.5px 7px;flex:0 0 auto;margin-top:2px}
.tSell{color:var(--up);border-color:rgba(192,57,43,.4);background:rgba(192,57,43,.06)}
.tHold{color:var(--down);border-color:rgba(30,122,94,.35);background:rgba(30,122,94,.06)}
.tInfo{color:#8A6D2F;border-color:rgba(178,140,60,.4);background:rgba(178,140,60,.07)}
.sig .tx b{font-size:11.5px;display:block;margin-bottom:2px}
.sig .tx span{font-family:var(--mono);font-size:9px;color:var(--mute)}
.adv{border-radius:10px;padding:14px 16px;margin-bottom:11px;border:1px solid}
.adv h3{font-family:var(--serif);font-size:13.5px;margin-bottom:9px}
.advChg{background:rgba(44,95,138,.05);border-color:#9CBBD6}
.advExe{background:rgba(30,122,94,.05);border-color:#8FC7B4}
.advRisk{background:rgba(169,50,38,.05);border-color:rgba(169,50,38,.3)}
.warn{display:flex;gap:9px;font-size:11px;line-height:1.75;color:var(--dim);background:var(--card2);border:1px dashed #C9C1B0;border-radius:8px;padding:10px 13px;margin-top:10px}
.warn b{color:var(--seal)}
.disc{margin-top:26px;font-size:10px;line-height:1.8;color:var(--mute);border-top:1px solid var(--line);padding-top:13px}
code{font-family:var(--mono);font-size:10px;background:var(--card2);border:1px solid var(--line);border-radius:4px;padding:1px 5px}
.ok{color:var(--down);font-family:var(--mono)}
.pctbar{height:5px;background:#E8E3D8;border-radius:99px;overflow:hidden;margin-top:3px}
.pctbar i{display:block;height:100%;background:var(--blue);border-radius:99px}
"""

VGRADE = {"A": "gA", "B": "gB", "B+": "gB", "C": "gC", "C+": "gC", "D": "gD", "D+": "gD"}

# ── 引擎C 宏观流动性 §2.5（T-C4 集成层）────────────────────
# 温度计三态+icon 映射（复用 data_risk 视觉语言：极端档 .flag 醒目 / 中间档次级 / unavailable 灰条）
MACRO_ICON = {2: "▲▲", 1: "▲", 0: "●", -1: "▼", -2: "▼▼"}


def _macro_zh(lvl):
    from .factors import MACRO_LEVEL_ZH
    return MACRO_LEVEL_ZH.get(lvl, str(lvl)) if isinstance(lvl, int) else "unavailable"


def _macro_chip(lvl) -> str:
    """档位徽章：|±2| 极端醒目（seal 红）→ ±1 次级（蓝/棕）→ 0 中性 → unavailable 灰条。"""
    if not isinstance(lvl, int):
        return '<span class="flag gray">◇ unavailable</span>'
    if abs(lvl) == 2:
        return f'<span class="flag">{MACRO_ICON[lvl]} {_macro_zh(lvl)}</span>'
    if lvl == 1:
        return f'<span class="flag" style="color:#2C5F8A;border-color:#9CBBD6">{MACRO_ICON[lvl]} 偏松</span>'
    if lvl == -1:
        return f'<span class="flag" style="color:#8A6D2F;border-color:#C9AE7A">{MACRO_ICON[lvl]} 偏紧</span>'
    return '<span class="flag gray">● 中性</span>'


def macro_section_html(review) -> str:
    """§2.5 宏观流动性 HTML：三市场温度计 + 全局概览 + 指标明细 + 口径注记（单文件零外部依赖）。"""
    m = review.get("macro_liquidity") or {"status": "unavailable", "reason": "无数据"}
    if m.get("status") == "unavailable":
        return f"""<h2>②·5 宏观流动性<em>Engine C · Macro Liquidity</em></h2>
<div class="card"><span class="flag gray">◇ unavailable</span>
<div class="meta" style="padding:6px 0">透明降级（不阻断评估）：{m.get('reason', 'unavailable')}</div>
<div class="warn"><span>◇</span><div>宏观流动性数据缺失区别于行情数据——<b>不进门禁、评估不失败</b>；
采集入口：<code>fetch_macro_liquidity</code>（ap fetch 宏观独立采集）+ <code>ap clone</code> 后自动回填本节。</div></div></div>"""
    lv = m["markets"]
    grid = "".join(
        f"<div><div class='k'>{'全局' if k == 'G' else k + ' 市场'}</div>"
        f"<div class='v'>{'—' if k == 'G' and m['global_score'] is None else _macro_zh(lv[k]) if k != 'G' else _macro_zh(m['global'])}"
        f"<span style='font-size:9px;color:var(--mute)'> {lv[k] if k != 'G' else m['global']}"
        + (f" ｜score {m['global_score']}" if k == 'G' else "") + "</span></div></div>"
        for k in ("G", "A", "HK", "US"))
    rows = ""
    for sid, d in sorted(m.get("detail", {}).get("indicators", {}).items(),
                         key=lambda x: (x[1].get("market", ""), x[1].get("dim", ""), x[0])):
        if d.get("score") is None:
            rows += (f"<tr><td>{d.get('market')}</td><td>{d.get('dim')}</td><td><b>{sid}</b></td>"
                     f"<td colspan='3' class='meta'>—（{d.get('path')}，透明标注）</td></tr>")
        else:
            rows += (f"<tr><td>{d['market']}</td><td>{d['dim']}</td><td><b>{sid}</b></td>"
                     f"<td class='mono'>{d['pctile']:.3f}{(' ⟲' if d.get('stale') else '')}</td>"
                     f"<td class='mono'>{_num(d['score'])}</td><td>{_macro_chip(d['level'])}</td></tr>")
    n = m.get("notes") or {}
    return f"""<h2>②·5 宏观流动性<em>Engine C · Macro Liquidity · 不决策</em></h2>
<div class="card"><div class="grid" style="grid-template-columns:repeat(4,1fr)">{grid}</div>
<table style="margin-top:10px"><tr><th>市场</th><th>维度</th><th>指标</th><th>分位(新)</th><th>score</th><th>档位</th></tr>{rows}</table>
<div class="warn"><span>◇</span><div><b>市场条件变量</b>：不决策、不参与权重（引擎C 与引擎A 评分、引擎B 信号三线并列展示）。
口径 {n.get('scorer_version', '—')} · as_of <code>{m.get('as_of')}</code>（与评估同日，组合 K 线口径）·
来源 {'、'.join(n.get('institutions', ['—']))}。</div></div></div>"""


def macro_section_md(review) -> str:
    """§2.5 宏观流动性 MD 小节（与 HTML 同口径）。"""
    m = review.get("macro_liquidity") or {"status": "unavailable", "reason": "无数据"}
    if m.get("status") == "unavailable":
        return (f"\n## ②·5 宏观流动性（引擎C）\n- 透明降级（不阻断评估）：{m.get('reason', 'unavailable')}"
                "\n- 宏观数据缺失区别于行情数据：不进门禁、评估不失败；采集后自动回填本节\n")
    lv = m["markets"]
    L = [f"\n## ②·5 宏观流动性（引擎C · 不决策）",
         f"- 全局 {_macro_zh(m['global'])}（{m['global']}）· global_score {m['global_score']} · as_of {m['as_of']}（与评估同日）",
         f"- A {_macro_zh(lv['A'])}（{lv['A']}）｜HK {_macro_zh(lv['HK'])}｜US {_macro_zh(lv['US'])}",
         "\n| 市场 | 维度 | 指标 | 分位(新) | score | 档位 |\n| --- | --- | --- | --- | --- | --- |"]
    for sid, d in sorted(m.get("detail", {}).get("indicators", {}).items(),
                         key=lambda x: (x[1].get("market", ""), x[1].get("dim", ""), x[0])):
        if d.get("score") is None:
            L.append(f"| {d.get('market')} | {d.get('dim')} | {sid} | — | — | —（{d.get('path')}） |")
        else:
            tag = "⟲" if d.get("stale") else ""
            L.append(f"| {d['market']} | {d['dim']} | {sid} | {d['pctile']:.3f}{tag} "
                     f"| {_num(d['score'])} | {_macro_zh(d['level'])} |")
    n = m.get("notes") or {}
    L.append(f"\n- 口径 {n.get('scorer_version', '—')} · 来源 {'、'.join(n.get('institutions', ['—']))}")
    L.append("- 纪律：市场条件变量——不决策、不参与权重（引擎C 与引擎A 评分、引擎B 信号三线并列展示）\n")
    return "\n".join(L)


def _g(v):
    if not v or v == "—":
        return '<span style="color:var(--mute)">—</span>'
    return f'<span class="g {VGRADE.get(v[0], "gC")}">{v}</span>'


def _pct(v, digits=1):
    if v is None:
        return "—"
    return f"{v * 100:.{digits}f}%"


def _num(v, digits=2):
    return "—" if v is None else f"{v:.{digits}f}"


# ── R4：信号延迟衰减 caveat（fast_decay 信号在报告中挂警示）──
def decay_caveat_md(review) -> str:
    cs = review.get("decay_caveats") or []
    if not cs:
        return ""
    head = "> ⚠ **信号延迟衰减 caveat（R4）**：以下引擎 B 信号 T+1 开盘执行衰减过快（> 50%），registry 已置 weakening，其提示仅供参考，不构成择时依据："
    L = ["", head]
    for c in cs:
        L.append(f"> - `{c['signal_id']}`（{c.get('name') or ''}）：T+1 开盘衰减 "
                 f"{_pct(c.get('decay_rate'))}（事件 {c.get('n_events')}）")
    L.append("")
    return "\n".join(L)


def decay_caveat_html(review) -> str:
    cs = review.get("decay_caveats") or []
    if not cs:
        return ""
    items = "".join(f'<div><code>{c["signal_id"]}</code>（{c.get("name") or ""}）：T+1 开盘衰减 '
                    f'{_pct(c.get("decay_rate"))} · 事件 {c.get("n_events")}</div>' for c in cs)
    return ('<div class="warn"><span>⚠</span><div><b>信号延迟衰减 caveat（R4）</b>：以下引擎 B 信号 '
            'T+1 开盘执行衰减过快（&gt; 50%），registry 已置 weakening，其提示仅供参考，不构成择时依据：'
            + items + '</div></div>')


def _cls(v):
    return "up" if (v or 0) > 0 else "down" if (v or 0) < 0 else ""


def _verify_rows(verify):
    rows = ""
    for k, v in verify.items():
        ok = str(v).startswith("pass")
        rows += (f'<tr><td class="mono">{k}</td><td class="{"ok" if ok else "up"}">{"✓ " if ok else "✗ "}{v}</td></tr>')
    return rows


def _src_rows(prov):
    rows = ""
    for s in prov["sources"]:
        rows += (f'<tr><td class="mono">{s["dataset"]}</td><td><code>{s["interface"]}</code></td>'
                 f'<td>{s["upstream"]}</td><td class="mono">{json.dumps(s["params"], ensure_ascii=False)}</td>'
                 f'<td class="mono" style="font-size:9px">{s["sha256"][:16]}…</td></tr>')
    return rows


def _guidance(p):
    b, a = p["beta"], p["alpha"]
    name = p["name"]
    if b["state"] == "below":
        head = f'<span class="tag tSell">预警</span>'
        body = (f"收盘已低于 MA20（{_num(b['ma20'])}）达 <b>{_pct(b['ma20_break'])}</b>（±2% 带外）。"
                f"下一交易日观察能否收复 MA20 {_num(b['ma20'])}；持续处于带外则进入减仓评估单流程（人工确认，引擎不自动卖出）。"
                f"参考：带外确认位 {_num(b['band_low'])} · 20日低点 {_num(b['low20'])}。")
    elif b["state"] == "above":
        head = f'<span class="tag tHold">趋势上方</span>'
        body = (f"收盘高于 MA20 达 <b>{_pct(b['ma20_break'])}</b>，20日动量 {_pct(b['mom_20d'])}。持有为主；"
                f"回踩执行参考带 <b>[{_num(b['band_low'])}, {_num(b['band_high'])}]</b>（MA20±1.5σ，σ为20日日波动）。"
                f"有效收盘跌破 {_num(b['band_low'])} 转预警；突破 20 日高点 {_num(b['high20_intra'])} 确认强势。")
    else:
        head = f'<span class="tag tInfo">中性区</span>'
        body = (f"收盘处于 MA20 ±2% 带内（{_pct(b['ma20_break'])}）。无操作指令；关注带边界突破方向："
                f"上沿 {_num(b['band_high'])} / 下沿 {_num(b['band_low'])}，突破 20 日高点 {_num(b['high20_intra'])} 确认强势。")
    val_note = ""
    if a.get("val_pe_pct") is not None:
        val_note = (f"估值：PE(TTM) {_num(a['val_pe_ttm'])}（五年{_pct(a['val_pe_pct'],0)}分位）· "
                    f"PB {_num(a['val_pb'])}（{_pct(a.get('val_pb_pct'),0)}分位）· 股息率(TTM) {_pct(a['div_yield_ttm'])}。")
    return f"""<div class="sig">{head}<div class="tx"><b>{name} <span class="mono" style="font-size:9px;color:var(--mute)">{p['secid']}</span></b>
<span>{body}</span><span>{val_note}</span></div></div>"""


def render_html(review, plan, ctx: Ctx, run_id: str, code_ver, ds_commit, verify) -> str:
    prov = json.loads((ctx.local_repo / "market" / "provenance.json").read_text(encoding="utf-8"))
    # ── 组合通用描述（股票/基金组合自适应）──
    positions = review["positions"]
    n_pos = len(positions)
    w0 = positions[0]["weight"] if positions else 0
    upstreams = " · ".join(sorted({s["upstream"] for s in prov["sources"]}))
    has_fund_pos = any(not p["alpha"].get("used_period") for p in positions)
    has_stock = any(p.get("asset_type") == "STOCK" for p in positions)
    qdii = [p["name"] for p in positions if "QDII" in p.get("industry", "")]
    pe_ps = [p for p in positions if p["alpha"].get("val_pe_pct") is not None]
    amix = {}
    for p in positions:
        k = {"STOCK": "A股股票", "ETF": "ETF基金", "LOF": "LOF基金"}.get(p.get("asset_type", "STOCK"), "其他")
        amix[k] = amix.get(k, 0) + p["weight"]
    mix_txt = " / ".join(f"{k} {v:.0f}%" for k, v in amix.items())
    fx_txt = (f"全部 A 股上市、人民币计价；跨境品种（{'、'.join(qdii)}）注意场内溢价/折价" if qdii
              else "单一人民币计价，无汇率敞口")
    period_txt = "；".join(p["flags"][0] for p in positions)
    fund_note = ("基金类标的（ETF/LOF）无财务报告：估值/质量/成长维度不适用（—）且不参与五维综合；动量/波动为价格类因子照常计算。"
                 if has_fund_pos else "")
    exe_cost_note = (("A股 T+1 · 卖出印花税 0.05% + 佣金（假设万2.5）" if has_stock
                      else "ETF/LOF 交易免印花税 · T+0/T+1 按品种（见变更建议行）")
                     + " · 指令超时 10 个交易日强制复核")
    h = []
    h.append(f"""<!DOCTYPE html><html lang="zh-CN"><head><meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>AlphaPocket · {review['name']} 复盘报告 · {run_id}</title><style>{CSS}</style></head><body>""")

    h.append(f"""<h1>「{review['name']}」组合复盘报告</h1>
<div class="meta">RUN <b>{run_id}</b> · CODE <b>{code_ver}</b> · DATASET <b>{ds_commit}</b> · AS_OF <b>{review['as_of']}</b> · 下一交易日 <b>{review['next_trade_date']}</b><br>
数据源 <b>AKShare {prov['akshare_version']}</b>（{upstreams}）· RUNTIME quant-std (dir-sandbox) · 执行器 ap CLI · 报告模板 {review['id']}_review v1</div>""")

    # ① 数据与验证
    h.append(f"""<h2>① 数据与验证<em>Data &amp; Verification</em></h2>
<div class="card"><table><tr><th>数据集</th><th>接口</th><th>上游</th><th>参数</th><th>sha256</th></tr>{_src_rows(prov)}</table>
<div class="meta" style="margin-top:8px">取数时间 {prov['fetched_at']} · PIT 规则：{prov['pit_rules']['note']}</div></div>
<div class="card"><table><tr><th>验证项</th><th>结果</th></tr>{_verify_rows(verify)}</table>
<div class="warn"><span>◇</span><div>六项验证在任务执行后自动运行（<code>runner.VERIFY_SUITE</code>），任一红灯禁止 <code>ap push</code>；
独立复算命令：<code>python3 ap fetch &amp;&amp; python3 ap run tasks/review_{review['id']}.yaml</code>；点查：<code>ap serve</code> 后经 MCP <code>data.query</code>。</div></div></div>""")

    # ② 组合体检
    pm = review["portfolio_metrics"]
    h.append(f"""<h2>② 组合体检<em>Portfolio Health · 等权名义</em></h2>
<div class="card"><div class="grid g6">
<div><div class="k">当日收益</div><div class="v {_cls(pm['day_ret'])}">{_pct(pm['day_ret'])}</div></div>
<div><div class="k">近20日</div><div class="v {_cls(pm['ret_20d'])}">{_pct(pm['ret_20d'])}</div></div>
<div><div class="k">年初至今</div><div class="v {_cls(pm['ret_ytd'])}">{_pct(pm['ret_ytd'])}</div></div>
<div><div class="k">近一年</div><div class="v {_cls(pm['ret_1y'])}">{_pct(pm['ret_1y'])}</div></div>
<div><div class="k">一年最大回撤</div><div class="v down">{_pct(pm['max_dd_1y'])}</div></div>
<div><div class="k">持仓数</div><div class="v">{len(review['positions'])}</div></div>
</div>
<div class="meta" style="margin-top:9px">{pm['method']} · {review['note']} · 资产分布：{mix_txt}（{fx_txt}）</div></div>""")

    # ②·5 宏观流动性（引擎C，T-C4：透明降级不阻断评估）
    h.append(macro_section_html(review))

    # ③ 中长期因子评估（引擎 A）
    rows = ""
    for p in review["positions"]:
        a = p["alpha"]
        rows += f"""<tr><td><b>{p['name']}</b><br><span style="font-family:var(--mono);font-size:9px;color:var(--mute)">{p['secid']} · {p['industry']}</span></td>
<td class="mono">{_num(a['val_pe_ttm'])}<br><span style="font-size:9px;color:var(--mute)">{_pct(a['val_pe_pct'],0)}分位</span></td>
<td class="mono">{_num(a['val_pb'])}<br><span style="font-size:9px;color:var(--mute)">{_pct(a.get('val_pb_pct'),0)}分位</span></td>
<td class="mono">{_pct(a['div_yield_ttm'])}</td>
<td class="mono">{_num(a['qua_roe_ttm'],1)}%</td>
<td class="mono">{_num(a.get('qua_margin'),1)}%</td>
<td class="mono {_cls(a.get('grow_rev_yoy'))}">{_num(a.get('grow_rev_yoy'),1)}%</td>
<td class="mono {_cls(a.get('grow_np_yoy'))}">{_num(a.get('grow_np_yoy'),1)}%</td>
<td class="mono {_cls(a['mom_12_1'])}">{_pct(a['mom_12_1'])}</td>
<td class="mono">{_pct(a['vol_250'],0)}</td>
<td>{''.join(_g(v) for v in p['grades'].values())}<br><span style="font-size:9px;color:var(--mute)">综合 {_g(p['composite_grade'])}{f' {p['composite']}' if p['composite'] is not None else '（基金不适用）'}</span></td></tr>"""
    h.append(f"""<h2>③ 中长期因子评估<em>Engine A · 估值/质量/成长/动量/红利低波</em></h2>
<div class="card"><table><tr><th>标的</th><th>PE-TTM</th><th>PB</th><th>股息率</th><th>ROE-TTM</th><th>主营利润率</th>
<th>营收同比</th><th>净利同比</th><th>动量12-1</th><th>年化波动</th><th>五维·综合</th></tr>{rows}</table>
<div class="warn"><span>◇</span><div>估值分位为<b>自身五年历史分位</b>（PIT 重构：TTM EPS 按报告可用人时点阶梯对齐，报告期+披露窗口=Q1+30/H1+62/Q3+31/FY+120日）；
TTM EPS = 上年FY + 本年YTD − 上年同期YTD（摊薄口径，未逐笔校正股本变动）。
五维评分锚点：ROE [5→35,10→55,15→75,20→90]；成长(营收/净利均值) [0→40,10→60,20→75,35→90]；动量 [0→45,10→60,30→80]；
红利低波 = 股息[1→35,2→55,3→70,5→90]与低波[15%→100,30%→60,40%→40]各半；等级 A≥85 / B+≥70 / B≥60 / C+≥50 / C≥40 / D+≥30。
持仓财报期：{period_txt}。{fund_note}</div></div></div>""")

    # ④ 短期20日评估（引擎 B）
    rows = ""
    for p in review["positions"]:
        b = p["beta"]
        state_txt = {"above": '<span class="down">上方</span>', "below": '<span class="up">带外↓</span>',
                     "neutral": '<span style="color:#8A6D2F">带内</span>'}[b["state"]]
        rows += f"""<tr><td><b>{p['name']}</b></td>
<td class="mono">{_num(p['close'])} <span class="{_cls(p['day_pct'])}" style="font-size:9px">{_pct(p['day_pct'])}</span></td>
<td class="mono {_cls(b['mom_20d'])}">{_pct(b['mom_20d'])}</td>
<td class="mono">{_pct(b['vol_20d'],0)}</td>
<td class="mono">{_num(b['ma20'])}</td>
<td class="mono {_cls(b['ma20_break'])}">{_pct(b['ma20_break'])}<br><span style="font-size:9px">{state_txt}</span></td>
<td class="mono">{_num(b['high20_intra'])}<br><span class="down" style="font-size:9px">{_pct(b['dist_high20'])}</span></td>
<td class="mono">{_num(b['low20'])}</td>
<td class="mono" style="font-size:10px">{_num(b['band_low'])} ~ {_num(b['band_high'])}</td></tr>"""
    sigs = "".join(f"""<div class="sig"><span class="tag {'tSell' if '卖出' in s['type'] else 'tHold'}">{s['type']} · {s['evidence']}</span>
<div class="tx"><b>{s['name']} · {s['signal_id']}</b><span>{s['detail']} → {s['action']} · 冷却 {s['cooldown']}</span></div></div>"""
                   for s in review.get("signals", [])) or f'<div class="meta" style="padding:8px 0">本日无引擎 B 触发信号（{n_pos} 标的均未越 ±2% 确认带 / 未创20日新高）</div>'
    h.append(f"""<h2>④ 短期20日评估<em>Engine B · 增强不决策</em></h2>
<div class="card"><table><tr><th>标的</th><th>收盘(当日)</th><th>20日动量</th><th>20日波动</th><th>MA20</th>
<th>收盘/MA20</th><th>20日高点(距离)</th><th>20日低点</th><th>执行参考带</th></tr>{rows}</table>
<div style="margin-top:6px">{sigs}</div>
<div class="warn"><span>⚠</span><div><b>增强不决策</b>：引擎 B 只产出评估单/提醒/参考价位，永不改动引擎 A 目标持仓——统一走拟调仓确认闭环。
执行参考带 = MA20 × (1 ± 1.5σ)，σ 为 20 日日收益标准差（年化波动/√252）。</div></div>{decay_caveat_html(review)}</div>""")

    # ⑤ 下一交易日指引
    hd = review.get("holiday_days", 0)
    gap_note = f"自 AS_OF {review['as_of']} 起休市 {hd} 个日历日（节假日）" if hd else "AS_OF 即最近交易日"
    guides = "".join(_guidance(p) for p in review["positions"])
    h.append(f"""<h2>⑤ 下一交易日指引<em>Next Trading Day · {review['next_trade_date']}</em></h2>
<div class="card"><div class="meta" style="margin:0 0 6px">下一交易日 <b>{review['next_trade_date']}</b>（交易日历实时计算）· {gap_note} · 节后首日注意跳空与流动性</div>
{guides}
<div class="warn"><span>◇</span><div>指引为<b>规则化数据分析输出</b>（引擎A五维 × 引擎B 20日状态交叉），非投资建议；
所有价位均由 {review['as_of']} 收盘数据确定性计算，可经 checkpoint 重放复现。</div></div></div>""")

    # ⑥ 变更/执行/风险建议
    trades = "".join(f"""<tr><td><code>{t['side']}</code></td><td><b>{t['name']}</b><br>
<span style="font-family:var(--mono);font-size:9px;color:var(--mute)">{t['secid']}</span></td>
<td class="mono">{t['from']}% → <b>{t['to']}%</b></td><td style="font-size:10.5px;line-height:1.6">{t['reason']}<br>
<span style="font-family:var(--mono);font-size:8.5px;color:var(--mute)">证据 {t['evidence']} · {t['settle']} · {t['cost']}</span></td></tr>"""
                     for t in plan["diff_trades"]) or f'<tr><td colspan="4" class="meta" style="padding:10px 7px">无触发调仓（{n_pos} 标的引擎 B 均未触发卖出预警；持有确认项见执行建议）</td></tr>'
    h.append(f"""<h2>⑥ 变更/执行/风险建议<em>Three Advices · 需人工确认</em></h2>
<div class="adv advChg"><h3>变更建议 · 拟调仓单 {plan['plan_id']}（{plan['status']}）</h3>
<table><tr><th></th><th>标的</th><th>权重</th><th>理由链</th></tr>{trades}</table>
<div class="meta" style="margin-top:8px">预计换手 {plan['turnover_est']} · 成本预估 ¥{plan['cost_est_cny']:,}（按名义 ¥100,000 · {plan.get('cost_note', '')}）· 策略永不直接动仓位</div></div>""")

    exe = "".join(_guidance(p) for p in review["positions"] if p["beta"]["state"] != "neutral")
    if not exe:
        exe = '<div class="meta" style="padding:6px 0">全部标的处于中性区：无执行择时指令，按既定计划持有。</div>'
    h.append(f"""<div class="adv advExe"><h3>执行建议（引擎B择时 · 模式①③）</h3>{exe}
<div class="meta" style="margin-top:6px">{exe_cost_note}</div></div>""")

    vol_sorted = sorted(positions, key=lambda p: -(p["alpha"].get("vol_250") or 0))
    if pe_ps:
        val_row = ("估值风险", "；".join(f"{p['name']} PE分位{_pct(p['alpha'].get('val_pe_pct'), 0)}" for p in pe_ps),
                   "分位高于 80% 的持仓，估值维度打分显著降权，复核持有逻辑")
    elif qdii:
        val_row = ("QDII 溢价/折价", "、".join(qdii),
                   "场内价格可显著偏离净值；买入前核对 IOPV/净值溢价，高溢价时暂缓执行")
    else:
        val_row = ("估值风险", "—", "基金类组合无估值分位数据")
    risk_rows = "".join(f"""<tr><td>{r}</td><td class="mono">{v}</td><td>{s}</td></tr>""" for r, v, s in [
        ("节假日跳空", f"休市 {hd} 个日历日",
         "节后首日波动放大概率高；指引价位以 AS_OF 收盘计算，开盘跳空超出执行参考带时暂缓执行"),
        ("持仓集中", f"{n_pos} 标的等权 {w0:.0f}%",
         f"单票权重上限 {w0:.0f}%；若某标的综合评级降至 D 且引擎B预警，触发集中度再评估"),
        ("最高波动持仓", f"{vol_sorted[0]['name']} {_pct(vol_sorted[0]['alpha'].get('vol_250'), 0)}",
         "高波动标的对组合回撤贡献最大；关注其 MA20 状态是否转 below"),
        val_row,
    ])
    h.append(f"""<div class="adv advRisk"><h3>风险建议（风险官规则）</h3><table>
<tr><th>风险</th><th>读数</th><th>建议</th></tr>{risk_rows}</table></div>""")

    h.append(f"""<div class="disc"><b>合规与复现</b>：数据来自公开来源（AKShare 聚合 {upstreams}），仅供研究参考，不构成投资建议；接口可能因上游网站变化而失效。
可复现四元组：run <code>{run_id}</code> · code <code>{code_ver}</code> · dataset <code>{ds_commit}</code> · as_of <code>{review['as_of']}</code>；
六项验证结果见 ①；重放：<code>python3 ap fetch &amp;&amp; python3 ap run tasks/review_{review['id']}.yaml --as-of {review['as_of']}</code>。</div>
</body></html>""")
    return "".join(h)


def render_md(review, plan, run_id, code_ver, ds_commit, verify) -> str:
    n_pos = len(review["positions"])
    qdii = [p["name"] for p in review["positions"] if "QDII" in p.get("industry", "")]
    L = []
    L.append(f"# 「{review['name']}」组合复盘报告\n")
    L.append(f"RUN `{run_id}` · CODE `{code_ver}` · DATASET `{ds_commit}` · AS_OF `{review['as_of']}` · 下一交易日 `{review['next_trade_date']}`\n")
    L.append("## ① 数据与验证\n| 验证项 | 结果 |\n| --- | --- |")
    for k, v in verify.items():
        L.append(f"| {k} | {'✓' if str(v).startswith('pass') else '✗'} {v} |")
    L.append("\n## ② 组合体检（等权名义）")
    pm = review["portfolio_metrics"]
    L.append(f"- 当日 {_pct(pm['day_ret'])} · 近20日 {_pct(pm['ret_20d'])} · 年初至今 {_pct(pm['ret_ytd'])} · 近一年 {_pct(pm['ret_1y'])} · 一年最大回撤 {_pct(pm['max_dd_1y'])}")
    L.append(f"- {pm['method']} · {review['note']}\n")
    L.append(macro_section_md(review))          # ②·5 宏观流动性（引擎C，T-C4）
    L.append("## ③ 中长期因子评估（引擎A）\n| 标的 | PE-TTM(分位) | PB(分位) | 股息率 | ROE-TTM | 营收同比 | 净利同比 | 动量12-1 | 年化波动 | 五维 | 综合 |")
    L.append("| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |")
    for p in review["positions"]:
        a = p["alpha"]
        comp = f"{p['composite_grade']} {p['composite']}" if p["composite"] is not None else "—（基金不适用）"
        L.append(f"| {p['name']} | {_num(a['val_pe_ttm'])}({_pct(a['val_pe_pct'],0)}) | {_num(a['val_pb'])}({_pct(a.get('val_pb_pct'),0)}) "
                 f"| {_pct(a['div_yield_ttm'])} | {_num(a['qua_roe_ttm'],1)}% | {_num(a.get('grow_rev_yoy'),1)}% | {_num(a.get('grow_np_yoy'),1)}% "
                 f"| {_pct(a['mom_12_1'])} | {_pct(a['vol_250'],0)} | {'/'.join(p['grades'].values())} | {comp} |")
    L.append("")
    L.append("## ④ 短期20日评估（引擎B）\n| 标的 | 收盘 | 20日动量 | 20日波动 | MA20 | 收盘/MA20 | 20日高点(距离) | 执行参考带 |")
    L.append("| --- | --- | --- | --- | --- | --- | --- | --- |")
    for p in review["positions"]:
        b = p["beta"]
        L.append(f"| {p['name']} | {_num(p['close'])}({_pct(p['day_pct'])}) | {_pct(b['mom_20d'])} | {_pct(b['vol_20d'],0)} "
                 f"| {_num(b['ma20'])} | {_pct(b['ma20_break'])}({b['state']}) | {_num(b['high20_intra'])}({_pct(b['dist_high20'])}) "
                 f"| {_num(b['band_low'])}~{_num(b['band_high'])} |")
    for s in review.get("signals", []):
        L.append(f"- [{s['type']}·{s['evidence']}] {s['name']} {s['signal_id']}：{s['detail']} → {s['action']}（冷却 {s['cooldown']}）")
    cav = decay_caveat_md(review)
    if cav:
        L.append(cav)
    L.append("")
    L.append(f"## ⑤ 下一交易日指引（{review['next_trade_date']}）")
    for p in review["positions"]:
        b, a = p["beta"], p["alpha"]
        if b["state"] == "below":
            act = f"预警：观察能否收复 MA20 {_num(b['ma20'])}；持续带外进入减仓评估（人工确认）"
        elif b["state"] == "above":
            act = f"趋势上方：持有；回踩参考带 [{_num(b['band_low'])}, {_num(b['band_high'])}]；跌破 {_num(b['band_low'])} 转预警"
        else:
            act = f"中性区：无操作；关注带边界 {_num(b['band_low'])}/{_num(b['band_high'])} 突破方向"
        if a.get("val_pe_pct") is not None:
            tail = f"估值 PE {_num(a['val_pe_ttm'])}（五年{_pct(a['val_pe_pct'],0)}分位）· 综合 {p['composite_grade']}"
        else:
            tail = f"动量12-1 {_pct(a['mom_12_1'])} · 250日波动 {_pct(a['vol_250'],0)}（基金类：无估值分位）"
        L.append(f"- **{p['name']}**：{act} · {tail}")
    L.append("")
    L.append(f"## ⑥ 变更/执行/风险建议 · {plan['plan_id']}（{plan['status']}）")
    for t in plan["diff_trades"]:
        L.append(f"- **{t['side']} {t['name']}** {t['from']}% → {t['to']}%｜{t['reason']}｜证据 {t['evidence']}")
    if not plan["diff_trades"]:
        L.append("- 无触发调仓（引擎B未触发卖出预警）")
    L.append(f"- 预计换手 {plan['turnover_est']} · 成本预估 ¥{plan['cost_est_cny']:,}（{plan.get('cost_note', '')}）")
    L.append(f"- 风险：节假日跳空（休市 {review.get('holiday_days', 0)} 个日历日）；{n_pos}标的等权集中；最高波动持仓 "
             + volname(review)
             + (f"；QDII 溢价/折价（{'、'.join(qdii)}）买入前核对净值溢价" if qdii else "") + "\n")
    L.append("> 数据来自公开来源（AKShare 聚合新浪/腾讯/东方财富等公开行情），仅供研究参考，不构成投资建议。")
    return "\n".join(L)


def volname(review):
    v = sorted(review["positions"], key=lambda p: -(p["alpha"].get("vol_250") or 0))
    return f"{v[0]['name']} {_pct(v[0]['alpha'].get('vol_250'), 0)}"


def render_all(review, plan, ctx: Ctx, run_id: str, art_dir: Path, verify: dict | None = None) -> dict:
    verify = verify or {}
    ds_commit = "ds@" + __import__("hashlib").sha256(
        (ctx.local_repo / "market" / "provenance.json").read_bytes()).hexdigest()[:12] \
        if (ctx.local_repo / "market" / "provenance.json").exists() else "ds@unknown"
    html = render_html(review, plan, ctx, run_id, ctx.code_version, ds_commit, verify)
    md = render_md(review, plan, run_id, ctx.code_version, ds_commit, verify)
    (art_dir / "report.html").write_text(html, encoding="utf-8")
    (art_dir / "report.md").write_text(md, encoding="utf-8")
    (art_dir / "review.json").write_text(json.dumps(review, ensure_ascii=False, indent=2), encoding="utf-8")
    (art_dir / "rebalance_plan.json").write_text(json.dumps(plan, ensure_ascii=False, indent=2), encoding="utf-8")
    return {"html": str(art_dir / "report.html"), "md": str(art_dir / "report.md")}
