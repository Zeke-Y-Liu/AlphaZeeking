"""单因子检验流水线（实施计划 §6 M3：§6.4 步骤 ①–③ 工具化）。

检验协议（中长期口径，区别于默认日频 Alphalens）：
- 截面频率：月度（每月最后一个交易日，库内口径）；
- 前瞻收益：未来 1/3/6 个月（月末对月末；末端窗口不完整 → 该月 IC 弃用不外推）；
- 核心指标：Spearman Rank IC 时序 → IC 均值 / IC_IR（月度口径） / Newey-West 调整 t 值
  （自动 lag = floor(4*(T/100)^(2/9)) 经验法则）；
- 分层检验：quantile 分层（协议 10 层；截面宽不足自适应退化并显式标注），
  多头组超额（vs 基准，默认沪深300ETF 510300.SH 净值代理）+ 多空价差 +
  单调性检验（各月 Spearman(层序, 层超额) 的均值，层宽逐月自适应）；
- 辅助指标：覆盖度（截面有值标的 / 库内全集）、月度换手（多头组成员变动率）、
  分层收益衰减（层收益 × horizon 矩阵）；
- 公告周标注：截面日前 7 日内该标的有财报公告 → announce_near，IC 另输出剔除对照。

依赖纪律：纯标准库 + duckdb。沙箱无 pandas/numpy/scipy/statsmodels/alphalens
（实测 3.14.7 pyenv）→ 自研兜底为唯一路径且不依赖库版本（任务 6.1 的设计意图）；
Alphalens 封装位留待依赖可用时接入（CP-0005 登记）。

诚实口径（CP-0005 登记）：
- 样本 = 库内标的全集（repo 9 标的、4 股票），分层退化为 N=min(10, 截面宽) 层；
- 基准为 510300.SH ETF 净值代理（含跟踪误差）；库内无基准 → 超额退化为绝对收益并标注；
- 检验窗口 = 库内实际区间（非协议 2016-01 起），功效结论以「机制成立 + 待全量历史补强」表述。

入库（任务 6.4）：meta_factor_eval(factor_id × horizon 一行，report_json 含 IC 时序全量)，
供 M4 分段稳健与 M6 合成加权复用。
"""
from __future__ import annotations

import json
import math
import subprocess
from datetime import date, timedelta
from pathlib import Path

import duckdb

EVAL_TABLE = """CREATE TABLE IF NOT EXISTS meta_factor_eval (
    factor_id TEXT, version TEXT, as_of DATE, git_sha TEXT,
    horizon_months INTEGER, ic_mean DOUBLE, ic_ir DOUBLE, ic_tstat_nw DOUBLE,
    n_months INTEGER, layers INTEGER, top_excess_mean DOUBLE,
    long_short_mean DOUBLE, monotonicity DOUBLE, turnover DOUBLE,
    report_json TEXT, created_at TEXT)"""

DEFAULT_BENCHMARK = "510300.SH"      # 沪深300ETF（基准代理，CP-0005 登记）
HORIZONS = (1, 3, 6)                 # 协议：未来 1/3/6 个月
ANNOUNCE_NEAR_DAYS = 7               # 公告周：截面日前 7 日内有财报公告


# ── 统计原语（自研，与 applib/walkforward._spearman 同口径双实现交叉锚定） ──
def avg_rank(xs: list[float]) -> list[float]:
    """平均秩（并列取平均，1-based）。"""
    idx = sorted(range(len(xs)), key=lambda i: xs[i])
    r = [0.0] * len(xs)
    i = 0
    while i < len(idx):
        j = i
        while j + 1 < len(idx) and xs[idx[j + 1]] == xs[idx[i]]:
            j += 1
        avg = (i + j) / 2 + 1.0
        for m in range(i, j + 1):
            r[idx[m]] = avg
        i = j + 1
    return r


def spearman(xs: list[float], ys: list[float]) -> float | None:
    """Spearman 秩相关；样本 < 3 或任一常数列 → None（截面 IC 不计算）。"""
    n = len(xs)
    if n < 3:
        return None
    rx, ry = avg_rank(xs), avg_rank(ys)
    mx, my = sum(rx) / n, sum(ry) / n
    cov = sum((rx[i] - mx) * (ry[i] - my) for i in range(n))
    vx = sum((rx[i] - mx) ** 2 for i in range(n))
    vy = sum((ry[i] - my) ** 2 for i in range(n))
    return cov / math.sqrt(vx * vy) if vx > 0 and vy > 0 else None


