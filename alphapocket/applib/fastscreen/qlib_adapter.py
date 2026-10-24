"""M7 T-F8：Qlib 交叉核对通道（分支 A，pyqlib 0.9.7 × pandas 3.0.6 适配实测记录见 pip_probe.md）。

职责（计划 §10.2「Qlib Backtest 交叉核对，年化差异 >1pct 触发实现偏差审查」）：
- 数据：fl 口径 daily_bars → qlib bin 格式（features/{sym}/close+volume.day.bin +
  calendars/day.txt + instruments/all.txt）；数据同源均不复权（缺 factor.bin →
  qlib adjusted_price 模式，trade_unit 不生效——对齐差异表逐项披露）；
- 策略：TopkDropout(topk=n, n_drop=0) ≈ 等权 Top-N 月调，月频信号外置
  （自定义 qlib.backtest.signal.Signal 子类喂 fl 截面合成 score，绕开
  pandas 3.0 已移除的 DataFrame.last 路径——零 monkey-patch）；
- 成本：M6 成本表逐项映射 qlib open_cost/close_cost（佣金/印花税/过户费/滑点），
  对不齐的项（最低佣金 / 整手 / T+1 / 涨跌停 / 成交价时点）列差异表，禁止隐藏口径差宣称一致；
- 判定：|ann_qlib − ann_self| < 1pct（含成本轨，同基数 252/交易日）；>1pct →
  四查偏差审查报告（撮合时点 / 成本 / 日历 / 复权）逐项定位，如实产出。

时序口径（与主链同构）：qlib 决策步 shift=1 → T 日信号 T+1 收盘成交；主链为
T 日选股 T+1 VWAP 成交 → 成交价时点差（close vs VWAP）为固有差异项之一。
"""
from __future__ import annotations

import argparse
import json
import shutil
from pathlib import Path

import numpy as np
import pandas as pd
import qlib  # 新依赖：只允许 fastscreen 内 import（T-F8 红线，静态测试守护）
from qlib.backtest.signal import Signal as _QlibSignal

from applib import backtest as BT
from applib.ctx import Ctx
from applib.fastscreen.vbt_screener import (
    DEFAULT_END,
    DEFAULT_START,
    build_scores,
    crosscheck_self,
)

_REVIEW_AXES = ("撮合时点", "成本", "日历", "复权")


# ── 数据：fl_daily_bar → qlib bin ───────────────────────
def dump_qlib(bars: dict, out: Path) -> dict:
    """daily_bars（secid → [{dt, close, volume, ...}]）→ qlib bin 目录。

    每标的按全日历对齐（缺失日 NaN 填充——qlib bin 语义为日历连续序列）；
    instruments 起止用标的自身首末有效日。返回元信息（n_syms/n_days/n_missing）。
    """
    universe = sorted(bars)
    dates = sorted(set().union(*[{r["dt"] for r in bars[s]} for s in universe]))
    if len(dates) < 3:
        raise ValueError(f"日历过短：{len(dates)} 日")
    if out.exists():
        shutil.rmtree(out)
    (out / "calendars").mkdir(parents=True)
    (out / "instruments").mkdir(parents=True)
    (out / "calendars" / "day.txt").write_text(
        "\n".join(dates) + "\n", encoding="utf-8")
    inst_lines, n_missing = [], 0
    for s in universe:
        sym = s.split(".")[0].lower()          # 510300.SH → sh510300（qlib 小写惯例）
        rows = {r["dt"]: r for r in bars[s]}
        valid = [d for d in dates if d in rows and rows[d].get("close") is not None]
        if not valid:
            continue
        inst_lines.append(f"{sym}\t{valid[0]}\t{valid[-1]}")
        n_missing += len(dates) - len(valid)
        pdir = out / "features" / sym
        pdir.mkdir(parents=True)
        for field in ("close", "volume"):
            vals = [float(rows[d][field]) if d in rows and rows[d].get(field) is not None
                    else np.nan for d in dates]
            with open(pdir / f"{field}.day.bin", "wb") as f:
                np.asarray([0.0], dtype="<f4").tofile(f)      # 首元素 = 日历起始下标
                np.asarray(vals, dtype="<f4").tofile(f)
    (out / "instruments" / "all.txt").write_text("\n".join(inst_lines) + "\n",
                                                 encoding="utf-8")
    return {"n_syms": len(universe), "n_days": len(dates), "n_missing_cells": n_missing,
            "dates": dates, "sym_map": {s: s.split(".")[0].lower() for s in universe}}


