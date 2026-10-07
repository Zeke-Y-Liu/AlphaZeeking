"""R4 · 时间延迟敏感性实证（《研究缺口R1-R4实施计划》§6）

对引擎 B 的信号事件，基于阶段〇 ``iter_signal_events`` 识别全部历史事件，逐事件计算四种
执行时点的前向 ``horizon`` 日收益（默认 20，与 ``horizon_days=20`` 一致）：

| 执行时点 | 价格 | 含义 |
| --- | --- | --- |
| T+0 | 信号日收盘 ``close`` | 理论即时执行（上界） |
| T+1 开盘 | 次日 ``open`` | **产品实际口径**（M6 次日开盘/VWAP 可执行） |
| T+1 收盘 | 次日 ``close`` | 最保守当日口径 |
| T+2 开盘 | 第二日 ``open`` | 极端延迟（决策/通道拖延） |

- **方向口径**：卖出预警取跌幅保护口径（``dir = -1``，价格下跌为“正收益”），其余取 ``+1``。
- **衰减率** = (T+0 收益 − T+k 收益) / |T+0 收益|（>0 = 延迟侵蚀收益）。
- **降级规则**：``T+1 开盘`` 衰减率 > 50% 且事件数 ≥ 30 → registry 该 signal 条目置 ``weakening``
  并加 ``decay_flag: fast_decay``（report.py 挂 caveat）；事件数 < 30 → 标 ``insufficient``（待全量
  历史补强，小样本保护，不降级）。

产物：``sandbox/artifacts/decay_<ts>/{decay.json, decay.md, decay.html}``
"""
from __future__ import annotations

import json
import time
from pathlib import Path

from . import factors as F

HORIZON = 20
EXEC_POINTS = [("t0_close", "T+0 收盘"), ("t1_open", "T+1 开盘"),
               ("t1_close", "T+1 收盘"), ("t2_open", "T+2 开盘")]
FAST_DECAY = 0.50      # T+1 开盘衰减率阈值
MIN_EVENTS = 30        # 小样本保护阈值
# 信号事件 id → registry 因子条目 id（既有 signal 类条目，不新增重复条目；
# P1.1/P1.3 新增的两个事件信号自身即 registry 条目 → 同名映射）
SIGNAL_REGISTRY_MAP = {"sig_ma20_break": "ma20_break", "sig_high20_touch": "high20_touch",
                       "sig_bias_ext": "sig_bias_ext", "sig_vol_price_div": "sig_vol_price_div"}
FLAG_RANK = {"ok": 0, "insufficient": 1, "fast_decay": 2}


def _dir(sig_type: str) -> float:
    """方向：卖出预警取跌幅保护口径（-1），买入/持有确认取 +1"""
    return -1.0 if "卖出" in str(sig_type) else 1.0


def event_returns(ev: dict) -> dict | None:
    """单事件四种执行时点 → 方向调整后的前向收益（退出价固定为信号后第 horizon 日收盘）"""
    exit_c = ev.get("exit_close")
    if exit_c is None:
        return None
    d = _dir(ev.get("type"))
    out = {}
    for k, _ in EXEC_POINTS:
        px = (ev.get("exec_prices") or {}).get(k)
        out[k] = (d * (exit_c / px - 1.0)) if px else None
    return out


def _mean(xs: list) -> float | None:
    xs = [x for x in xs if x is not None]
    return sum(xs) / len(xs) if xs else None


def analyze_events(events: list[dict], horizon: int = HORIZON) -> dict:
    """事件 → 分信号衰减表（纯函数）：四种执行时点均值 + 衰减率 + 分年度 + 判定。"""
    groups: dict[tuple, list] = {}
    for ev in events:
        r = event_returns(ev)
        if r is None:
            continue
        groups.setdefault((ev["signal_id"], ev["type"]), []).append((ev["dt"][:4], r))

    signals = []
    for (sid, stype), rows in sorted(groups.items()):
        n = len(rows)
        mean = {k: _mean([r[k] for _, r in rows]) for k, _ in EXEC_POINTS}
        base = mean["t0_close"]
        decay = {k: ((base - mean[k]) / abs(base) if (base not in (None, 0) and mean[k] is not None)
                     else None) for k, _ in EXEC_POINTS}
        by_year = {}
        for y in sorted({y for y, _ in rows}):
            yr = [r for yy, r in rows if yy == y]
            b = _mean([r["t0_close"] for r in yr])
            t1 = _mean([r["t1_open"] for r in yr])
            by_year[y] = {"n": len(yr), "t0": b, "t1_open": t1,
                          "decay": ((b - t1) / abs(b) if (b not in (None, 0) and t1 is not None) else None)}
        d1 = decay["t1_open"]
        if n < MIN_EVENTS:
            flag = "insufficient"
        elif d1 is not None and d1 > FAST_DECAY:
            flag = "fast_decay"
        else:
            flag = "ok"
        signals.append({"signal_id": sid, "type": stype, "n_events": n, "mean": mean,
                        "decay_rate": decay, "by_year": by_year, "flag": flag,
                        "verdict": _verdict(flag, d1, n)})
    return {"generated": time.strftime("%Y-%m-%dT%H:%M:%S"), "code_version": "R4",
            "horizon": int(horizon), "n_events": sum(s["n_events"] for s in signals),
            "n_signals": len(signals), "exec_points": [{"key": k, "label": v} for k, v in EXEC_POINTS],
            "signals": signals}


