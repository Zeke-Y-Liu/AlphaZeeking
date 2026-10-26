"""因子计算核心（真实数据）：引擎 A（中长期）+ 引擎 B（20日信号）

引擎 A：估值（PE/PB·五年分位）、质量（ROE_TTM/利润率）、成长（营收/净利同比）、
        动量（12-1月）、红利低波（股息率TTM/250日波动）—— PIT 阶梯函数对齐报告可用人时点；
        另含微观流动性诊断族（Amihud 非流动性/Roll 隐含价差/CS 高低价价差，不入维度评分）。
引擎 B：20日窗口 mom/vol/MA20突破/20日新高/距高点——增强不决策。

全部输入来自 dwd 层（AKShare 真实数据），输出确定性可复现。
"""
from __future__ import annotations

import datetime as dt
import itertools
import math

ENGINE_A_FACTORS = ["val_pe_ttm", "val_pb", "val_pe_pct", "val_pb_pct", "div_yield_ttm",
                    "qua_roe_ttm", "qua_margin", "grow_rev_yoy", "grow_np_yoy", "mom_12_1", "vol_250",
                    # P1.1 反转族（价格类，超跌为正）
                    "rev_20d", "rev_60d",
                    # P1.2 品质族补齐（需 A 股三表科目，D5）
                    "qua_leverage", "qua_accrual", "qua_fcf",
                    # P1.4 微观流动性族（诊断：Amihud 非流动性 / Roll 隐含价差 / CS 高低价价差，
                    # 不入维度评分——流动性溢价视角的个股横截面描述因子）
                    "liq_amihud_20d", "liq_roll_60d", "liq_cs_60d"]
ENGINE_B_FACTORS = ["mom_20d", "vol_20d", "ma20_break", "high20_touch", "dist_high20",
                    # P1.3 量价情绪（成交量维度）
                    "turnover_pct"]
# 港美利率/汇率 regime 族（H-0006）：市场级条件变量（择时），非横截面选股因子
HK_REGIME_FACTORS = ["carry_hk_us", "peg_pressure"]

# 引擎C 宏观流动性指标族（市场级条件变量；scorer 定义见 compute_macro_liquidity）
MACRO_FACTORS = ["m2_yoy", "tsf_new_12m", "shibor_1m", "lpr_1y", "rrr_big", "cn_us_10y",
                 "us_10y_2y", "fed_target", "us_cpi_yoy", "us_cpi_surprise"]
MACRO_DIM_WEIGHTS = {"D1": 0.30, "D2": 0.25, "D3": 0.25, "D4": 0.20}

# 引擎D 指数趋势指数族（市场级条件变量，2026-10-26 Goal：第一批四指数）。
# id 集与 datasource._INDEX_FETCH 拉取清单对拍（tests/test_factor_index_trend.py 锚定）；
# registry 登记形态（engines.index_trend.metrics）由 engines.base._index_metrics 导出。
INDEX_TREND_INDEXES = ["ndx", "csi300", "hstech", "chinext"]
INDEX_TREND_META = {
    "ndx":     {"name": "纳斯达克综合", "market": "US"},
    "csi300":  {"name": "沪深300", "market": "A"},
    "hstech":  {"name": "恒生科技", "market": "HK"},
    "chinext": {"name": "创业板指", "market": "A"},
}
INDEX_TREND_REGIME = {"quantile_win": 252, "ma_fast": 20, "ma_slow": 60,
                      "pos_high": 80.0, "pos_low": 20.0}

# 五维默认权重（锚点：与历史硬编码一致；R1 敏感度扫描通过 compute_alpha(weights=...) 覆盖）
DEFAULT_DIM_WEIGHTS = {"估值": .25, "质量": .25, "成长": .20, "动量": .15, "红利低波": .15}


# ── 工具 ──────────────────────────────────────────────
def _interp(x, anchors):
    """分段线性插值打分：anchors=[(x,score),...] 升序"""
    if x is None:
        return None
    if x <= anchors[0][0]:
        return anchors[0][1]
    if x >= anchors[-1][0]:
        return anchors[-1][1]
    for (x0, s0), (x1, s1) in itertools.pairwise(anchors):
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


def _pearson(xs: list[float], ys: list[float]) -> float | None:
    """皮尔逊相关系数；样本 < 3 或任一常数列 → None（P1.3 量价相关性用）。"""
    n = len(xs)
    if n < 3:
        return None
    mx, my = sum(xs) / n, sum(ys) / n
    vx = sum((x - mx) ** 2 for x in xs)
    vy = sum((y - my) ** 2 for y in ys)
    if vx == 0 or vy == 0:
        return None
    return sum((x - mx) * (y - my) for x, y in zip(xs, ys)) / (vx * vy) ** 0.5


def _pctile(hist: list[float], cur: float | None) -> float | None:
    """当前值在历史序列中的分位（0–1，并列取 0.5 权重）；空序列或 cur=None → None。"""
    if cur is None or not hist:
        return None
    return (sum(1 for v in hist if v < cur) + 0.5 * sum(1 for v in hist if v == cur)) / len(hist)


def rev(secid: str, bars: list[dict], n: int, as_of: str | None = None) -> float | None:
    """n 交易日反转因子：过去 n 日累计收益取负（超跌为正 → 预期均值回复，P1.1）。

    行业中性化在截面评估层完成（本函数为单标的取值）；当前 α universe 仅 4 只股票且各属
    不同行业 → 行业中性退化为横截面去均值，见 ``industry_neutralize``。
    """
    closes = [b["close"] for b in bars if b.get("close") and (as_of is None or b["dt"] <= as_of)]
    if len(closes) < n + 1:
        return None
    return -(closes[-1] / closes[-1 - n] - 1)


def industry_neutralize(vals: dict[str, float | None], industries: dict[str, str]) -> dict[str, float | None]:
    """行业内去均值（行业中性化，P1.1）：同行业截面减行业均值；单只成组 → 减自身 → 0。

    universe 扩张后无损接入；当前 α universe（4 股/4 行业）中性后趋 0，属数据面限制。
    """
    groups: dict[str, list[str]] = {}
    for s, v in vals.items():
        if v is not None:
            groups.setdefault(industries.get(s) or "—", []).append(s)
    out: dict[str, float | None] = {}
    for s, v in vals.items():
        if v is None:
            out[s] = None
            continue
        g = groups.get(industries.get(s) or "—", [])
        gm = sum(vals[x] for x in g) / len(g) if g else 0.0
        out[s] = v - gm
    return out