def newey_west_t(xs: list[float], lag: int | None = None) -> dict | None:
    """Newey-West HAC 调整 t 值（月度 IC 自相关修正）。

    SE = sqrt((γ0 + 2·Σ_{l≤L} w_l·γ_l)/T)，γ_l = (1/T)Σ e_t·e_{t-l}，w_l = 1 − l/(L+1)；
    lag=None → 自动 L = floor(4·(T/100)^(2/9))。T < 2 → None。
    """
    T = len(xs)
    if T < 2:
        return None
    L = lag if lag is not None else int(4 * (T / 100) ** (2 / 9))
    m = sum(xs) / T
    e = [x - m for x in xs]
    g = [sum(e[t] * e[t - l] for t in range(l, T)) / T for l in range(L + 1)]
    hac = g[0] + 2 * sum((1 - l / (L + 1)) * g[l] for l in range(1, L + 1))
    if hac <= 0:
        return None
    se = math.sqrt(hac / T)
    return {"t": m / se, "se": se, "lag": L, "n": T}


# ── 数据提取（DuckDB；输入 = M2 物化产物 + dwd 表） ──
def month_ends(con: duckdb.DuckDBPyConnection, start: str, end: str) -> list[str]:
    """每月最后一个交易日（库内口径，升序）。"""
    rows = con.execute("""
        SELECT substr(CAST(dt AS VARCHAR), 1, 7) AS ym, max(CAST(dt AS VARCHAR)) AS me
        FROM dwd_daily_bar WHERE dt >= CAST(? AS DATE) AND dt <= CAST(? AS DATE)
        GROUP BY ym ORDER BY me
    """, [start, end]).fetchall()
    return [r[1] for r in rows]


def close_map(con: duckdb.DuckDBPyConnection, start: str, end: str) -> dict:
    """{secid: {dt: close}}（全窗口；量级 = 标的×交易日，repo 规模可全载）。"""
    rows = con.execute("""
        SELECT secid, CAST(dt AS VARCHAR), close FROM dwd_daily_bar
        WHERE dt >= CAST(? AS DATE) AND dt <= CAST(? AS DATE) AND close IS NOT NULL
    """, [start, end]).fetchall()
    out: dict[str, dict[str, float]] = {}
    for s, dt, c in rows:
        out.setdefault(s, {})[dt] = c
    return out


def cross_sections(con: duckdb.DuckDBPyConnection, factor_id: str,
                   start: str, end: str) -> dict[str, dict[str, float]]:
    """月末截面因子值 {月末日: {secid: value}}（消费 M2 物化长表）。"""
    rows = con.execute("""
        SELECT CAST(dt AS VARCHAR), secid, value FROM factor_value_daily
        WHERE factor_id = ? AND dt >= CAST(? AS DATE) AND dt <= CAST(? AS DATE)
    """, [factor_id, start, end]).fetchall()
    out: dict[str, dict[str, float]] = {}
    for dt, s, v in rows:
        out.setdefault(dt, {})[s] = v
    return out


def announce_near(con: duckdb.DuckDBPyConnection, start: str, end: str,
                  days: int = ANNOUNCE_NEAR_DAYS) -> dict[str, set[str]]:
    """公告周标注：{月末日: 公告后 7 日内截面上的 secid 集}（PIT：announce <= dt）。"""
    rows = con.execute("""
        SELECT DISTINCT CAST(b.dt AS VARCHAR), f.secid
        FROM (SELECT DISTINCT dt FROM dwd_daily_bar
              WHERE dt >= CAST(? AS DATE) AND dt <= CAST(? AS DATE)) b
        JOIN dwd_financial_report f
          ON f.announce_date <= b.dt AND f.announce_date >= b.dt - ?::INTEGER
    """, [start, end, days]).fetchall()
    out: dict[str, set[str]] = {}
    for dt, s in rows:
        out.setdefault(dt, set()).add(s)
    return out