def _verdict(flag: str, d1: float | None, n: int) -> str:
    if flag == "insufficient":
        return f"事件数不足（{n} < {MIN_EVENTS}）→ 待全量历史补强，不降级（小样本保护）"
    if flag == "fast_decay":
        return (f"⚠ 衰减过快（T+1 开盘衰减 {d1 * 100:.1f}% > {FAST_DECAY * 100:.0f}%）"
                f"→ registry 置 weakening + 报告 caveat")
    return (f"✓ 延迟稳健（T+1 开盘衰减 {'—' if d1 is None else f'{d1 * 100:.1f}%'} ≤ "
            f"{FAST_DECAY * 100:.0f}%）")


def run_decay(bars: dict, signal_ids: str | list | None = "all", horizon: int = HORIZON,
              start: str | None = None, end: str | None = None) -> dict:
    """全历史信号事件 → 延迟衰减报告（只读，不落盘）。``bars``：{secid: [K线]}。"""
    ids = None if (signal_ids in (None, "all")) else (
        [signal_ids] if isinstance(signal_ids, str) else list(signal_ids))
    events = []
    for secid, arr in bars.items():
        for sid in (ids or F.ENGINE_B_SIGNAL_IDS):
            events.extend(F.iter_signal_events(secid, arr, sid, horizon=horizon,
                                               start=start, end=end))
    rep = analyze_events(events, horizon)
    rep["start"] = start
    rep["end"] = end
    rep["scanned_secids"] = len(bars)
    rep["notes"] = [
        "执行时点价格：T+0=信号日收盘，T+1/T+2 取该标的自身 K 线的次日 open/close（停牌/缺线按自身序列顺延）。",
        "退出价固定为信号后第 20 个交易日收盘（与 horizon_days=20 一致）→ 延迟执行捕获的区间更短，衰减率由此度量。",
        "方向口径：卖出预警取跌幅保护（dir=-1，下跌为正收益），买入/持有确认取 dir=+1。",
        "衰减率 = (T+0 收益 − T+k 收益) / |T+0 收益|；>0 表示延迟侵蚀收益。",
        f"降级规则：T+1 开盘衰减 > {FAST_DECAY * 100:.0f}% 且事件数 ≥ {MIN_EVENTS} → weakening + decay_flag=fast_decay；"
        + f"事件数 < {MIN_EVENTS} 只标注不降级（小样本保护）。",
        "产品口径为 T+1 开盘（M6 次日开盘/VWAP 可执行）；T+0 仅作理论上界，不作为可执行结论。",
        "α 数据来自 AKShare 聚合 9 标的，事件数不足的信号标「待全量历史补强」，不静默降级。",
    ]
    return rep


# ── registry 降级 ───────────────────────────────────────
def apply_registry(reg: dict, rep: dict) -> list[dict]:
    """把衰减摘要写回 registry signal 条目；fast_decay → 置 weakening + decay_flag（就地修改）。"""
    byrid: dict[str, list] = {}
    for s in rep.get("signals", []):
        byrid.setdefault(SIGNAL_REGISTRY_MAP.get(s["signal_id"], s["signal_id"]), []).append(s)
    changes = []
    for rid, ss in sorted(byrid.items()):
        f = next((x for x in reg.get("factors", []) if x.get("id") == rid), None)
        if f is None:
            changes.append({"registry_id": rid, "applied": False, "reason": "未注册（血缘悬空）"})
            continue
        worst = max(ss, key=lambda s: FLAG_RANK.get(s["flag"], 0))
        flag = worst["flag"]
        total_n = sum(s["n_events"] for s in ss)
        f["delay_decay"] = {
            "t0": _round(worst["mean"]["t0_close"]), "t1_open": _round(worst["mean"]["t1_open"]),
            "decay_rate": _round(worst["decay_rate"]["t1_open"]), "n_events": total_n,
            "horizon": rep["horizon"], "flag": flag,
            "by_type": {f"{s['signal_id']}/{s['type']}": {
                "t0": _round(s["mean"]["t0_close"]), "t1_open": _round(s["mean"]["t1_open"]),
                "decay_rate": _round(s["decay_rate"]["t1_open"]), "n_events": s["n_events"],
                "flag": s["flag"]} for s in ss},
        }
        ch = {"registry_id": rid, "applied": True, "flag": flag, "n_events": total_n}
        if flag == "fast_decay":
            f["decay_flag"] = "fast_decay"
            if f.get("status") != "weakening":
                ch["status"] = f"{f.get('status')}→weakening"
                f["status"] = "weakening"
        changes.append(ch)
    return changes