def quality_factors(R: dict | None) -> dict:
    """品质族补齐（P1.2）：偿债 / 应计质量 / 自由现金流率 —— 取自 A 股三表科目（PIT 报告期）。

    - ``qua_leverage``：资产负债率 = 负债合计/资产总计（%，越低越优）
    - ``qua_accrual``：应计质量 = 经营活动现金流净额/净利润（越高越优，盈利含金量）
    - ``qua_fcf``：自由现金流率 = (经营现金流净额−资本开支)/营业总收入（%，越高越优）
    三表科目为 YTD 累计口径 → 比值为同期口径自洽；科目缺失（非 A 股）→ None（不适用）。
    """
    if not R:
        return {"qua_leverage": None, "qua_accrual": None, "qua_fcf": None}
    ta, tl = R.get("total_assets"), R.get("total_liab")
    ocf, capex, rev, np_ = R.get("ocf"), R.get("capex"), R.get("revenue"), R.get("net_profit")
    lev = (tl / ta * 100) if (ta and tl is not None and ta > 0) else None
    accr = (ocf / np_) if (ocf is not None and np_ and np_ > 0) else None
    fcf = ((ocf - (capex or 0.0)) / rev * 100) if (ocf is not None and rev and rev > 0) else None
    return {"qua_leverage": lev, "qua_accrual": accr, "qua_fcf": fcf}


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
    """TTM 摊薄 EPS；港股/美股取上游直出 ``eps_ttm``，A 股按 YTD 递推。

    - 港股/美股：报告自带 ``eps_ttm``（港股 EPS_TTM 列 / 美股近 4 季合计）——
      美股单季报口径下 YTD 递推不成立，凡带该键即以其为准（缺失则降级 None）。
    - A 股（无 ``eps_ttm`` 键）：上年FY + 本年YTD − 上年同期YTD（FY 报告期直接取全年）。
    """
    R = latest_report(reports, t)
    if R is None:
        return None
    if "eps_ttm" in R:
        return R["eps_ttm"]
    if R.get("eps_ytd") is None:
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
# 引擎 A 评分锚点（T-R5 抽出：原文照抄自 compute_alpha 内联字面量，真源迁移到模块级；
# s_val 无锚点表——公式 100−分位×100，注册表只登记下列五组）
_ALPHA_ANCHORS: dict[str, list[tuple[float, float]]] = {
    "s_qua": [(0, 10), (5, 35), (10, 55), (15, 75), (20, 90), (30, 100)],
    "s_gro": [(-20, 10), (0, 40), (10, 60), (20, 75), (35, 90), (60, 100)],
    "s_mom": [(-30, 10), (-10, 30), (0, 45), (10, 60), (30, 80), (60, 100)],
    "s_dvd": [(0, 10), (1, 35), (2, 55), (3, 70), (5, 90), (7, 100)],
    "s_lvol": [(15, 100), (20, 85), (30, 60), (40, 40), (60, 20)],
}