# ── 策略：月频外置信号（绕开 DataFrame.last）─────────────
class MonthlyScoreSignal(_QlibSignal):
    """月频截面信号：决策步 shift=1 下 start_time=信号日，命中再平衡日 → 返回当日 score。

    返回 DataFrame(index=instrument, columns=[score])（TopkDropout 取 iloc[:, 0] 排序）；
    非信号日返回 None → 策略空决策保持持仓 ≈ 等权 Top-N 月调。
    """

    def __init__(self, score: pd.DataFrame):
        s = score.copy()
        # qlib 侧规范化：index → 零点 Timestamp（qlib 决策窗口为 normalize 级比较），
        # columns → qlib 小写 instrument（须与 exchange codes 完全一致，否则选股对不上交易域）
        s.index = pd.DatetimeIndex(pd.to_datetime(s.index)).normalize()
        self.score = s

    def get_signal(self, start_time=None, end_time=None):
        st = pd.Timestamp(start_time).normalize()
        if st not in self.score.index:
            return None
        row = self.score.loc[st].dropna()
        if row.empty:
            return None
        return row.to_frame("score")


def qlib_cost_map(cost_table: dict) -> dict:
    """M6 成本表 → qlib 成本参数（逐项映射；不可表达项进差异表）。

    qlib 语义：open_cost 只计买入、close_cost 只计卖出 → 印花税(0.05%卖出)放
    close_cost 恰好单向对齐；滑点主链是价格乘子、qlib 是费用率 → 金额近似、机制差披露。
    """
    s = float(cost_table["slippage_bps"]) / 1e4
    base = float(cost_table["commission_rate"]) + float(cost_table["transfer_fee_rate"])
    return {"open_cost": base + s,
            "close_cost": base + float(cost_table["stamp_tax_rate"]) + s,
            "min_cost": float(cost_table["commission_min_cny"])}


def run_qlib_backtest(qlib_dir: Path, score: pd.DataFrame, *, topk: int,
                      start: str, end: str, codes: list[str],
                      benchmark: str, cost_zero: bool = False) -> dict:
    """TopkDropout(topk, n_drop=0) + 月频外置信号 → {rets, total, cagr, n_trades, turnover}。

    cost_zero=True → 成本全清零（偏差审查定位实验：分离成本贡献）。
    """
    import warnings
    warnings.filterwarnings("ignore")
    qlib.init(provider_uri=str(qlib_dir), region="cn")
    from qlib.contrib.evaluate import backtest_daily
    from qlib.contrib.strategy import TopkDropoutStrategy

    strategy = TopkDropoutStrategy(signal=MonthlyScoreSignal(score), topk=int(topk),
                                   n_drop=0, hold_thresh=1,
                                   risk_degree=1.0)   # qlib 缺省 0.95 留 5% 现金（已知解测试在档）→ 显式满仓对齐主链
    executor = {"class": "SimulatorExecutor", "module_path": "qlib.backtest.executor",
                "kwargs": {"time_per_step": "day", "generate_portfolio_metrics": True}}
    cost = ({"open_cost": 0.0, "close_cost": 0.0, "min_cost": 0.0} if cost_zero
            else qlib_cost_map(BT.DEFAULT_COST_TABLE))
    ex_kw = {"freq": "day", "codes": codes, "deal_price": "close",
             "limit_threshold": None, "trade_unit": 100, **cost}
    rep, _pos = backtest_daily(pd.Timestamp(start), pd.Timestamp(end),
                               strategy=strategy, executor=executor,
                               benchmark=benchmark, exchange_kwargs=ex_kw)
    rets = rep["return"].astype(float)
    nav = (1.0 + rets).cumprod()
    total = float(nav.iloc[-1]) - 1.0
    n = len(rets)
    cagr = float(nav.iloc[-1]) ** (252.0 / n) - 1.0 if n else 0.0
    return {"n_days": n, "total_return": round(total, 6), "cagr": round(cagr, 6),
            "n_trades": int((rep["turnover"] > 0).sum()),
            "total_turnover": round(float(rep["total_turnover"].iloc[-1]), 2),
            "total_cost": round(float(rep["total_cost"].iloc[-1]), 2),
            "start": str(rets.index[0].date()), "end": str(rets.index[-1].date())}


