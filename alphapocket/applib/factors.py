"""因子计算核心（真实数据）：引擎 A（中长期）+ 引擎 B（20日信号）

引擎 A：估值（PE/PB·五年分位）、质量（ROE_TTM/利润率）、成长（营收/净利同比）、
        动量（12-1月）、红利低波（股息率TTM/250日波动）—— PIT 阶梯函数对齐报告可用人时点。
引擎 B：20日窗口 mom/vol/MA20突破/20日新高/距高点——增强不决策。

全部输入来自 dwd 层（AKShare 真实数据），输出确定性可复现。
"""
from __future__ import annotations
import datetime as dt

ENGINE_A_FACTORS = ["val_pe_ttm", "val_pb", "val_pe_pct", "val_pb_pct", "div_yield_ttm",
                    "qua_roe_ttm", "qua_margin", "grow_rev_yoy", "grow_np_yoy", "mom_12_1", "vol_250"]
ENGINE_B_FACTORS = ["mom_20d", "vol_20d", "ma20_break", "high20_touch", "dist_high20"]


# ── 工具 ──────────────────────────────────────────────
def _interp(x, anchors):
    """分段线性插值打分：anchors=[(x,score),...] 升序"""
    if x is None:
        return None
    if x <= anchors[0][0]:
        return anchors[0][1]
    if x >= anchors[-1][0]:
        return anchors[-1][1]
    for (x0, s0), (x1, s1) in zip(anchors, anchors[1:]):
        if x0 <= x <= x1:
            return s0 + (s1 - s0) * (x - x0) / (x1 - x0) if x1 != x0 else s0
    return None


def _grade(score):
    if score is None:
        return "—"
    for th, g in [(85, "A"), (70, "B+"), (60, "B"), (50, "C+"), (40, "C"), (30, "D+")]:
        if score >= th:
            return g
    return "D"


# ── PIT：报告期可用性 ──────────────────────────────────
def latest_report(reports: list[dict], t: str) -> dict | None:
    """t 日（含）已可用的最新报告期（按 period 优先、announce 兜底）"""
    avail = [r for r in reports if r["announce_date"] <= t]
    return max(avail, key=lambda r: (r["period"], r["announce_date"])) if avail else None


def _by_period(reports: list[dict], period: str, t: str) -> dict | None:
    for r in reports:
        if r["period"] == period and r["announce_date"] <= t:
            return r
    return None


def ttm_eps(reports: list[dict], t: str) -> float | None:
    """TTM 摊薄 EPS = 上年FY + 本年YTD − 上年同期YTD（FY 报告期直接取全年）"""
    R = latest_report(reports, t)
    if R is None or R.get("eps_ytd") is None:
        return None
    y, md = R["period"][:4], R["period"][5:]
    if md == "12-31":
        return R["eps_ytd"]
    py = int(y) - 1
    fy = _by_period(reports, f"{py}-12-31", t)
    prev = _by_period(reports, f"{py}-{md}", t)
    if fy and prev and fy.get("eps_ytd") is not None and prev.get("eps_ytd") is not None:
        return fy["eps_ytd"] + R["eps_ytd"] - prev["eps_ytd"]
    return None