def compute_alpha(secid: str, bars: list[dict], reports: list[dict], dividends: list[dict],
                  as_of: str | None = None, weights: dict | None = None,
                  anchors: dict | None = None, active_ids: set | None = None) -> dict:
    """bars/reports 按 dt/announce 升序；返回 as_of 的引擎 A 全因子 + 五维评分

    ``weights``：五维权重（默认 None = 内置 ``DEFAULT_DIM_WEIGHTS``，R1 敏感度扫描可配）；
    ``anchors``：评分锚点注入（T-R5；None → 内置 ``_ALPHA_ANCHORS``，按键合并——只传需
    覆盖的组，形如 {"s_mom": [[-30, 10], ...]}）；
    ``active_ids``：因子启停白名单（T-R5；None → 全部 active）。inactive 因子计算照常，
    但退出对应维度评分输入、返回值置 None，并在 ``flags`` 键追加 "factor_<id>_inactive"
    （无 inactive 因子时不产生该键——默认输出与历史版本逐位一致，锚点纪律）。
    """
    closes = [(b["dt"], b["close"]) for b in bars if b["close"]]
    # 空序列透明降级（Q-fix）：无任何收盘 → 全字段 None + insufficient_history，评估不崩溃
    if not closes:
        return {
            "as_of": as_of, "close": None, "dims": {}, "grades": {},
            "composite": None, "composite_grade": None,
            "used_period": None, "used_announce": None, "staleness_days": None,
            "flags": ["insufficient_history（无可用行情序列）"],
        }
    as_of = as_of or closes[-1][0]
    hist = [(d, c) for d, c in closes if d <= as_of]
    close = hist[-1][1]
    act = set(active_ids) if active_ids is not None else None

    def _v(fid: str, val):
        """inactive 因子：评分输入/返回值视为 None（计算照常，仅参与面剔除）"""
        return None if (act is not None and fid not in act) else val

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

    # P1.1 反转族：过去 20/60 交易日累计收益取负（超跌为正，预期均值回复）
    rev_20d = -(cs[-1] / cs[-21] - 1) if len(cs) >= 21 else None
    rev_60d = -(cs[-1] / cs[-61] - 1) if len(cs) >= 61 else None

    # P1.4 微观流动性族（诊断，不入维度评分）：窗口内 bar 序列（≤ as_of）
    lb = [b for b in bars if b.get("dt") and b.get("close") and b["dt"] <= as_of]

    def _amihud(n: int = 20) -> float | None:
        """Amihud (2002) 非流动性：mean(|r_t| / amount_t) × 1e9（亿元/日成交额口径）。
        窗口内任一日 amount 缺失/非正 → None（不适用，透明降级）。"""
        seg = lb[-(n + 1):]
        if len(seg) < n + 1:
            return None
        tot = 0.0
        for i in range(1, len(seg)):
            amt = seg[i].get("amount")
            if not amt or amt <= 0:
                return None
            tot += abs(seg[i]["close"] / seg[i - 1]["close"] - 1) / amt
        return tot / n * 1e9

    def _roll(n: int = 60) -> float | None:
        """Roll (1984) 隐含价差：2√(−cov(Δr_t, Δr_{t−1})) × 100（%）；cov ≥ 0 时估计无定义 → None。
        取最近 n 对协方差（n+3 根 bar）：63 根 → 62 收益 → 61 差分 → 60 对。"""
        seg = lb[-(n + 3):]
        if len(seg) < n + 3:
            return None
        rets = [seg[i]["close"] / seg[i - 1]["close"] - 1 for i in range(1, len(seg))]
        dr = [rets[i] - rets[i - 1] for i in range(1, len(rets))]
        a, b = dr[1:], dr[:-1]
        ma, mb = sum(a) / len(a), sum(b) / len(b)
        cov = sum((x - ma) * (y - mb) for x, y in zip(a, b)) / (len(a) - 1)
        return 2.0 * (-cov) ** 0.5 * 100 if cov < 0 else None

    def _cs_spread(n: int = 60) -> float | None:
        """Corwin-Schultz (2012) 高低价价差（%）：单日 log 高低幅平方 β 与两日跨幅 γ 聚合 →
        α = (√(2β)−√β)/(3−2√2) − √(γ/(3−2√2))，spread = 2(e^α−1)/(1+e^α)；α<0 按文献惯例取 0。
        窗口内任一日 high/low 缺失或非正 → None。"""
        seg = lb[-(n + 1):]
        if len(seg) < n + 1:
            return None
        betas, gammas = [], []
        for i in range(1, len(seg)):
            h1, l1, h2, l2 = (seg[i - 1].get("high"), seg[i - 1].get("low"),
                              seg[i].get("high"), seg[i].get("low"))
            if not (h1 and l1 and h2 and l2 and min(h1, l1, h2, l2) > 0):
                return None
            betas.append(math.log(h1 / l1) ** 2 + math.log(h2 / l2) ** 2)
            gammas.append(math.log(max(h1, h2) / min(l1, l2)) ** 2)
        beta = sum(betas) / len(betas)
        gamma = sum(gammas) / len(gammas)
        k = 3.0 - 2.0 * 2.0 ** 0.5
        alpha = (2.0 ** 0.5 - 1.0) * beta ** 0.5 / k - (gamma / k) ** 0.5
        if alpha < 0:
            return 0.0
        return 2.0 * (math.exp(alpha) - 1.0) / (math.exp(alpha) + 1.0) * 100

    liq_amihud_20d = _amihud()
    liq_roll_60d = _roll()
    liq_cs_60d = _cs_spread()

    # P1.2 品质族补齐：三表科目（仅 A 股有；缺失 → None 不适用）
    qf = quality_factors(R)

    # 五维评分（0-100，锚点见 report 说明；T-R5：锚点/因子启停注册表驱动）
    anc = {**_ALPHA_ANCHORS, **anchors} if anchors else _ALPHA_ANCHORS
    _vals = [100 - p * 100 if p is not None else None
             for p in (_v("val_pe_pct", pe_pct), _v("val_pb_pct", pb_pct))]
    s_val = ((_vals[0] + _vals[1]) / 2
             if (_vals[0] is not None and _vals[1] is not None)
             else (_vals[0] if _vals[0] is not None else _vals[1]))
    s_qua = _interp(_v("qua_roe_ttm", roe_ttm), anc["s_qua"])
    gs = [R.get("rev_g"), R.get("np_g")] if R else [None, None]
    gs = [g for fid, g in zip(("grow_rev_yoy", "grow_np_yoy"), gs) if _v(fid, g) is not None]
    s_gro = _interp(sum(gs) / len(gs), anc["s_gro"]) if gs else None
    s_mom = _interp(_v("mom_12_1", mom_12_1), anc["s_mom"])
    s_dvd = _interp(_v("div_yield_ttm", div_yield * 100 if div_yield is not None else None), anc["s_dvd"])
    s_lvol = _interp(_v("vol_250", vol_250 * 100 if vol_250 is not None else None), anc["s_lvol"])
    s_dl = (s_dvd + s_lvol) / 2 if (s_dvd is not None and s_lvol is not None) else (s_dvd if s_dvd is not None else s_lvol)
    dims = {"估值": s_val, "质量": s_qua, "成长": s_gro, "动量": s_mom, "红利低波": s_dl}
    W = weights or DEFAULT_DIM_WEIGHTS
    w_sum = sum(W[k] for k in W if dims[k] is not None)
    tot = sum(dims[k] * W[k] for k in W if dims[k] is not None) / w_sum if w_sum else None
    if R is None:
        tot = None   # 无财报数据（ETF/LOF/指数类）：五维综合不适用（动量/波动因子照常计算）

    out = {
        "as_of": as_of, "close": close,
        "val_pe_ttm": pe, "val_pb": pb, "val_pe_pct": pe_pct, "val_pb_pct": pb_pct,
        "div_yield_ttm": div_yield, "qua_roe_ttm": roe_ttm,
        "qua_margin": R.get("margin") if R else None,
        "grow_rev_yoy": R.get("rev_g") if R else None,
        "grow_np_yoy": R.get("np_g") if R else None,
        "mom_12_1": mom_12_1, "vol_250": vol_250,
        # P1.1 反转 / P1.2 品质补齐
        "rev_20d": rev_20d, "rev_60d": rev_60d,
        "qua_leverage": qf["qua_leverage"], "qua_accrual": qf["qua_accrual"], "qua_fcf": qf["qua_fcf"],
        # P1.4 微观流动性族（诊断，不入维度评分）
        "liq_amihud_20d": liq_amihud_20d, "liq_roll_60d": liq_roll_60d, "liq_cs_60d": liq_cs_60d,
        "used_period": R["period"] if R else None,
        "used_announce": R["announce_date"] if R else None,
        "pe_hist_n": len(pe_win), "pb_hist_n": len(pb_win),
        "dims": {k: round(v, 1) if v is not None else None for k, v in dims.items()},
        "grades": {k: _grade(v) for k, v in dims.items()},
        "composite": round(tot, 1) if tot is not None else None,
        "composite_grade": _grade(tot),
    }
    if act is not None:                       # T-R5：inactive 因子值置 None + flags 标记
        flags = [f"factor_{fid}_inactive" for fid in ENGINE_A_FACTORS if fid not in act]
        for fid in ENGINE_A_FACTORS:
            if fid not in act:
                out[fid] = None
        if flags:
            out["flags"] = flags
    return out


# ── 引擎 B ────────────────────────────────────────────
# 引擎 B 信号运行时定义（T-R4 抽出：原文照抄自 compute_beta 内联字面量，真源迁移到模块级）
# - type/action/detail 可按方向分支（below/above、pos/neg）；detail 为 str.format 模板，
#   数值键 {ratio, band_pct, n, close, bias_pct, vp_corr, vpd_thresh} 运行时注入；
# - status != active 的信号不生成；signal_specs 注入（engines/beta.py 装配）按 id 覆盖。
_BETA_SIGNALS: list[dict] = [
    {"id": "sig_ma20_break", "status": "active", "evidence": "E2", "cooldown": "10 交易日",
     "type": {"below": "卖出预警", "above": "买入/持有确认"},
     "action": {"below": "生成减仓评估单（不自动卖出）", "above": "引擎A目标仓位执行确认（模式③）"},
     "detail": {"below": "收盘低于 MA20 {ratio:+.1%}（{band_pct} 确认带外）",
                "above": "收盘高于 MA20 {ratio:+.1%}（{band_pct} 确认带外）"}},
    {"id": "sig_high20_touch", "status": "active", "evidence": "E2", "cooldown": "10 交易日",
     "type": "持有确认", "action": "趋势延续确认，持有（引擎A持仓内）",
     "detail": "收盘创 {n} 日收盘新高（{close:.2f}）"},
    {"id": "sig_bias_ext", "status": "active", "evidence": "E2", "cooldown": "10 交易日",
     "type": {"pos": "卖出预警", "neg": "买入/持有确认"},
     "action": "反转风险提示：超买减仓评估 / 超卖关注回归（不自动交易）",
     "detail": {"pos": "乖离率 {ratio:+.1%}（|乖离| ≥ {bias_pct:g}%，超买极端）",
                "neg": "乖离率 {ratio:+.1%}（|乖离| ≥ {bias_pct:g}%，超卖极端）"}},
    {"id": "sig_vol_price_div", "status": "active", "evidence": "E2", "cooldown": "10 交易日",
     "type": "卖出预警", "action": "趋势可持续性存疑：减仓评估（不自动卖出）",
     "detail": "20 日量价相关性 {vp_corr:+.2f} ≤ −{vpd_thresh:g}（价升量缩/量价背离）"},
]
_BETA_SIGNAL_BASE = {s["id"]: s for s in _BETA_SIGNALS}