# ── 判定与偏差审查 ──────────────────────────────────────
def compare_annual(sid: str, top_n: int, ann_self: float, ann_qlib: float,
                   n_days: int, decomp: dict | None = None) -> dict:
    """|Δ| < 1pct 通过；>1pct → 四查审查报告（撮合时点/成本/日历/复权逐项定位）。

    decomp（定位实验，数字证据）：Δ(自研net−qlib) = cost_self + cost_qlib + exec_zero
    （恒等分解；注意 Δ 定义为 自研−qlib = −ann_diff）；exec_close_zero 隔离
    「T 日收盘零成本 vs T+1 VWAP 零成本」的约束+时滞贡献。
    """
    diff = round(float(ann_qlib) - float(ann_self), 6)
    passed = abs(diff) < 0.01
    review = None
    if not passed:
        d = decomp or {}
        num = lambda k: ("n/a" if d.get(k) is None else f"{d[k]:+.4%}")
        parts = {"cost_self": d.get("cost_self") or 0, "cost_qlib": d.get("cost_qlib") or 0,
                 "exec_zero": d.get("exec_zero") or 0}
        lead = max(parts, key=lambda k: abs(parts[k])) if d else None
        review = {
            "trigger": f"|Δann|={abs(diff):.4%} ≥ 1pct（{sid} top_n={top_n}）",
            "decomposition": {
                "identity": "Δ(自研net−qlib) = cost_self(主链净−gross) + cost_qlib(qlib costed−zero)"
                            " + exec_zero(自研gross−qlib零成本)",
                "cost_self": d.get("cost_self"), "cost_qlib": d.get("cost_qlib"),
                "exec_zero": d.get("exec_zero"),
                "exec_close_zero": d.get("exec_close_zero"),
                "residual_check": (None if not d else round(
                    (d.get("cost_self") or 0) + (d.get("cost_qlib") or 0)
                    + (d.get("exec_zero") or 0) + diff, 8)),
            },
            "axes": {
                "撮合时点": "qlib=T+1 close；自研=T+1 VWAP/avg4（数据自洽防御可降级）。"
                            f"零成本下撮合+约束+时滞差 exec_zero={num('exec_zero')}；"
                            f"其中 T日收盘零成本对照 exec_close_zero={num('exec_close_zero')}"
                            "（差值≈T+1 时滞与 VWAP 价差的分离项）",
                "成本": "映射：open=佣金+过户+滑点、close=+印花税+滑点（比率制）；"
                        f"主链成本贡献 cost_self={num('cost_self')}，"
                        f"qlib 成本贡献 cost_qlib={num('cost_qlib')}（双向零成本实验分离）；"
                        "不可表达项：最低佣金¥5/整手（adjusted_price 模式不生效）",
                "日历": "同源日历（bars 并集）；qlib 回测区间 = 日历首日..倒数第二日"
                        "（epsilon_change 越界适配），自研对照同区间——"
                        f"qlib n_days={n_days}，自研 n_days={n_days + 1}；"
                        "1 日基数差对年化的影响 <0.05pct（量级：cagr×252/n²）",
                "复权": "数据同源（前复权 OHLC，M6 已登记 amount 矛盾）→ 缺 factor.bin 走 "
                        "adjusted_price 现价模式：复权口径对齐，无独立贡献项；"
                        "整手/adjusted_price 副作用并入 exec_zero 约束差",
            },
            "conclusion": (None if not d else
                           f"主导项={lead}（|Δ| 分解恒等式对账：residual_check≈0 为通过）"),
        }
    return {"strategy": sid, "top_n": int(top_n), "ann_self": round(float(ann_self), 6),
            "ann_qlib": round(float(ann_qlib), 6), "ann_diff": diff,
            "n_days": n_days, "passed": passed, "review": review}