def _round(v, d: int = 6):
    return None if v is None else round(v, d)


# ── 渲染 ───────────────────────────────────────────────
def _pct(v, d: int = 2):
    return "—" if v is None else f"{v * 100:+.{d}f}%"


def _lab(r: dict) -> str:
    return f"{r['signal_id']} · {r['type']}"


def _svg_curve(sig: dict, w: int = 460, h: int = 190) -> str:
    """单信号延迟衰减曲线：x=四种执行时点，y=方向调整后均值收益"""
    pts_vals = [sig["mean"][k] for k, _ in EXEC_POINTS]
    vals = [v for v in pts_vals if v is not None]
    if len(vals) < 2:
        return ""
    lo, hi = min(vals), max(vals)
    pad = (hi - lo) * 0.2 or 0.005
    lo, hi = lo - pad, hi + pad
    x0, y0, pw = 44, 16, w - 68
    ph = h - y0 - 40
    nseg = len(EXEC_POINTS) - 1
    pts = [(x0 + pw * i / nseg, y0 + ph * (1 - (v - lo) / (hi - lo)), v)
           for i, v in enumerate(pts_vals) if v is not None]
    path = "M" + " L".join(f"{x:.1f},{y:.1f}" for x, y, _ in pts)
    dots = "".join(f'<circle cx="{x:.1f}" cy="{y:.1f}" r="3.4" fill="#A93226"/>'
                   f'<text x="{x:.1f}" y="{y - 8:.1f}" text-anchor="middle" font-size="9" fill="#7A7060">'
                   f'{v * 100:+.1f}%</text>' for x, y, v in pts)
    xs = "".join(f'<text x="{x0 + pw * i / nseg:.1f}" y="{h - 20}" text-anchor="middle" font-size="9" '
                 f'fill="#9C9585">{EXEC_POINTS[i][1]}</text>' for i in range(len(EXEC_POINTS)))
    grid = "".join(
        f'<line x1="{x0}" y1="{y0 + ph * f:.1f}" x2="{x0 + pw}" y2="{y0 + ph * f:.1f}" '
        f'stroke="#DCD5C6" stroke-dasharray="3 4"/>'
        f'<text x="{x0 - 6}" y="{y0 + ph * f + 3:.1f}" text-anchor="end" font-size="8.5" fill="#9C9585">'
        f'{(hi - (hi - lo) * f) * 100:+.1f}%</text>' for f in (0, 0.5, 1.0))
    return (f'<svg viewBox="0 0 {w} {h}" width="100%" role="img" aria-label="延迟衰减曲线">'
            f'{grid}<path d="{path}" fill="none" stroke="#A93226" stroke-width="1.9"/>{dots}{xs}</svg>')