def compute_beta(secid: str, bars: list[dict], as_of: str | None = None,
                 win: int = 20, band: float = 0.02, band_sigma: float = 1.5,
                 bias_thresh: float = 0.08, vpd_thresh: float = 0.30,
                 pct_window: int = 250,
                 signal_specs: list[dict] | None = None) -> dict:
    """引擎 B：20 日窗口信号（参数化）。

    - ``win``：MA / 动量 / 高点窗口（默认 20，R1 敏感度扫描可配）
    - ``band``：MA 突破确认带宽（默认 ±2%，R1 可配）
    - ``band_sigma``：波动止损带宽（默认 1.5σ，R1 可配）
    - ``bias_thresh``：极端乖离阈值（默认 ±8%，P1.1 sig_bias_ext）
    - ``vpd_thresh``：量价背离阈值（20 日量价相关性 ≤ −阈值，默认 0.30，P1.3）
    - ``pct_window``：换手率代理分位窗口（默认 250 交易日，P1.3）
    - ``signal_specs``：信号定义注入（T-R4；None → 内置 _BETA_SIGNALS）。每条
      {id, status, type, action, detail, evidence, cooldown}，status≠active 不生成；
      未列出的信号不生成；字段缺省/分支缺失回退内置文案，detail 数值运行时格式化。

    默认参数下既有字段（mom_20d/vol_20d/ma20_break/…/state）与历史版本逐位一致（锚点纪律）。
    """
    n_req = int(win)
    closes = [(b["dt"], b["close"]) for b in bars if b["close"]]
    highs = [(b["dt"], b["high"]) for b in bars if b.get("high")]
    as_of = as_of or (closes[-1][0] if closes else None)
    hist_c = [c for d, c in closes if d <= as_of]
    hist_h = [h for d, h in highs if d <= as_of]
    hist_l = [b["low"] for b in bars if b["dt"] <= as_of and b.get("low")]
    hist_amt = [b.get("amount") for b in bars if b.get("close") and b["dt"] <= as_of]   # 与 hist_c 同序对齐
    close = hist_c[-1]

    # 短序列透明降级（Q-fix：新上市/夹具标的 bars < 2 根无法构成任何收益窗口）：
    # 输出结构与正常一致（字段置 None + insufficient_history 标记），评估继续不崩溃。
    if len(hist_c) < 2:
        return {
            "as_of": as_of, "close": close, "mom_20d": None, "vol_20d": None,
            "ma20": None, "ma20_break": None, "state": "insufficient_history",
            "high20_touch": 0, "dist_high20": None, "high20_intra": None, "low20": None,
            "turnover_pct": None, "vol_price_corr": None, "sigma_daily": None,
            "band_low": None, "band_high": None, "vol_stop": False,
            "ma_window": 0, "band": band, "band_sigma": band_sigma,
            "bias_thresh": bias_thresh, "vpd_thresh": vpd_thresh, "pct_window": int(pct_window),
            "window_requested": n_req, "insufficient_history": True, "signals": [],
        }
    # 窗口自适应：n_eff = min(请求窗口, 可用收益点数)（Q-fix——夹具/新上市 ETF 仅 6 根
    # 历史时按 5 日收益计算而非越界崩溃；正常标的 n_eff == n_req，行为逐位不变）
    n = min(n_req, len(hist_c) - 1)
    w = hist_c[-n:]
    ma20 = sum(w) / n
    ratio = close / ma20 - 1
    mom = close / w[0] - 1
    rets20 = [hist_c[i] / hist_c[i - 1] - 1 for i in range(len(hist_c) - n, len(hist_c))]
    mu = sum(rets20) / len(rets20)
    vol20 = (sum((r - mu) ** 2 for r in rets20) / (len(rets20) - 1)) ** 0.5 * (252 ** 0.5) if len(rets20) > 1 else 0.0
    high20_close = max(w)
    high20_intra = max(hist_h[-n:]) if hist_h else close   # 无 high 列（夹具/降级源）→ 以收盘代
    low20 = min(hist_l[-n:]) if hist_l else close
    high20_touch = 1 if close >= high20_close else 0
    dist_high20 = close / high20_intra - 1
    band_pct = f"±{band * 100:g}%"

    # P1.3 量价情绪：20 日量价相关性（日收益 vs 成交额变化）+ 换手率代理分位（成交额 pct_window 日分位）
    r_all = [None] * len(hist_c)
    da = [None] * len(hist_c)
    for i in range(1, len(hist_c)):
        r_all[i] = hist_c[i] / hist_c[i - 1] - 1
        a0, a1 = hist_amt[i - 1], hist_amt[i]
        da[i] = (a1 / a0 - 1) if (a0 not in (None, 0) and a1 is not None) else None
    idx = [i for i in range(len(hist_c) - n, len(hist_c)) if r_all[i] is not None and da[i] is not None]
    vp_corr = _pearson([r_all[i] for i in idx], [da[i] for i in idx])
    amt_hist = [a for a in hist_amt if a is not None][-int(pct_window):]
    turnover_pct = _pctile(amt_hist, hist_amt[-1] if hist_amt else None)   # 换手率代理：成交额分位

    if ratio < -band:
        state = "below"
    elif ratio > band:
        state = "above"
    else:
        state = "neutral"

    # 信号生成（T-R4 注册表化）：触发条件/分支键是公式语义（内嵌于此，不随 spec 注入）；
    # spec 只管启停（status）与文案（type/action/detail/cooldown/evidence），缺省回退内置。
    specs = _BETA_SIGNALS if signal_specs is None else signal_specs
    fmt = {"ratio": ratio, "band_pct": band_pct, "n": n, "close": close,
           "bias_pct": bias_thresh * 100, "vp_corr": vp_corr, "vpd_thresh": vpd_thresh}
    sigs: list[dict] = []
    for sp in specs:
        if not isinstance(sp, dict) or sp.get("status", "active") != "active":
            continue                                  # 停用/实验信号：不生成（含 iter_signal_events）
        sid = sp.get("id")
        base = _BETA_SIGNAL_BASE.get(sid)
        if base is None:
            continue                                  # 公式真源无此信号的触发定义 → 不生成
        if sid == "sig_ma20_break":
            key = "below" if ratio < -band else ("above" if ratio > band else None)
        elif sid == "sig_high20_touch":
            key = "" if high20_touch else None
        elif sid == "sig_bias_ext":
            key = None if abs(ratio) < bias_thresh else ("pos" if ratio > 0 else "neg")
        elif sid == "sig_vol_price_div":
            key = "" if (vp_corr is not None and vp_corr <= -vpd_thresh) else None
        else:
            key = None
        if key is None:
            continue

        def _fld(name, _sp=sp, _base=base, _key=key):  # spec → 内置 回退；dict 分支缺失同回退
            v = _sp.get(name, _base.get(name))
            if isinstance(v, dict):
                bd = _base.get(name)
                v = v.get(_key, bd.get(_key) if isinstance(bd, dict) else bd)
            return v

        sigs.append({"signal_id": sid, "type": _fld("type"),
                     "detail": str(_fld("detail")).format(**fmt),
                     "evidence": _fld("evidence"),
                     "action": _fld("action"), "cooldown": _fld("cooldown")})

    sd = vol20 / (252 ** 0.5)  # 日波动
    return {
        "as_of": as_of, "close": close, "mom_20d": mom, "vol_20d": vol20,
        "ma20": ma20, "ma20_break": ratio, "state": state,
        "high20_touch": high20_touch, "dist_high20": dist_high20,
        "high20_intra": high20_intra, "low20": low20,
        # P1.3 量价情绪
        "turnover_pct": turnover_pct, "vol_price_corr": vp_corr,
        "sigma_daily": sd,
        "band_low": ma20 * (1 - band_sigma * sd), "band_high": ma20 * (1 + band_sigma * sd),
        "vol_stop": bool(close < ma20 * (1 - band_sigma * sd)),
        "ma_window": n, "band": band, "band_sigma": band_sigma,
        "window_requested": n_req,
        "bias_thresh": bias_thresh, "vpd_thresh": vpd_thresh, "pct_window": int(pct_window),
        "signals": sigs,
    }