# ── 检验协议 ──
def assign_layers(values: dict[str, float], n_layers: int) -> list[list[str]]:
    """截面 quantile 分层：按 (value, secid) 升序连续均分 → 每层非空（并列确定性切分）。

    层 1 = 低值组 … 层 L = 高值组；L = min(n_layers, 截面宽)。
    """
    secids = sorted(values, key=lambda s: (values[s], s))
    L = min(n_layers, len(secids))
    layers: list[list[str]] = [[] for _ in range(L)]
    for i, s in enumerate(secids):
        layers[i * L // len(secids)].append(s)
    return layers


def fwd_returns(closes: dict, t0: str, t1: str, secids: list[str]) -> dict[str, float]:
    """个股前瞻收益 close(t1)/close(t0) − 1（两端必有 bar，缺 → 无该样本）。"""
    out = {}
    for s in secids:
        c0, c1 = closes.get(s, {}).get(t0), closes.get(s, {}).get(t1)
        if c0 and c1:
            out[s] = c1 / c0 - 1
    return out


def bench_return(closes: dict, benchmark: str | None, t0: str, t1: str) -> float | None:
    if not benchmark:
        return None
    b = closes.get(benchmark, {})
    c0, c1 = b.get(t0), b.get(t1)
    return c1 / c0 - 1 if c0 and c1 else None


def turnover(prev: list[str], cur: list[str]) -> float | None:
    """多头组月度换手：1 − |交集|/|前组|（前组空 → None）。"""
    if not prev:
        return None
    return 1 - len(set(prev) & set(cur)) / len(prev)


def evaluate_factor(con: duckdb.DuckDBPyConnection, meta, start: str, end: str,
                    horizons: tuple[int, ...] = HORIZONS,
                    benchmark: str | None = DEFAULT_BENCHMARK,
                    n_layers: int = 10) -> dict:
    """单因子月度检验全流程 → report dict（协议全部指标）。

    仅接受 alpha 因子（type=filter 无打分语义，检验无意义 → 显式报错）。
    """
    if (meta.type or "alpha") != "alpha":
        raise ValueError(f"{meta.id}: type={meta.type} 非打分因子，检验流水线仅接受 alpha")
    mes = month_ends(con, start, end)
    if len(mes) < 3:
        raise ValueError(f"{meta.id}: 检验窗口月份数不足（{len(mes)} < 3）")
    xs = cross_sections(con, meta.id, start, end)
    closes = close_map(con, start, end)
    near = announce_near(con, start, end)
    all_secids = sorted({r[0] for r in con.execute(
        "SELECT DISTINCT secid FROM dwd_daily_bar"
        " WHERE dt >= CAST(? AS DATE) AND dt <= CAST(? AS DATE)", [start, end]).fetchall()})
    has_bench = benchmark in closes
    bench_note = (f"基准 {benchmark}（ETF 净值代理，含跟踪误差）" if has_bench
                  else f"库内无 {benchmark} → 超额退化为绝对收益")

    per_h: dict[int, dict] = {}
    for h in horizons:
        ics, ics_ex, layer_rows = [], [], []       # layer_rows[t] = {dt, n_layers, layer_excess}
        long_mem_prev: list[str] | None = None
        tos, long_exs, ls_exs, top_exs, bot_exs, monos = [], [], [], [], [], []
        for i, t in enumerate(mes):
            j = i + h                                  # 前瞻窗口末端 = 后第 h 个月末
            if j >= len(mes):
                break                                  # 窗口不完整 → 弃用（不外推）
            t1 = mes[j]
            sec_vals = xs.get(t, {})
            if not sec_vals:
                continue
            rets = fwd_returns(closes, t, t1, list(sec_vals))
            if len(rets) < 3:
                continue
            secids = sorted(rets)                      # 因子与收益双全样本
            vx = [sec_vals[s] for s in secids]
            vy = [rets[s] for s in secids]
            ic = spearman(vx, vy)
            if ic is not None:
                ics.append({"dt": t, "ic": ic, "n": len(secids)})
                keep = [k for k, s in enumerate(secids) if s not in near.get(t, ())]
                ic_ex = spearman([vx[k] for k in keep], [vy[k] for k in keep]) if len(keep) >= 3 else None
                ics_ex.append({"dt": t, "ic": ic_ex, "n": len(keep),
                               "n_dropped": len(secids) - len(keep)})
            # 分层（超额口径；截面宽不足 → 层数逐月自适应退化）
            br = bench_return(closes, benchmark if has_bench else None, t, t1)
            layers = assign_layers({s: sec_vals[s] for s in secids}, n_layers)
            lret = [sum(rets[s] - (br or 0) for s in g) / len(g) if g else None for g in layers]
            layer_rows.append({"dt": t, "n_layers": len(layers), "layer_excess": lret})
            top_g, bot_g = layers[-1], layers[0]
            top_ex = sum(rets[s] - (br or 0) for s in top_g) / len(top_g)
            bot_ex = sum(rets[s] - (br or 0) for s in bot_g) / len(bot_g)
            top_exs.append(top_ex)
            bot_exs.append(bot_ex)
            long_g = top_g if (meta.direction or 0) > 0 else bot_g
            long_exs.append(sum(rets[s] - (br or 0) for s in long_g) / len(long_g))
            ls_exs.append(top_ex - bot_ex)
            m_mono = spearman([float(k + 1) for k, v in enumerate(lret) if v is not None],
                              [v for v in lret if v is not None])
            if m_mono is not None:
                monos.append(m_mono)
            if long_mem_prev is not None:
                tv = turnover(long_mem_prev, long_g)
                if tv is not None:
                    tos.append(tv)
            long_mem_prev = long_g

        def _agg(vs):
            """(均值, IC_IR, NW-t)；T<2 或波动为 0 → IR/NW 诚实置 None（不除零）。"""
            T = len(vs)
            if T == 0:
                return None, None, None
            m = sum(vs) / T
            ss = sum((v - m) ** 2 for v in vs)
            std = math.sqrt(ss / (T - 1)) if T > 1 and ss > 0 else None
            return m, (m / std if std else None), newey_west_t(vs)

        ic_mean, ic_ir, nw = _agg([r["ic"] for r in ics]) if ics else (None, None, None)
        lmeans = None
        if layer_rows:
            L = max(len(r["layer_excess"]) for r in layer_rows)
            lmeans = []
            for li in range(L):
                col = [r["layer_excess"][li] for r in layer_rows
                       if li < len(r["layer_excess"]) and r["layer_excess"][li] is not None]
                lmeans.append(sum(col) / len(col) if col else None)
        # 单调性 = 各月 Spearman(层序, 层超额) 的均值（层宽逐月自适应，逐月计算更诚实）
        mono = sum(monos) / len(monos) if monos else None
        ic_ex_agg = None
        if any(r["ic"] is not None for r in ics_ex):
            vals = [r["ic"] for r in ics_ex if r["ic"] is not None]
            ic_ex_agg = {"ic_mean": sum(vals) / len(vals), "n_months": len(vals),
                         "n_dropped_total": sum(r["n_dropped"] for r in ics_ex)}

        n_layers_obs = max((r["n_layers"] for r in layer_rows), default=None)
        per_h[h] = {
            "ic_mean": ic_mean, "ic_ir": ic_ir,
            "ic_t_nw": nw["t"] if nw else None, "nw_lag": nw["lag"] if nw else None,
            "n_months": len(ics),
            "ic_series": ics, "ic_ex_announce": ic_ex_agg,
            "layers": n_layers_obs, "layer_mean_excess": lmeans,
            "layer_rows": layer_rows,
            "top_excess_mean": (sum(top_exs) / len(top_exs)) if top_exs else None,
            "bottom_excess_mean": (sum(bot_exs) / len(bot_exs)) if bot_exs else None,
            "long_short_mean": (sum(ls_exs) / len(ls_exs)) if ls_exs else None,
            "long_excess_mean": (sum(long_exs) / len(long_exs)) if long_exs else None,
            "monotonicity": mono, "turnover": (sum(tos) / len(tos)) if tos else None,
        }

    cov_ratios = [len(xs.get(t, {})) for t in mes]
    n_all = len(all_secids)
    report = {
        "factor_id": meta.id, "name": meta.name, "version": meta.version,
        "family": meta.family, "direction": meta.direction,
        "engine": meta.engine,
        "as_of": end, "git_sha": _git_sha(),
        "window": {"start": mes[0], "end": mes[-1], "n_months_total": len(mes)},
        "universe": {"n_all": n_all, "secids": all_secids,
                     "note": "库内标的全集（repo 样本，非全 A）"},
        "benchmark": {"secid": benchmark if has_bench else None, "note": bench_note},
        "protocol": {"horizons": list(horizons), "n_layers": n_layers,
                     "announce_near_days": ANNOUNCE_NEAR_DAYS,
                     "notes": ["月末截面；前瞻收益 = 月末对月末；末端不完整窗口弃用",
                               "IC = Spearman(因子值, 前瞻收益)；分层超额 vs 基准",
                               "分层层数 = min(10, 截面宽)，截面宽不足显式退化（layer_rows.n_layers 逐月记录）",
                               "单调性 = 各月 Spearman(层序, 层超额) 均值；IC_IR/NW-t 在 IC 恒定或样本不足时诚实置 None"]},
        "horizons": per_h,
        "coverage": {"avg": (sum(cov_ratios) / len(cov_ratios)) / n_all if n_all else None,
                     "min": min(cov_ratios) / n_all if n_all and cov_ratios else None,
                     "series": {t: len(xs.get(t, {})) for t in mes}},
    }
    return report


def _git_sha() -> str:
    try:
        return subprocess.run(["git", "rev-parse", "--short", "HEAD"], cwd=Path(__file__).parent,
                              capture_output=True, text=True, timeout=5).stdout.strip() or "unknown"
    except Exception:
        return "unknown"


# ── 报告（任务 6.2） ──
def render_md(rep: dict) -> str:
    """检验报告 → Markdown（协议全部指标 + 分层图[比例条]）。"""
    d = rep["direction"]
    d_txt = {1: "+1（高值多头）", -1: "-1（低值多头）", 0: "0（过滤）"}.get(d, str(d))
    lines = [f"# 单因子检验报告：{rep['factor_id']}（{rep['name']}）", "",
             f"- 元信息：version={rep['version']} · as_of={rep['as_of']} · "
             f"git={rep['git_sha']} · engine={rep['engine']} · direction={d_txt}",
             f"- 窗口：{rep['window']['start']}..{rep['window']['end']}"
             f"（{rep['window']['n_months_total']} 个月末截面）",
             f"- 样本：{rep['universe']['note']}（n={rep['universe']['n_all']}）；"
             f"{rep['benchmark']['note']}",
             f"- 覆盖度：截面均值 {rep['coverage']['avg'] * 100:.1f}% / 最低 "
             f"{rep['coverage']['min'] * 100:.1f}%", "",
             "## IC（Spearman Rank，月度截面）", "",
             "| horizon | IC 均值 | IC_IR | NW-t | 月数 | 剔除公告周 IC | 公告周剔除样本 |",
             "|---|---|---|---|---|---|---|"]
    for h in sorted(rep["horizons"]):
        r = rep["horizons"][h]
        ex = r["ic_ex_announce"]
        ex_txt = f"{ex['ic_mean']:.3f}（{ex['n_months']} 月）" if ex else "—"
        drop = f"{ex['n_dropped_total']} 样本" if ex else "—"
        ic_m = f"{r['ic_mean']:.3f}" if r["ic_mean"] is not None else "—"
        ic_ir = f"{r['ic_ir']:.2f}" if r["ic_ir"] is not None else "—（IC 恒定/样本不足）"
        nw = f"{r['ic_t_nw']:.2f}（lag={r['nw_lag']}）" if r["ic_t_nw"] is not None else "—"
        lines.append(f"| {h} 月 | {ic_m} | {ic_ir} | {nw} | {r['n_months']} | {ex_txt} | {drop} |")
    for h in sorted(rep["horizons"]):
        r = rep["horizons"][h]
        lines += ["", f"## 分层（{h} 月前瞻，超额 vs 基准，{r['layers']} 层）", ""]
        if not r["layer_mean_excess"]:
            lines.append("（无可分层月份）")
            continue
        mx = max(abs(v) for v in r["layer_mean_excess"] if v is not None) or 1.0
        lines += ["| 层（1=低值…L=高值） | 月均超额 | 图示 |", "|---|---|---|"]
        for i, v in enumerate(r["layer_mean_excess"]):
            bar = "" if v is None else ("█" * max(1, round(abs(v) / mx * 20))
                                        if v >= 0 else "░" * max(1, round(abs(v) / mx * 20)))
            lines.append(f"| L{i + 1} | {v * 100:.2f}% | {bar} |")
        mono = (f"{r['monotonicity']:.3f}" if r["monotonicity"] is not None
                else "—（层数不足）")
        lines += ["",
                  f"- 多头组（direction={d_txt}）月均超额：{(r['long_excess_mean'] or 0) * 100:.2f}%"
                  f"；多空价差（L{r['layers']}−L1）：{(r['long_short_mean'] or 0) * 100:.2f}%",
                  f"- 单调性 Spearman(层序, 层均超额)：{mono}",
                  f"- 多头组月度换手：{(r['turnover'] or 0) * 100:.1f}%" if r["turnover"] is not None
                  else "- 多头组月度换手：—（截面过窄）"]
    lines += ["", "## 分层收益衰减（层均超额 % × horizon）", "",
              "| 层 | " + " | ".join(f"{h} 月" for h in sorted(rep["horizons"])) + " |",
              "|---" * (len(rep["horizons"]) + 1) + "|"]
    for i in range(rep["horizons"][sorted(rep["horizons"])[0]]["layers"] or 0):
        cells = []
        for h in sorted(rep["horizons"]):
            lm = rep["horizons"][h]["layer_mean_excess"]
            cells.append(f"{lm[i] * 100:.2f}" if lm and lm[i] is not None else "—")
        lines.append(f"| L{i + 1} | " + " | ".join(cells) + " |")
    lines += ["", "## 口径与局限（诚实原则）", ""]
    for n in rep["protocol"]["notes"]:
        lines.append(f"- {n}")
    lines += ["- 样本为 repo 小样本（4 股票池），截面功效弱：结论以「机制成立 + 待全量"
              "历史补强」表述，不静默放大；",
              "- 分层图以文本比例条呈现（沙箱无 matplotlib）；超额含基准代理跟踪误差。"]
    return "\n".join(lines) + "\n"


# ── 入库（任务 6.4） ──
def ensure_eval_tables(con: duckdb.DuckDBPyConnection) -> None:
    con.execute(EVAL_TABLE)


def save_eval(con: duckdb.DuckDBPyConnection, rep: dict) -> int:
    """报告入库 meta_factor_eval（每 horizon 一行；report_json 含 IC 时序全量）。"""
    ensure_eval_tables(con)
    created = date.today().isoformat()
    rows = []
    for h, r in rep["horizons"].items():
        rows.append((rep["factor_id"], rep["version"], rep["as_of"], rep["git_sha"],
                     int(h), r["ic_mean"], r["ic_ir"], r["ic_t_nw"], r["n_months"],
                     r["layers"], r["top_excess_mean"], r["long_short_mean"],
                     r["monotonicity"], r["turnover"],
                     json.dumps({"ic_series": r["ic_series"], "layer_rows": r["layer_rows"],
                                 "ic_ex_announce": r["ic_ex_announce"]},
                                ensure_ascii=False),
                     created))
    con.executemany("INSERT INTO meta_factor_eval VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", rows)
    return len(rows)


# ── 一条命令出报告（任务 6.3：CLI 编排；数据装配与 tests/conftest repo_con 同构双实现） ──
_EVAL_DDLS = (
    """CREATE TABLE dwd_daily_bar (secid TEXT, dt DATE, open DOUBLE, high DOUBLE,
        low DOUBLE, close DOUBLE, volume BIGINT, amount DOUBLE, pct DOUBLE)""",
    """CREATE TABLE dwd_financial_report (secid TEXT, period DATE, announce_date DATE,
        eps_ytd DOUBLE, bps DOUBLE, roe DOUBLE, roe_w DOUBLE, rev_g DOUBLE,
        np_g DOUBLE, margin DOUBLE, revenue DOUBLE, net_profit DOUBLE,
        total_assets DOUBLE, total_liab DOUBLE, ocf DOUBLE, capex DOUBLE,
        cash DOUBLE, interest_debt DOUBLE)""",
    "CREATE TABLE dwd_dividend (secid TEXT, ex_date DATE, announce DATE, dps DOUBLE)",
    "CREATE TABLE dwd_mktcap_daily (secid TEXT, dt DATE, mktcap DOUBLE)",
    """CREATE TABLE dim_security (secid TEXT, name TEXT, market TEXT,
        asset_type TEXT, industry TEXT)""",
)


def build_universe_db(repo_dir: str | Path, path: str = ":memory:") -> duckdb.DuckDBPyConnection:
    """alphapocket repo JSON → 检验用库（dwd 五表；契约独立声明，双实现交叉锚定）。

    path 默认内存库；传文件路径时状态跨进程持久（M5 生命周期门禁凭据所在），
    dwd 派生表每次全量重灌（repo JSON 为权威快照，幂等），meta_* 状态表保留。
    """
    repo = Path(repo_dir)
    con = duckdb.connect(path)
    existing = {r[0] for r in con.execute(
        "SELECT table_name FROM duckdb_tables()").fetchall()}
    for ddl in _EVAL_DDLS:
        tname = ddl.split("CREATE TABLE", 1)[1].split("(", 1)[0].strip()
        if tname in existing:
            con.execute(f"DROP TABLE {tname}")
    for ddl in _EVAL_DDLS:
        con.execute(ddl)
    import json as _json
    bars = _json.loads((repo / "market/daily_bars.json").read_text())
    fund = _json.loads((repo / "factors/fundamental_values.json").read_text())
    con.executemany("INSERT INTO dwd_daily_bar VALUES (?,?,?,?,?,?,?,?,?)",
                    [(s, r["dt"], r["open"], r["high"], r["low"], r["close"],
                      r["volume"], r["amount"], r["pct"]) for s, a in bars.items() for r in a])
    con.executemany("INSERT INTO dwd_financial_report VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    [(s, r["period"], r["announce_date"], r["eps_ytd"], r["bps"], r["roe"],
                      r["roe_w"], r["rev_g"], r["np_g"], r["margin"], r.get("revenue"),
                      r.get("net_profit"), r.get("total_assets"), r.get("total_liab"),
                      r.get("ocf"), r.get("capex"), r.get("cash"), r.get("interest_debt"))
                     for s, v in fund.items() for r in v["reports"]])
    con.executemany("INSERT INTO dwd_dividend VALUES (?,?,?,?)",
                    [(s, d["ex_date"], d["announce"], d["dps"])
                     for s, v in fund.items() for d in v["dividends"]])
    mc = repo / "dim/market_cap.json"
    if mc.exists():
        mcap = _json.loads(mc.read_text())
        con.executemany("INSERT INTO dwd_mktcap_daily VALUES (?,?,?)",
                        [(s, dt_, float(mv)) for s, a in mcap.items() for dt_, mv in a])
    sc = repo / "dim/security.json"
    if sc.exists():
        secs = _json.loads(sc.read_text())
        con.executemany("INSERT INTO dim_security VALUES (?,?,?,?,?)",
                        [(d["secid"], d["name"], d.get("market"), d.get("asset_type"),
                          d.get("industry")) for d in secs])
    return con