# ── 引擎 A ────────────────────────────────────────────
def compute_alpha(secid: str, bars: list[dict], reports: list[dict], dividends: list[dict],
                  as_of: str | None = None) -> dict:
    """bars/reports 按 dt/announce 升序；返回 as_of 的引擎 A 全因子 + 五维评分"""
    closes = [(b["dt"], b["close"]) for b in bars if b["close"]]
    as_of = as_of or closes[-1][0]
    hist = [(d, c) for d, c in closes if d <= as_of]
    close = hist[-1][1]

    # PE/PB 历史序列（PIT 阶梯）与五年分位
    win_start = (dt.date.fromisoformat(as_of) - dt.timedelta(days=365 * 5)).isoformat()
    pe_s, pb_s = [], []
    for d, c in hist:
        e = ttm_eps(reports, d)
        R = latest_report(reports, d)
        if e and e > 0:
            pe_s.append((d, c / e))
        if R and R.get("bps") and R["bps"] > 0:
            pb_s.append((d, c / R["bps"]))
    pe_win = [v for d, v in pe_s if d >= win_start]
    pb_win = [v for d, v in pb_s if d >= win_start]
    pe = pe_s[-1][1] if pe_s else None
    pb = pb_s[-1][1] if pb_s else None
    pct = lambda w, cur: (sum(1 for v in w if v < cur) + 0.5 * sum(1 for v in w if v == cur)) / len(w) if (w and cur) else None
    pe_pct, pb_pct = pct(pe_win, pe), pct(pb_win, pb)

    # 报告期类因子（取 as_of 可用最新报告）
    R = latest_report(reports, as_of)
    e = ttm_eps(reports, as_of)
    bps = R.get("bps") if R else None
    roe_ttm = (e / bps * 100) if (e and bps) else None

    # 股息率 TTM（近365日除权除息，闭区间 [as_of-365d, as_of]）
    dvd_start = (dt.date.fromisoformat(as_of) - dt.timedelta(days=365)).isoformat()
    # 无分红数据（ETF/LOF）→ 不适用（None）；有数据但窗口内无除权 → 0（真实为零）
    dps_sum = (sum(d["dps"] for d in dividends if dvd_start <= d["ex_date"] <= as_of and d.get("dps"))
               if dividends else None)
    div_yield = dps_sum / close if (dps_sum is not None and close) else None

    # 动量与波动（价格类）
    cs = [c for _, c in hist]
    mom_12_1 = cs[-21] / cs[-252] - 1 if len(cs) >= 252 else None
    rets = [cs[i] / cs[i - 1] - 1 for i in range(max(1, len(cs) - 250), len(cs))]
    vol_250 = (sum((r - sum(rets) / len(rets)) ** 2 for r in rets) / (len(rets) - 1)) ** 0.5 * (252 ** 0.5) if len(rets) > 2 else None

    # 五维评分（0-100，锚点见 report 说明）
    s_val = (lambda a, b: (a + b) / 2 if (a is not None and b is not None) else (a if a is not None else b))(
        *(100 - p * 100 if p is not None else None for p in (pe_pct, pb_pct)))
    s_qua = _interp(roe_ttm, [(0, 10), (5, 35), (10, 55), (15, 75), (20, 90), (30, 100)])
    gs = [R.get("rev_g"), R.get("np_g")] if R else [None, None]
    gs = [g for g in gs if g is not None]
    s_gro = _interp(sum(gs) / len(gs), [(-20, 10), (0, 40), (10, 60), (20, 75), (35, 90), (60, 100)]) if gs else None
    s_mom = _interp(mom_12_1, [(-30, 10), (-10, 30), (0, 45), (10, 60), (30, 80), (60, 100)])
    s_dvd = _interp(div_yield * 100 if div_yield is not None else None, [(0, 10), (1, 35), (2, 55), (3, 70), (5, 90), (7, 100)])
    s_lvol = _interp(vol_250 * 100 if vol_250 is not None else None, [(15, 100), (20, 85), (30, 60), (40, 40), (60, 20)])
    s_dl = (s_dvd + s_lvol) / 2 if (s_dvd is not None and s_lvol is not None) else (s_dvd if s_dvd is not None else s_lvol)
    dims = {"估值": s_val, "质量": s_qua, "成长": s_gro, "动量": s_mom, "红利低波": s_dl}
    W = {"估值": .25, "质量": .25, "成长": .20, "动量": .15, "红利低波": .15}
    w_sum = sum(W[k] for k in W if dims[k] is not None)
    tot = sum(dims[k] * W[k] for k in W if dims[k] is not None) / w_sum if w_sum else None
    if R is None:
        tot = None   # 无财报数据（ETF/LOF/指数类）：五维综合不适用（动量/波动因子照常计算）

    return {
        "as_of": as_of, "close": close,
        "val_pe_ttm": pe, "val_pb": pb, "val_pe_pct": pe_pct, "val_pb_pct": pb_pct,
        "div_yield_ttm": div_yield, "qua_roe_ttm": roe_ttm,
        "qua_margin": R.get("margin") if R else None,
        "grow_rev_yoy": R.get("rev_g") if R else None,
        "grow_np_yoy": R.get("np_g") if R else None,
        "mom_12_1": mom_12_1, "vol_250": vol_250,
        "used_period": R["period"] if R else None,
        "used_announce": R["announce_date"] if R else None,
        "pe_hist_n": len(pe_win), "pb_hist_n": len(pb_win),
        "dims": {k: round(v, 1) if v is not None else None for k, v in dims.items()},
        "grades": {k: _grade(v) for k, v in dims.items()},
        "composite": round(tot, 1) if tot is not None else None,
        "composite_grade": _grade(tot),
    }