# 引擎 B 信号事件 id（R4 延迟衰减 / R1 敏感度共用）
ENGINE_B_SIGNAL_IDS = ["sig_ma20_break", "sig_high20_touch", "sig_bias_ext", "sig_vol_price_div"]


def iter_signal_events(secid: str, bars: list[dict], signal_id: str = "all", horizon: int = 20,
                       win: int = 20, band: float = 0.02, band_sigma: float = 1.5,
                       start: str | None = None, end: str | None = None,
                       signal_specs: list[dict] | None = None) -> list[dict]:
    """阶段〇 0.3：逐日 as_of 调用参数化 ``compute_beta`` 识别全部历史信号事件。

    仅在该标的自身有效 K 线（``close`` 为真）序列上循环；每个事件带四种执行价
    ``t0_close / t1_open / t1_close / t2_open``（不足则 None）与固定窗口退出价
    ``exit_close``（信号后第 ``horizon`` 个交易日收盘），供 R4 延迟衰减实证使用。
    ``signal_specs`` 透传 compute_beta（T-R4：status≠active 的信号同步跳过）；
    循环内传同一引用，避免逐日重建。

    默认参数下事件与 ``compute_beta`` 逐日输出一致（锚点纪律）。
    """
    valid = [b for b in bars if b.get("close")]
    h = int(horizon)
    evs = []
    for i, b in enumerate(valid):
        d = b["dt"]
        if (start and d < start) or (end and d > end):
            continue
        if i < int(win):          # 窗口不足（compute_beta 需 ≥ win+1 根）→ 无信号可识别
            continue
        r = compute_beta(secid, bars, d, win, band, band_sigma, signal_specs=signal_specs)
        if not r["signals"]:
            continue
        n1 = valid[i + 1] if i + 1 < len(valid) else None
        n2 = valid[i + 2] if i + 2 < len(valid) else None
        fwd = valid[i + 1:i + h + 1]
        exit_close = fwd[-1]["close"] if len(fwd) >= h else None
        for s in r["signals"]:
            if signal_id != "all" and s["signal_id"] != signal_id:
                continue
            evs.append({
                "secid": secid, "dt": d, "signal_id": s["signal_id"], "type": s["type"],
                "exec_prices": {"t0_close": b["close"],
                                "t1_open": n1.get("open") if n1 else None,
                                "t1_close": n1["close"] if n1 else None,
                                "t2_open": n2.get("open") if n2 else None},
                "exit_close": exit_close,
            })
    return evs


# ── 港美利率/汇率 regime（H-0006 · 市场级条件变量）────────────
PEG_STRONG, PEG_WEAK = 7.75, 7.85            # 港元联系汇率强方/弱方兑换保证
PEG_SCALE = PEG_WEAK - PEG_STRONG            # 兑换区间宽度（0.10）
HK_CARRY_WIDEN_PT = 0.5                      # 「港美息差急扩」阈值（20 日变化，百分点）


def compute_hk_regime(series: list[dict], as_of: str | None = None, chg_win: int = 20,
                      peg_strong: float = PEG_STRONG, peg_weak: float = PEG_WEAK,
                      carry_widen_pt: float = HK_CARRY_WIDEN_PT) -> dict:
    """港美息差（``carry_hk_us``）与弱方兑换保证压力（``peg_pressure``）——市场级 regime 条件变量。

    机制（H-0006）：美元加息 → 港美利差走阔、港元趋弱 → 资金由港股回流美元资产 → 港股承压。
    两个载体是**条件变量**（择时），非横截面选股因子；仅在极端态（港元逼近弱方保证 / 息差急扩）触发。

    - ``carry_hk_us`` = HIBOR 1M − 美联储目标利率（百分点）：越低（越负）→ 美元资产相对港元资产的
      利差越阔 → 套息外流压力越大
    - ``carry_chg20`` = carry_hk_us 的 ``chg_win`` 交易日变化（百分点）：越负 → 息差急扩（美元相对港元抬升）
    - ``peg_pressure`` = (USDHKD − peg_strong)/(peg_weak − peg_strong)：0=强方保证，1=弱方保证；
      越高 → 港元越弱、越接近弱方兑换保证（T-R3 起阈值可注入，默认 = 模块常量 7.75/7.85）
    - ``state``：``outflow_pressure``（港元触弱方保证 USDHKD≥peg_weak，或息差 ``chg_win`` 日
      急扩 ≥ carry_widen_pt） / ``neutral``

    ``series`` 为 ``market/hk_rates.json`` 的逐日序列（字段可缺失，按 as_of 前向填充）；as_of=None 取末点。
    """
    as_of = as_of or (series[-1]["dt"] if series else None)
    pts = [r for r in series if r.get("dt") and (as_of is None or r["dt"] <= as_of)]
    hibor = next((r["hibor1m"] for r in reversed(pts) if r.get("hibor1m") is not None), None)
    fed = next((r["fed"] for r in reversed(pts) if r.get("fed") is not None), None)
    usdhkd = next((r["usdhkd"] for r in reversed(pts) if r.get("usdhkd") is not None), None)

    carry = (hibor - fed) if (hibor is not None and fed is not None) else None
    peg = ((usdhkd - peg_strong) / (peg_weak - peg_strong)) if usdhkd is not None else None

    carries = [r["hibor1m"] - r["fed"] for r in pts
               if r.get("hibor1m") is not None and r.get("fed") is not None]
    carry_chg = (carries[-1] - carries[-1 - chg_win]) if len(carries) > chg_win else None

    peg_weak_side = peg is not None and peg >= 1.0                            # USDHKD ≥ peg_weak：触弱方保证
    carry_widen = carry_chg is not None and carry_chg <= -carry_widen_pt      # 息差急扩（走阔）
    return {
        "as_of": as_of, "chg_win": int(chg_win),
        "carry_hk_us": carry, "carry_chg20": carry_chg,
        "peg_pressure": peg, "usdhkd": usdhkd, "hibor1m": hibor, "fed": fed,
        "peg_weak_side": peg_weak_side, "carry_widen": carry_widen,
        "state": "outflow_pressure" if (peg_weak_side or carry_widen) else "neutral",
    }