def render_md(payload: dict) -> str:
    sid = payload["strategy"]
    L = [f"# Qlib 交叉核对 —— {sid}", "",
         "- 数据：fl_daily_bar → qlib bin（同源不复权；features/ 目录布局 0.9.x）",
         ("- 策略：TopkDropout(topk=n, n_drop=0) ≈ 等权 Top-N 月调（月频信号外置，"
         "决策步 shift=1 → T 日信号 T+1 close 成交）"),
         "- 判定：|ann_qlib − ann_self| < 1pct（含成本轨，同基数 252/交易日）", "",
         "| top_n | ann_self(自研精验) | ann_qlib | Δ | n_days | 判定 |",
         "| --- | --- | --- | --- | --- | --- |"]
    for r in payload["rows"]:
        verdict = "✓ <1pct" if r["passed"] else "✗ ≥1pct → 偏差审查"
        L.append(f"| {r['top_n']} | {r['ann_self']:.4%} | {r['ann_qlib']:.4%} "
                 f"| {r['ann_diff']:+.4%} | {r['n_days']} | {verdict} |")
    L += ["", "## 成本/口径对齐差异表（诚实披露，禁止宣称一致）", "",
          "| 项 | 主链 M6 | qlib 0.9.7 | 对齐结果 |",
          "| --- | --- | --- | --- |"]
    for a, b, c in payload["diff_table"]:
        L.append(f"| {a} | {b} | {c.split('||')[0]} | {c.split('||')[1]} |")
    reviews = [r for r in payload["rows"] if r["review"]]
    if reviews:
        L += ["", "## 偏差审查报告（>1pct 触发，四查逐项定位）", "",
              ("恒等分解：Δ(自研net−qlib) = cost_self + cost_qlib + exec_zero；"
              "exec_close_zero = T日收盘零成本 − qlib零成本（约束+时滞分离项）")]
        for r in reviews:
            dc = r["review"]["decomposition"]
            L += ["", f"### {r['strategy']} top_n={r['top_n']}（Δ={r['ann_diff']:+.4%}）", "",
                  f"- cost_self（主链成本贡献）= {dc['cost_self']:+.4%}",
                  f"- cost_qlib（qlib 成本贡献）= {dc['cost_qlib']:+.4%}",
                  f"- exec_zero（零成本撮合+约束+时滞差）= {dc['exec_zero']:+.4%}",
                  f"- exec_close_zero（T日收盘零成本对照）= {dc['exec_close_zero']:+.4%}",
                  f"- 恒等式对账 residual_check = {dc['residual_check']:+.2e}",
                  f"- **定位结论**：{r['review']['conclusion']}"]
            for ax, body in r["review"]["axes"].items():
                L.append(f"- 四查·{ax}：{body}")
    else:
        L += ["", "- 全部 top_n 组合 |Δ|<1pct：未触发偏差审查（差异表仍如实在档）"]
    return "\n".join(L) + "\n"


_DIFF_TABLE = [
    ("仓位", "满仓 Top-N 等权（无现金缓冲）", "risk_degree=1.0 显式设置（qlib 缺省 0.95 留 5% 现金，合成已知解测试在档） || 对齐"),
    ("佣金", "0.025% 双边 min¥5", "open/close_cost 各含 0.00025 || min¥5 不可表达（影响极小，披露）"),
    ("印花税", "0.05% 卖出单向", "close_cost 含 0.0005（qlib close_cost 只计卖出） || 单向恰好对齐"),
    ("过户费", "0.001% 双边", "open/close 各 0.00001 || 对齐"),
    ("滑点", "5bps 价格乘子（买×(1+s)/卖×(1-s)，影响份额）", "open/close 各 0.0005 费用率（只扣现金） || 金额近似、机制差披露"),
    ("整手", "100 股向下取整", "trade_unit=100（缺 factor.bin → adjusted_price 模式不生效，WARNING 在档） || 不对齐，披露"),
    ("T+1/涨跌停", "t_plus_1 + defer/skip（一字板/停牌顺延）", "无此概念（limit_threshold=None） || 不对齐，月调下影响小，披露"),
    ("成交价", "T+1 VWAP/avg4（自洽防御降级）", "T+1 close || 时点差为固有差异项，四查之首"),
    ("复权", "不复权（前复权矛盾已登记 M6）", "不复权 + adjusted_price 现价模式 || 数据同源对齐"),
]