# ── 引擎 B ────────────────────────────────────────────
def compute_beta(secid: str, bars: list[dict], as_of: str | None = None) -> dict:
    closes = [(b["dt"], b["close"]) for b in bars if b["close"]]
    highs = [(b["dt"], b["high"]) for b in bars if b.get("high")]
    as_of = as_of or closes[-1][0]
    hist_c = [c for d, c in closes if d <= as_of]
    hist_h = [h for d, h in highs if d <= as_of]
    hist_l = [b["low"] for b in bars if b["dt"] <= as_of and b.get("low")]
    close = hist_c[-1]

    win = hist_c[-20:]
    ma20 = sum(win) / 20
    ratio = close / ma20 - 1
    mom = close / win[0] - 1
    rets20 = [hist_c[i] / hist_c[i - 1] - 1 for i in range(len(hist_c) - 20, len(hist_c))]
    mu = sum(rets20) / len(rets20)
    vol20 = (sum((r - mu) ** 2 for r in rets20) / (len(rets20) - 1)) ** 0.5 * (252 ** 0.5)
    high20_close = max(win)
    high20_intra = max(hist_h[-20:])
    low20 = min(hist_l[-20:])
    high20_touch = 1 if close >= high20_close else 0
    dist_high20 = close / high20_intra - 1

    if ratio < -0.02:
        state, sig = "below", {"signal_id": "sig_ma20_break", "type": "卖出预警",
                               "detail": f"收盘低于 MA20 {ratio:+.1%}（±2% 确认带外）", "evidence": "E2",
                               "action": "生成减仓评估单（不自动卖出）", "cooldown": "10 交易日"}
    elif ratio > 0.02:
        state, sig = "above", {"signal_id": "sig_ma20_break", "type": "买入/持有确认",
                               "detail": f"收盘高于 MA20 {ratio:+.1%}（±2% 确认带外）", "evidence": "E2",
                               "action": "引擎A目标仓位执行确认（模式③）", "cooldown": "10 交易日"}
    else:
        state, sig = "neutral", None
    sigs = [sig] if sig else []
    if high20_touch:
        sigs.append({"signal_id": "sig_high20_touch", "type": "持有确认",
                     "detail": f"收盘创 20 日收盘新高（{close:.2f}）", "evidence": "E2",
                     "action": "趋势延续确认，持有（引擎A持仓内）", "cooldown": "10 交易日"})

    sd = vol20 / (252 ** 0.5)  # 日波动
    return {
        "as_of": as_of, "close": close, "mom_20d": mom, "vol_20d": vol20,
        "ma20": ma20, "ma20_break": ratio, "state": state,
        "high20_touch": high20_touch, "dist_high20": dist_high20,
        "high20_intra": high20_intra, "low20": low20,
        "sigma_daily": sd,
        "band_low": ma20 * (1 - 1.5 * sd), "band_high": ma20 * (1 + 1.5 * sd),
        "signals": sigs,
    }