# ── 引擎C：宏观流动性三层聚合（纯函数，零 IO；T-C2）──────────────

# 指标 → 维度归属（冻结，§4.2）；市场不适用的维度直接缺席，权重在市场分中重归一化
_MACRO_DIM_OF: dict[str, dict[str, list[str]]] = {
    "A":  {"D1": ["rrr_big"], "D2": ["m2_yoy", "tsf_new_12m"],
           "D3": ["shibor_1m", "lpr_1y"], "D4": ["cn_us_10y"]},
    "HK": {"D3": ["hibor_1m"], "D4": ["carry_hk_us", "peg_pressure"]},      # D1/D2 不适用
    "US": {"D1": ["fed_target", "us_cpi_surprise"], "D3": ["us_10y_2y"]},   # D2 免费源缺失 / D4 缺席
}
# 指标 → (scorer, 参数)：quant=分位法 / mom=边际方向(尺度) / step=阶梯方向(衰减期) /
# clip=直接映射(尺度) / pulse=阈值脉冲（正=宽松）
_MACRO_SCORERS: dict[str, tuple] = {
    "shibor_1m": ("quant", None), "us_10y_2y": ("quant", None),
    "hibor_1m": ("quant", None),
    "carry_hk_us": ("clip", -1.0),              # score = clip(−carry/1.0)：负息差=紧
    "peg_pressure": ("clip", -1.0),             # score = −peg：触弱方=紧
    "m2_yoy": ("mom", 0.5), "tsf_new_12m": ("mom", 20000.0),
    "lpr_1y": ("step", 6), "rrr_big": ("step", 0), "fed_target": ("step", 0),
    "cn_us_10y": ("clip", 1.5), "us_cpi_surprise": ("pulse", None),
}


def _ml_clip(x: float | None, hi: float) -> float | None:
    """线性映射并截断到 [-1, +1]（正=宽松）。"""
    return None if x is None else max(-1.0, min(1.0, x / hi))


def _ml_level(s: float | None) -> int | None:
    """score → 五档位：−2 深/−1 偏/0 中/1 偏/2 深度宽松（对称 ±0.2/±0.6）。"""
    if s is None:
        return None
    if s <= -0.6:
        return -2
    if s <= -0.2:
        return -1
    if s >= 0.6:
        return 2
    if s >= 0.2:
        return 1
    return 0


def _ml_score(kind: str, arg, pts: list, quantile_win: int) -> float | None:
    """指标打分（正=宽松）；pts=[(dt, v)] 升序且已剔 None。"""
    if not pts:
        return None
    vals = [v for _, v in pts]
    if kind == "quant":                        # 分位法：高位=紧（利率/倒挂）
        q = _pctile(vals[-quantile_win:], vals[-1])
        return None if q is None else 1.0 - 2.0 * q
    if kind == "mom":                          # 边际方向：近 3 期差分均值 / 尺度（±scale 打满）
        if len(vals) < 3:
            return None
        return _ml_clip((vals[-1] - vals[-3]) / 2.0, arg) if arg else None
    if kind == "step":                         # 阶梯方向：最近一次变动（降=宽松=+1）
        for i in range(len(vals) - 1, 0, -1):
            if vals[i] != vals[i - 1]:
                if arg and (len(vals) - 1 - i) >= arg:   # 变动距今 ≥ 衰减期 → 中性（LPR）
                    return 0.0
                return 1.0 if vals[i] < vals[i - 1] else -1.0
        return 0.0                             # 全历史未动
    if kind == "clip":                         # 直接映射（cn_us_10y：走阔=宽松方向）
        return _ml_clip(vals[-1], arg)
    if kind == "pulse":                        # 阈值脉冲：CPI 超预期=紧缩路径延长=紧
        return -1.0 if vals[-1] > 0 else (1.0 if vals[-1] < 0 else 0.0)
    return None