def render_md(rep: dict) -> str:
    L = ["# 时间延迟衰减报告 · 引擎 B", "",
         f"生成 `{rep['generated']}` · 前向窗口 {rep['horizon']} 日 · "
         + f"扫描 {rep.get('scanned_secids', '—')} 标的 · 事件合计 {rep['n_events']}", ""]
    L += ["| 信号 | 事件类型 | 事件数 | T+0 收盘 | T+1 开盘 | T+1 收盘 | T+2 开盘 | 衰减率(T+1开盘) | 判定 |",
          "| --- | --- | --- | --- | --- | --- | --- | --- | --- |"]
    fl = {"ok": "✓ 稳健", "insufficient": "◻ 待补强", "fast_decay": "⚠ 衰减过快"}
    for s in rep["signals"]:
        L.append(f"| {s['signal_id']} | {s['type']} | {s['n_events']} | {_pct(s['mean']['t0_close'])} "
                 f"| {_pct(s['mean']['t1_open'])} | {_pct(s['mean']['t1_close'])} | {_pct(s['mean']['t2_open'])} "
                 f"| {_pct(s['decay_rate']['t1_open'])} | {fl[s['flag']]} |")
    L.append("")
    L.append("## 分年度明细（T+0 / T+1 开盘 / 衰减率）")
    L.append("")
    for s in rep["signals"]:
        L.append(f"### {_lab(s)}（{s['verdict']}）")
        L.append("")
        L.append("| 年度 | 事件数 | T+0 | T+1 开盘 | 衰减率 |")
        L.append("| --- | --- | --- | --- | --- |")
        for y, d in s["by_year"].items():
            L.append(f"| {y} | {d['n']} | {_pct(d['t0'])} | {_pct(d['t1_open'])} | {_pct(d['decay'])} |")
        L.append("")
    if rep.get("registry_changes"):
        L.append("## registry 降级联动")
        L.append("")
        for c in rep["registry_changes"]:
            L.append(f"- `{c['registry_id']}`：{c.get('status') or '维持'}（flag={c.get('flag')} · "
                     f"事件 {c.get('n_events')}）")
        L.append("")
    L.append("## 说明与边界")
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
.mono{font-variant-numeric:tabular-nums}
.flag{color:#A93226;font-weight:600}.ok{color:#1E8449;font-weight:600}
.warn{background:#FCF3E8;border-left:3px solid #D68910;padding:8px 12px;font-size:12px;border-radius:6px;margin:10px 0}
.note{font-size:11.5px;color:#7A7060;margin-top:6px}
.disc{margin-top:26px;font-size:11px;color:#9C9585;border-top:1px solid #E5DECF;padding-top:12px}"""


def render_html(rep: dict) -> str:
    fl = {"ok": ("ok", "✓ 稳健"), "insufficient": ("note", "◻ 待补强"), "fast_decay": ("flag", "⚠ 衰减过快")}
    cards = ""
    for s in rep["signals"]:
        cls, txt = fl[s["flag"]]
        rows = "".join(
            f'<tr><td class="mono">{e["label"]}</td><td class="mono">{_pct(s["mean"][e["key"]])}</td>'
            f'<td class="mono">{_pct(s["decay_rate"][e["key"]])}</td></tr>' for e in rep["exec_points"])
        yrows = "".join(
            f'<tr><td>{y}</td><td class="mono">{d["n"]}</td><td class="mono">{_pct(d["t0"])}</td>'
            f'<td class="mono">{_pct(d["t1_open"])}</td><td class="mono">{_pct(d["decay"])}</td></tr>'
            for y, d in s["by_year"].items())
        cards += (f'<h3>{_lab(s)} · <span class="{cls}">{txt}</span> '
                  f'<span class="note">事件 {s["n_events"]}</span></h3>'
                  f'<div class="card">{_svg_curve(s)}'
                  f'<table><tr><th>执行时点</th><th>均值收益(方向调整)</th><th>衰减率 vs T+0</th></tr>{rows}</table>'
                  f'<table style="margin-top:8px"><tr><th>年度</th><th>事件数</th><th>T+0</th>'
                  f'<th>T+1 开盘</th><th>衰减率</th></tr>{yrows}</table>'
                  f'<div class="meta">{s["verdict"]}</div></div>')
    chg = ""
    if rep.get("registry_changes"):
        chg = ('<h2>registry 降级联动</h2><div class="card">'
               + "".join(f'<div class="mono">{c["registry_id"]}：{c.get("status") or "维持"}'
                         f'（flag={c.get("flag")}）</div>' for c in rep["registry_changes"]) + '</div>')
    return f"""<!DOCTYPE html><html lang="zh-CN"><head><meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>AlphaPocket · R4 时间延迟衰减报告</title><style>{_CSS}</style></head><body>
<h1>R4 · 时间延迟衰减报告（引擎 B）</h1>
<div class="meta">生成 {rep['generated']} · 前向窗口 <b>{rep['horizon']}</b> 日 ·
扫描 {rep.get('scanned_secids', '—')} 标的 · 事件合计 <b>{rep['n_events']}</b><br>
产品口径 <b>T+1 开盘</b>（M6 次日开盘/VWAP 可执行）；T+0 仅作理论上界</div>
<h2>延迟衰减总表</h2>{cards}{chg}
<h2>说明与边界</h2><div class="card"><ul style="font-size:12px;margin:0;padding-left:18px">
{''.join(f'<li>{n}</li>' for n in rep['notes'])}</ul></div>
<div class="disc"><b>合规与复现</b>：数据来自公开来源（AKShare 聚合），仅供研究参考，不构成投资建议。
复现：<code>ap decay --signals all</code></div></body></html>"""


def write_all(rep: dict, art_dir: Path) -> dict:
    art_dir.mkdir(parents=True, exist_ok=True)
    jp, mp, hp = art_dir / "decay.json", art_dir / "decay.md", art_dir / "decay.html"
    jp.write_text(json.dumps(rep, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    mp.write_text(render_md(rep), encoding="utf-8")
    hp.write_text(render_html(rep), encoding="utf-8")
    return {"json": str(jp), "md": str(mp), "html": str(hp)}