def run_eval(repo_dir: str | Path, factor_ids: list[str], start: str, end: str,
             out_dir: str | Path | None = None,
             horizons: tuple[int, ...] = HORIZONS,
             benchmark: str | None = DEFAULT_BENCHMARK) -> list[dict]:
    """检验一条命令：建库 → 物化 → 检验 → 入库 meta_factor_eval → MD/JSON 落盘。

    out_dir 为 None 时不落盘（仅返回报告）；返回各因子报告 dict 列表。
    """
    from . import load_definitions, materialize
    con = build_universe_db(repo_dir)
    root = Path(__file__).resolve().parent
    metas = {m.id: m for m in load_definitions(root)}
    missing = [f for f in factor_ids if f not in metas]
    if missing:
        raise ValueError(f"因子未注册：{missing}")
    reports = []
    for fid in factor_ids:
        meta = metas[fid]
        materialize(con, meta, mode="full")
        rep = evaluate_factor(con, meta, start, end, horizons=horizons, benchmark=benchmark)
        save_eval(con, rep)
        if out_dir is not None:
            out = Path(out_dir) / fid
            out.mkdir(parents=True, exist_ok=True)
            (out / f"report_{rep['as_of']}.md").write_text(render_md(rep), encoding="utf-8")
            (out / f"report_{rep['as_of']}.json").write_text(
                json.dumps(rep, ensure_ascii=False, indent=1), encoding="utf-8")
        reports.append(rep)
    return reports