def compute_macro_liquidity(series: dict, hk_series: list | None = None,
                            as_of: str | None = None, quantile_win: int = 252,
                            market_weights: dict | None = None,
                            meta: dict | None = None,
                            dim_of: dict | None = None, scorers: dict | None = None,
                            dim_weights: dict | None = None,
                            active_ids: set | None = None) -> dict:
    """宏观流动性三层聚合（指标打分 → 维度分 → 市场档位/全局指数/极端态）。

    纯函数：入参 dict → 出参 dict，零 IO / 零网络 / 确定性。打分方向**正=宽松**。

    - ``series``：``market/macro_liquidity.json`` 的 series（{key: [{dt, v}]}）
    - ``hk_series``：``market/hk_rates.json`` 的 series（None → HK 维度 unavailable）
    - ``market_weights``：市场权重（T-C4 按持仓数传入；None → 三市场等权）
    - ``meta``：T-C1 落盘 JSON 的 meta（提供 source/freq；缺省则 indicators 精简）
    - ``dim_of``：指标 → {market: {dim: [ids]}}（T-R3 注入；None → 内置 _MACRO_DIM_OF）
    - ``scorers``：指标 → (kind, arg)，同 _MACRO_SCORERS 形状（None → 内置；未登记指标
      沿用 ("clip", 1.0) 兜底，即背景指标语义）
    - ``dim_weights``：D1..D4 维度权重（None → 内置 MACRO_DIM_WEIGHTS；缺 D 键时按
      剩余权重重归一化）
    - ``active_ids``：status=active 指标白名单（None = 全部参与）。不在集合内的指标跳过
      打分：indicators 仍登记 value，score=None 且 missing="inactive"，维度 n_total
      只计 active（保持「维度缺失缩分母」语义）

    输出见 T-C2 契约：{as_of, markets{A/HK/US}, global{score,level,state}, indicators, notes}；
    某市场全部数据缺失 → 该市场 {"status": "unavailable"}；全市场缺失 → 顶层同款。
    不传新参（dim_of/scorers/dim_weights/active_ids 全 None）→ 与内置常量路径逐位一致。
    """
    # as_of 缺省 = 全部序列末点最大值；各序列清洗为 [(dt, v)] 升序、剔 None、按 as_of 截断
    all_dts = [r.get("dt") for rows in (series or {}).values() if isinstance(rows, list)
               for r in rows if isinstance(r, dict) and r.get("dt")]
    all_dts += [r.get("dt") for r in (hk_series or []) if isinstance(r, dict) and r.get("dt")]
    as_of = as_of or (max(all_dts) if all_dts else None)

    def _clean(rows) -> list:
        return sorted(((r["dt"], r["v"]) for r in (rows or [])
                       if isinstance(r, dict) and r.get("dt") and r.get("v") is not None
                       and (as_of is None or r["dt"] <= as_of)), key=lambda t: t[0])

    pts = {k: _clean(rows) for k, rows in (series or {}).items()}

    # T-R3 配置解析：None → 内置常量（默认路径与改造前逐位一致）
    dim_of = dim_of if dim_of is not None else _MACRO_DIM_OF
    scorers = scorers if scorers is not None else _MACRO_SCORERS
    dim_weights = dim_weights if dim_weights is not None else MACRO_DIM_WEIGHTS
    active = active_ids if active_ids is not None else None      # None = 全部参与

    # HK 序列（H-0006 同源）：hibor 分位 / carry=HIBOR−Fed / peg=联系汇率压力
    hk_pts = [r for r in (hk_series or [])
              if isinstance(r, dict) and r.get("dt") and (as_of is None or r["dt"] <= as_of)]
    pts["hibor_1m"] = [(r["dt"], r["hibor1m"]) for r in hk_pts if r.get("hibor1m") is not None]
    pts["carry_hk_us"] = [(r["dt"], r["hibor1m"] - r["fed"]) for r in hk_pts
                          if r.get("hibor1m") is not None and r.get("fed") is not None]
    peg_pts = [(r["dt"], (r["usdhkd"] - PEG_STRONG) / PEG_SCALE) for r in hk_pts
               if r.get("usdhkd") is not None]
    pts["peg_pressure"] = peg_pts

    # ── 第一层：逐指标打分 + indicators（us_cpi_yoy 的 score 引用 surprise 脉冲）──
    pulse = _ml_score("pulse", None, pts.get("us_cpi_surprise"), quantile_win)
    indicators: dict[str, dict] = {}
    for mkt, mk_dims in dim_of.items():
        ind: dict[str, dict] = {}
        mkt_keys = {k for ks in mk_dims.values() for k in ks}
        if mkt == "US":
            mkt_keys |= {"us_cpi_yoy"}                  # 背景值进 indicators（不进维度）
        for k in mkt_keys:
            last = pts.get(k, [])[-1] if pts.get(k) else None
            off = active is not None and k not in active            # 注册表停用 → 跳过打分
            score = None if off else (
                pulse if k == "us_cpi_yoy" else _ml_score(
                    *scorers.get(k, ("clip", 1.0)), pts.get(k, []), quantile_win))
            entry = {"value": last[1] if last else None, "score": score,
                     "as_of": last[0] if last else None,
                     "missing": "inactive" if off else (score is None and not pts.get(k))}
            if meta and meta.get(k):
                entry["source"] = meta[k].get("upstream") or meta[k].get("source")
                entry["freq"] = meta[k].get("freq")
            ind[k] = entry
        indicators[mkt] = ind

    # ── 第二层：维度分（非 None 均值；n_used/n_total 记录缩水）──
    markets: dict[str, dict] = {}
    for mkt, mk_dims in dim_of.items():
        dims: dict[str, dict] = {}
        for d, keys in mk_dims.items():
            sc = [indicators[mkt][k]["score"] for k in keys if indicators[mkt][k]["score"] is not None]
            n_total = sum(1 for k in keys if active is None or k in active)   # inactive 不进分母
            dims[d] = {"score": sum(sc) / len(sc) if sc else None, "n_used": len(sc), "n_total": n_total}
        eff = {d: w for d, w in dim_weights.items()
               if d in dims and dims[d]["score"] is not None}
        if not eff:
            markets[mkt] = {"status": "unavailable"}
            continue
        s = sum(w * dims[d]["score"] for d, w in eff.items()) / sum(eff.values())
        markets[mkt] = {"score": s, "level": _ml_level(s), "dims": dims}

    # ── 第三层：全局加权 + 极端态状态机 + notes ──
    if all((markets.get(m) or {}).get("status") == "unavailable" for m in dim_of):
        return {"status": "unavailable"}
    w_all = market_weights or {m: 1.0 for m in dim_of}       # 缺省三市场等权
    w_eff = {m: w for m, w in w_all.items() if m in dim_of
             and markets.get(m, {}).get("score") is not None and w}
    gs = (sum(w * markets[m]["score"] for m, w in w_eff.items()) / sum(w_eff.values())
          if w_eff else None)
    g_level = _ml_level(gs)
    dim = lambda m, d: (markets.get(m, {}).get("dims") or {}).get(d, {}).get("score")
    state = "neutral"
    if g_level is not None and g_level <= -2 and any(
            (dim(m, "D1") is not None and dim(m, "D1") <= -0.5) for m in dim_of):
        state = "tight_stress"
    elif g_level == 2 and any(
            (dim(m, "D2") is not None and dim(m, "D2") >= 0.5) for m in dim_of):
        state = "easy_froth"

    notes: list[str] = []
    if markets.get("US", {}).get("score") is not None:
        notes += ["D1 以政策利率+CPI 近似（个人版口径限制）", "US D2 维度缺席（免费源缺失）"]
    missing = sorted(k for m, ind in indicators.items()
                     if markets.get(m, {}).get("score") is not None     # unavailable 市场不逐指标刷缺失
                     for k, e in ind.items()
                     if e["missing"] and e["missing"] != "inactive")    # 停用 ≠ 数据缺失
    if missing:
        notes.append("缺失指标：" + "、".join(missing))
    stopped = sorted(k for m, ind in indicators.items()
                     if markets.get(m, {}).get("score") is not None
                     for k, e in ind.items() if e["missing"] == "inactive")
    notes += [f"指标 {k} 已停用（inactive），不参与打分" for k in stopped]
    return {"as_of": as_of, "markets": markets,
            "global": {"score": gs, "level": g_level, "state": state},
            "indicators": indicators, "notes": notes}