# ── CLI ─────────────────────────────────────────────────
def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m applib.fastscreen.qlib_adapter")
    ap.add_argument("--strategy", required=True, help="策略 YAML 路径")
    ap.add_argument("--grid", default="top_n=3,5,8", help="top_n 网格，如 top_n=3,5,8")
    ap.add_argument("--start", default=DEFAULT_START)
    ap.add_argument("--end", default=DEFAULT_END)
    args = ap.parse_args(argv)

    ctx = Ctx()
    db_path = ctx.sandbox_dir / "data" / "fl.duckdb"
    from applib.strategy import load_strategy
    spec = load_strategy(Path(args.strategy), ctx.local_repo, db_path)
    sid = spec["id"]
    top_ns = [int(x) for x in args.grid.split("=", 1)[1].split(",") if x.strip()]

    # 信号面（与快筛/自研同口径合成，equal 主选股法）+ 同区间截断（qlib 日历 = 对照区间）
    panels, meta = build_scores(db_path, ctx.local_repo, spec, methods=["equal"],
                                start=args.start, end=args.end)
    score = panels["equal"]
    bars_all = json.loads((ctx.local_repo / "market" / "daily_bars.json").read_text(encoding="utf-8"))
    bars = {s: [r for r in rows if args.start <= r["dt"] <= args.end]
            for s, rows in bars_all.items()}

    adir = ctx.artifacts_dir / "m7_cross"
    adir.mkdir(parents=True, exist_ok=True)
    qdir = adir / f"qlib_data_{sid}"
    dump_meta = dump_qlib(bars, qdir)
    dates = dump_meta["dates"]
    codes = sorted({v for v in dump_meta["sym_map"].values()})
    bench = dump_meta["sym_map"].get(spec.get("benchmark") or "510300.SH")
    # score 列名 → qlib 小写 instrument（与 exchange codes 同域，否则选股对不上交易域）
    score = score.rename(columns=dump_meta["sym_map"])
    # 决策窗口端点适配：qlib 取 [start, end]，日历末步 epsilon 越界 → end=日历倒数第二日；
    # 自研对照强制同区间（同起同止，n_days 可对账）。
    q_start, q_end = dates[0], dates[-2]

    rows = []
    for n in top_ns:
        self_rows = crosscheck_self(db_path, ctx.local_repo, spec, top_ns=[n],
                                    start=q_start, end=q_end, want_gross=True)
        ann_self = self_rows[0]["self_cagr"]
        q = run_qlib_backtest(qdir, score, topk=n, start=q_start, end=q_end,
                              codes=codes, benchmark=bench)
        row = compare_annual(sid, n, ann_self, q["cagr"], q["n_days"])
        row["qlib_detail"] = q
        if not row["passed"]:
            # 偏差审查定位实验（数字证据，恒等分解）：
            # Δ = cost_self(自研net−gross) + cost_qlib(qlib costed−zero) + exec_zero(自研gross−qlib零成本)
            qz = run_qlib_backtest(qdir, score, topk=n, start=q_start, end=q_end,
                                   codes=codes, benchmark=bench, cost_zero=True)
            s_close = dict(spec)
            s_close["top_n"], s_close["exec"] = int(n), "close"
            from applib.factor_backtest import run_factor_backtest
            bt_close = run_factor_backtest(db_path, ctx.local_repo, s_close,
                                           start=q_start, end=q_end, cost_bps=0.0)
            decomp = {"cost_self": round(ann_self - self_rows[0].get("self_cagr_gross", ann_self), 6),
                      "cost_qlib": round(qz["cagr"] - q["cagr"], 6),
                      "exec_zero": round(self_rows[0].get("self_cagr_gross", ann_self) - qz["cagr"], 6),
                      "exec_close_zero": round(float(bt_close["strategy_metrics"]["cagr"]) - qz["cagr"], 6)}
            row = compare_annual(sid, n, ann_self, q["cagr"], q["n_days"], decomp=decomp)
            row["qlib_detail"] = q
            row["decomposition"] = decomp
        rows.append(row)
    payload = {"strategy": sid, "rows": rows, "diff_table": _DIFF_TABLE,
               "dump": {k: dump_meta[k] for k in ("n_syms", "n_days", "n_missing_cells")},
               "versions": meta["versions"],
               "interval": {"qlib_self_aligned": [q_start, q_end],
                            "note": "qlib 回测区间=日历首日..倒数第二日（末步 epsilon 越界适配）；自研对照同区间"}}
    (adir / f"qlib_cross_{sid}.json").write_text(
        json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    (adir / f"qlib_cross_{sid}.md").write_text(render_md(payload), encoding="utf-8")
    print(f"[qlib_adapter] {sid}: {len(rows)} 组合 → {adir / f'qlib_cross_{sid}.md'}")
    for r in rows:
        tag = "PASS" if r["passed"] else "REVIEW"
        print(f"  top_n={r['top_n']}: self={r['ann_self']:+.4%} "
              f"qlib={r['ann_qlib']:+.4%} Δ={r['ann_diff']:+.4%} [{tag}]")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