# ── 引擎D：指数级别趋势（纯函数，零 IO；2026-10-26 新 Goal）──────────────


def _it_ma(closes: list[float], n: int) -> float | None:
    """近 n 根简单均线；窗口不足 → None（透明降级）。"""
    return sum(closes[-n:]) / n if len(closes) >= n else None


def _it_ret(closes: list[float], n: int) -> float | None:
    """n 日区间收益 = 末收 / 前n收 - 1；序列不足 → None。"""
    return closes[-1] / closes[-1 - n] - 1 if len(closes) > n and closes[-1 - n] else None


def compute_index_trend(bars: dict, as_of: str | None = None,
                        quantile_win: int = 252, ma_fast: int = 20, ma_slow: int = 60,
                        pos_high: float = 80, pos_low: float = 20,
                        active_ids: set | None = None,
                        meta: dict | None = None) -> dict:
    """指数级别趋势：当前位置 + 趋势判断（市场级条件变量，不产生买卖信号）。

    纯函数：入参 dict → 出参 dict，零 IO / 零网络 / 确定性（同引擎C 纪律）。

    - ``bars``：``market/index_bars.json`` 的 series（{index_id: [{dt, close}, …]}）；
      清洗：剔 None 收盘、按 ``as_of`` 截断（PIT 口径，日线级别无前视）；
    - ``active_ids``：registry status=active 指数白名单（None = 全部参与）。
      停用指数仍登记行情（close/as_of）+ ``missing: "inactive"``，不产出
      趋势/位置判断（对齐引擎C inactive 语义：登记可见、不打分）；
    - ``meta``：index_bars.json 落盘 meta（{id: {name/market/source}}）透传。

    每指数输出（对照需求「当前的位置和趋势判断」）：
    - **位置**：``pos_q`` 末收在近 ``quantile_win`` 根收盘高低区间的百分位（0-100，
      平区间恒 50）、``dist_high`` / ``dist_low`` 距区间高/低点距离（负=低于）；
      ``position`` 三档：high_zone（≥ pos_high）/ low_zone（≤ pos_low）/ mid_zone；
    - **趋势**：``ret_fast``(20日) / ``ret_slow``(60日) 区间收益、``ma_fast`` / ``ma_slow``
      均线、``ma_fast_above``（末收 > 快线）、``ma_fast_above_slow``（快线 > 慢线，
      数据不足为 None 仅作观察）；``trend`` 三态判定：up（快线上方 且 20 日收益 > 0）
      / down（快线下方 且 20 日收益 < 0）/ range（方向不一致或证据缺失）；
    - **降级**：序列 < ma_fast + 1 根 → ``status: insufficient``（核心判断不适用）；
      序列空 → ``status: unavailable``；全部指数缺失 → 顶层 unavailable。

    输出：``{as_of, indexes: {id: {…}}}``；调用方（portfolio_review）失败透明降级。
    """
    if not isinstance(bars, dict) or not bars:
        return {"status": "unavailable", "error": "指数序列为空"}
    # as_of 缺省 = 各指数序列末点最大值（同引擎C 口径）
    all_dts = [r.get("dt") for rows in bars.values() if isinstance(rows, list)
               for r in rows if isinstance(r, dict) and r.get("dt")]
    as_of = as_of or (max(all_dts) if all_dts else None)

    def _clean(rows) -> list:
        return [(r["dt"], r["close"]) for r in (rows or [])
                if isinstance(r, dict) and r.get("dt") and r.get("close") is not None
                and (as_of is None or r["dt"] <= as_of)]

    indexes: dict[str, dict] = {}
    for key in sorted(bars):
        if not isinstance(bars[key], list):
            continue
        # meta 透传与数据可用性无关：降级分支（inactive/unavailable/insufficient）
        # 同样登记 name/market/source（报告/前端均按 name 展示，避免「chinext（—）」）
        m = meta.get(key) if (isinstance(meta, dict)
                              and isinstance(meta.get(key), dict)) else None
        nm = ({"name": m.get("name"), "market": m.get("market"),
               "source": m.get("source")} if m else {})
        pts = _clean(bars[key])
        if active_ids is not None and key not in active_ids:
            if pts:
                indexes[key] = {**nm, "status": "inactive", "missing": "inactive",
                                "close": pts[-1][1], "as_of": pts[-1][0]}
            else:
                indexes[key] = {**nm, "status": "inactive", "missing": "inactive"}
            continue
        if not pts:
            indexes[key] = {**nm, "status": "unavailable"}
            continue
        closes = [c for _, c in pts]
        entry: dict = {"close": closes[-1], "as_of": pts[-1][0], "bars_n": len(closes)}
        if len(closes) < ma_fast + 1:
            indexes[key] = {**entry, **nm, "status": "insufficient",
                            "note": f"序列不足 {ma_fast + 1} 根（快均线 {ma_fast} + 区间收益），"
                                    "趋势/位置判断不适用"}
            continue
        # ── 位置：近 quantile_win 根区间百分位 + 距高/低点 ──
        win = closes[-quantile_win:] if quantile_win > 0 else closes
        hi, lo = max(win), min(win)
        pos_q = 50.0 if hi == lo else (closes[-1] - lo) / (hi - lo) * 100.0
        entry.update({"pos_q": round(pos_q, 1),
                      "dist_high": closes[-1] / hi - 1 if hi else None,
                      "dist_low": closes[-1] / lo - 1 if lo else None,
                      "pos_high_252": hi, "pos_low_252": lo,
                      "position": ("high_zone" if pos_q >= pos_high
                                   else "low_zone" if pos_q <= pos_low else "mid_zone")})
        # ── 趋势：均线结构 + 区间收益 → 三态 ──
        ma_f, ma_s = _it_ma(closes, ma_fast), _it_ma(closes, ma_slow)
        ret_f, ret_s = _it_ret(closes, ma_fast), _it_ret(closes, ma_slow)
        above = closes[-1] > ma_f if ma_f is not None else None
        entry.update({"ret_20d": ret_f, "ret_60d": ret_s,
                      "ma_fast": ma_f, "ma_slow": ma_s,
                      "ma_fast_above": above,
                      "ma_fast_above_slow": (ma_f > ma_s
                                             if (ma_f is not None and ma_s is not None) else None)})
        entry["trend"] = ("up" if (above is True and (ret_f or 0) > 0)
                          else "down" if (above is False and (ret_f or 0) < 0)
                          else "range")
        entry.update(nm)                    # name/market/source（循环头已解析）
        indexes[key] = entry
    if not indexes:
        return {"status": "unavailable", "error": "指数序列为空"}
    return {"as_of": as_of, "indexes": indexes}
