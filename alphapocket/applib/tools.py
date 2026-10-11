"""ap-mcp 工具集：data / factor / portfolio / report / sync（V4-α 真实数据版）

纪律：ap 自身不调用任何大模型 API —— 推理由宿主 IDE agent（Trae/opencode/Codex）完成。
数据：AKShare 公开接口（新浪行情/财务/分红 + 腾讯交叉源 + 交易日历），经 datasource.fetch 落库。
"""
from __future__ import annotations
import json, datetime as dt
import duckdb
from .ctx import Ctx, new_run_id
from . import data as D
from . import repo as R
from . import factors as F
from . import hypothesis as H
from . import datasets as _DS          # T-R9：engine.metrics.list 数据集声明派生


def _now_ts():
    return dt.datetime.now().strftime("%Y-%m-%dT%H:%M:%S")


def _load(p):
    return json.loads(p.read_text(encoding="utf-8"))


# ── factor ─────────────────────────────────────────────
def factor_list(args, ctx):
    return _load(ctx.local_repo / "factors" / "registry.json")


def _data_missing(args, ctx):
    """T-R10 data.missing：逐数据集缺失/完整性/新鲜度检测（提示不阻断，透明降级）。"""
    return _DS.check_data_missing(ctx, stale_days=int(args.get("stale_days") or 7))


def _bars_reports(ctx):
    """从本地 repo 取真实 bars/报告/分红（review 独立可复现：review = f(repo)）"""
    bars = _load(ctx.local_repo / "market" / "daily_bars.json")
    fund = _load(ctx.local_repo / "factors" / "fundamental_values.json")
    return bars, fund


def factor_compute(args, ctx):
    """引擎 B（20日信号）：真实行情 → mom/vol/MA20突破/20日新高/距高点 + 信号"""
    con = duckdb.connect(str(ctx.db_path))
    con.execute("DELETE FROM factor_value_daily WHERE factor_id IN (%s)" %
                ",".join(f"'{f}'" for f in F.ENGINE_B_FACTORS))
    bars, _ = _bars_reports(ctx)
    out, signals = [], []
    as_of = args.get("as_of") or "auto"
    for secid, arr in bars.items():
        if not arr:                          # 上游失败标的（0 根 K 线）：跳过，防 IndexError
            continue
        b = F.compute_beta(secid, arr, None if as_of == "auto" else as_of)
        vals = {"mom_20d": b["mom_20d"], "vol_20d": b["vol_20d"], "ma20_break": b["ma20_break"],
                "high20_touch": b["high20_touch"], "dist_high20": b["dist_high20"]}
        for fid, v in vals.items():
            if v is not None:
                con.execute("INSERT INTO factor_value_daily VALUES (?,?,?,?)", (b["as_of"], secid, fid, v))
        out.append({"secid": secid, "dt": b["as_of"], **{k: round(v, 4) if isinstance(v, float) else v
                                                          for k, v in vals.items()}, "state": b["state"]})
        signals.extend(dict(s, secid=secid) for s in b["signals"])
    con.close()
    # T0.3：不再写入模块级全局态；signals/as_of 直接随返回值传递给调用方（MCP 契约不变）
    as_of_b = out[0]["dt"] if out else None
    return {"engine": "B", "as_of": as_of_b, "computed": out, "signals": signals}


def factor_compute_alpha(args, ctx):
    """引擎 A（中长期因子）：PIT 财务阶梯 + 行情 → 估值/质量/成长/动量/红利低波 + 五维评分"""
    con = duckdb.connect(str(ctx.db_path))
    con.execute("DELETE FROM factor_value_daily WHERE factor_id IN (%s)" %
                ",".join(f"'{f}'" for f in F.ENGINE_A_FACTORS))
    con.execute("DELETE FROM factor_pit_meta")
    bars, fund = _bars_reports(ctx)
    out = []
    as_of = args.get("as_of") or "auto"
    for secid, arr in bars.items():
        if not arr:                          # 上游失败标的（0 根 K 线）：跳过，防 IndexError
            continue
        fd = fund.get(secid, {})
        a = F.compute_alpha(secid, arr, fd.get("reports", []), fd.get("dividends", []),
                            None if as_of == "auto" else as_of)
        for fid in F.ENGINE_A_FACTORS:
            if a.get(fid) is not None:
                con.execute("INSERT INTO factor_value_daily VALUES (?,?,?,?)", (a["as_of"], secid, fid, a[fid]))
        if a["used_period"] is not None:   # 基金/指数类无财报 → 无 PIT 元数据
            con.execute("INSERT INTO factor_pit_meta VALUES (?,?,?,?,?)",
                        (a["as_of"], secid, "report_set", a["used_period"], a["used_announce"]))
        out.append({"secid": secid, "dt": a["as_of"], "close": a["close"],
                    "pe_ttm": a["val_pe_ttm"], "pb": a["val_pb"], "pe_pct": a["val_pe_pct"],
                    "div_yield": a["div_yield_ttm"], "roe_ttm": a["qua_roe_ttm"],
                    "mom_12_1": a["mom_12_1"], "composite": a["composite"],
                    "grades": a["grades"], "used_period": a["used_period"]})
    con.close()
    return {"engine": "A", "as_of": out[0]["dt"] if out else None, "computed": out,
            "pit_rule": "报告期+披露窗口（Q1+30/H1+62/Q3+31/FY+120 日）= 可用人时点"}


# ── portfolio ───────────────────────────────────────────
def _next_trade_date(ctx, as_of):
    """下一交易日 + 休市日历天数（自 as_of 至下一交易日之间的自然日）"""
    cal = _load(ctx.local_repo / "market" / "trade_calendar.json")
    today = dt.date.today().isoformat()
    for d in cal:
        if d >= today:
            gap_days = (dt.date.fromisoformat(d) - dt.date.fromisoformat(as_of)).days - 1
            return d, max(gap_days, 0)
    return None, 0


def _macro_block(ctx, as_of: str) -> dict:
    """引擎C regime 块（T-C4 集成层）：as_of 与评估同日（组合 K 线最后交易日）。

    透明降级（红线2）：宏观数据缺失/损坏区别于行情数据——不进门禁、评估不失败，
    status=unavailable 透传；不决策（红线1）：块内无任何信号字段、不参与权重。
    """
    f = ctx.local_repo / "market" / "macro_liquidity.json"
    if not f.exists():
        return {"status": "unavailable",
                "reason": "macro_liquidity.json 不存在（未采集，不阻断评估）"}
    try:
        ind = _load(f).get("indicators") or {}
        if not ind:
            return {"status": "unavailable", "reason": "indicators 为空（数据层未采集）"}
        m = F.compute_macro_liquidity(ind, as_of)
        m["status"] = "ok" if m["global_score"] is not None else "unavailable"
        return m
    except Exception as e:                         # noqa: BLE001 - 宏观块解析失败不阻断评估
        return {"status": "unavailable",
                "reason": f"宏观 payload 解析失败透明降级（{type(e).__name__}）"}


def portfolio_review(args, ctx):
    pid = str(args.get("portfolio", "cn4"))
    pf_path = ctx.local_repo / "portfolio" / f"{pid}.json"
    if not pf_path.exists():
        return {"error": f"组合不存在：{pid}（{pf_path}）"}
    pf = _load(pf_path)
    dims = {d["secid"]: d for d in _load(ctx.local_repo / "dim" / "security.json")}
    bars, fund = _bars_reports(ctx)
    positions, signals = [], []
    day_rets, members = {}, {}
    for p in pf["positions"]:
        secid = p["secid"]
        arr = bars[secid]
        fd = fund.get(secid, {})
        a = F.compute_alpha(secid, arr, fd.get("reports", []), fd.get("dividends", []))
        b = F.compute_beta(secid, arr)
        day_pct = arr[-1]["pct"]
        # 基金/指数类无财报（used_announce=None）→ 无陈旧度概念
        staleness = ((dt.date.fromisoformat(a["as_of"]) - dt.date.fromisoformat(a["used_announce"])).days
                     if a["used_announce"] else None)
        flags = ([f"财报期 {a['used_period']}（可用人 {a['used_announce']}）"] if a["used_period"]
                 else [f"{p.get('asset_type', '基金')}（无财报期，估值/质量/成长不适用）"])
        if staleness is not None and staleness > 150:
            flags.append("数据陈旧")
        positions.append({
            "secid": secid, "name": p["name"], "industry": dims[secid]["industry"], "weight": p["weight"],
            "asset_type": p.get("asset_type", "STOCK"),
            "settle": p.get("settle", "A股 T+1"), "cost": p.get("cost", "印花税卖出0.05%+佣金"),
            "close": b["close"], "day_pct": day_pct,
            "alpha": {k: a[k] for k in ("val_pe_ttm", "val_pb", "val_pe_pct", "val_pb_pct", "div_yield_ttm",
                                        "qua_roe_ttm", "qua_margin", "grow_rev_yoy", "grow_np_yoy",
                                        "mom_12_1", "vol_250", "used_period", "pe_hist_n")},
            "beta": {k: b[k] for k in ("mom_20d", "vol_20d", "ma20", "ma20_break", "state",
                                       "high20_touch", "dist_high20", "high20_intra", "low20",
                                       "sigma_daily", "band_low", "band_high")},
            "dims": a["dims"], "grades": a["grades"],
            "composite": a["composite"], "composite_grade": a["composite_grade"],
            "flags": flags})
        signals.extend(dict(s, secid=secid, name=p["name"]) for s in b["signals"])
        members[secid] = arr
        day_rets[secid] = day_pct

    # 等权名义组合（日再平衡）真实收益序列
    # P0-2/P0-3：日期轴取各标的并集（原取首标的全集，遇停牌/晚上市会 StopIteration）；
    # 缺 K 线/首日 pct=None 按收益 0 计（等权日再平衡下的权重冻结近似），分母固定为持仓数
    n_pos = len(members)
    dates = sorted(set().union(*[{r["dt"] for r in arr} for arr in members.values()]))
    port = []
    for d in dates:
        rs = [next((r["pct"] for r in members[s] if r["dt"] == d), None) for s in members]
        rs = [r for r in rs if r is not None]
        if rs:
            port.append((d, sum(rs) / n_pos))
    cum, series = 1.0, []
    for d, r in port:
        cum *= (1 + r)
        series.append((d, cum))
    # 一年最大回撤：仅最近 252 个交易日窗口（原实现误为全历史口径）
    win = series[-252:]
    peak_w, mdd = win[0][1], 0.0
    for _, v in win:
        peak_w = max(peak_w, v)
        mdd = min(mdd, v / peak_w - 1)
    # 区间收益 = 累计净值之比（series 为日再平衡净值曲线）
    def ret(n):
        return series[-1][1] / series[-1 - n][1] - 1 if len(series) > n else None
    ytd_base = next((i for i, (d, _) in enumerate(port) if d >= f"{port[-1][0][:4]}-01-01"), 0)
    next_td, holiday_days = _next_trade_date(ctx, port[-1][0])

    reg = _load(ctx.local_repo / "factors" / "registry.json")
    # R4：信号延迟衰减 caveat（fast_decay 的 engine B signal → 报告挂警示）
    decay_caveats = [{"signal_id": f["id"], "name": f.get("name"),
                      "decay_rate": (f.get("delay_decay") or {}).get("decay_rate"),
                      "t1_open": (f.get("delay_decay") or {}).get("t1_open"),
                      "n_events": (f.get("delay_decay") or {}).get("n_events")}
                     for f in reg["factors"]
                     if (f.get("delay_decay") or {}).get("flag") == "fast_decay"]
    return {
        "id": pf["id"], "name": pf["name"], "as_of": port[-1][0], "base_ccy": pf["base_ccy"],
        "note": pf["note"], "next_trade_date": next_td, "holiday_days": holiday_days,
        "portfolio_metrics": {"day_ret": port[-1][1], "ret_20d": ret(20),
                              "ret_ytd": series[-1][1] / series[ytd_base][1] - 1,
                              "ret_1y": ret(252), "max_dd_1y": mdd, "nav": series[-1][1],
                              "method": "等权·日再平衡·名义组合（真实行情合成；停牌/缺K线按0收益·权重冻结近似；最大回撤=近252交易日窗口）"},
        "positions": positions, "signals": signals,
        "decay_caveats": decay_caveats,
        "macro_liquidity": _macro_block(ctx, port[-1][0]),   # 引擎C：as_of 与评估同日（T-C4）
        "factor_health": {"active": [f["id"] for f in reg["factors"] if f["status"] == "active"],
                          "weakening": [f for f in reg["factors"] if f["status"] == "weakening"]},
        "data_quality": {"last_bar": port[-1][0], "calendar_next": next_td, "sources": "AKShare 新浪+腾讯",
                         "provenance": str(ctx.local_repo / "market" / "provenance.json")},
    }


def portfolio_rebalance_plan(args, ctx):
    """拟调仓/执行单（变更建议）：引擎A五维 + 引擎B信号规则生成，附理由链，需人工确认

    成本口径（C2 修复）：名义资金 ¥1,000,000（与执行单 ``exec_orders.build_exec_orders``
    默认一致），单笔费用按执行单同费率（佣金万2.5/单边、最低 ¥5/笔；A 股股票卖出另计
    印花税 0.05%）逐笔求和。
    """
    from . import exec_orders as EO
    NOMINAL_CAPITAL_CNY = 1_000_000   # 与 build_exec_orders 默认资金一致（单笔费用求和口径）
    pid = str(args.get("portfolio", "cn4"))
    rv = portfolio_review({"portfolio": pid}, ctx)
    asset_by_secid = {p.get("secid"): p.get("asset_type", "ETF") for p in rv["positions"]}
    trades = []
    for p in rv["positions"]:
        b, a = p["beta"], p["alpha"]
        if b["state"] == "below":
            trades.append({"side": "evaluate_sell", "secid": p["secid"], "name": p["name"],
                           "from": p["weight"], "to": round(p["weight"] * 0.6, 1),
                           "reason": f"引擎B sig_ma20_break 卖出预警：收盘低于MA20 {b['ma20_break']:+.1%}（±2%带外）"
                                     f" · 20日动量 {b['mom_20d']:+.1%} · 250日波动 {a['vol_250']:.0%}",
                           "evidence": "E2", "settle": p.get("settle", "A股 T+1"),
                           "cost": p.get("cost", "印花税卖出0.05%+佣金")})
        elif b["state"] == "above" and b["mom_20d"] > 0 and (a.get("mom_12_1") or 0) > 0:
            trades.append({"side": "hold_confirm", "secid": p["secid"], "name": p["name"],
                           "from": p["weight"], "to": p["weight"],
                           "reason": f"双引擎同向：引擎A动量 {a['mom_12_1']:+.1%}（12-1月） + 引擎B站上MA20 {b['ma20_break']:+.1%}"
                                     f" · 20日新高 {'是' if b['high20_touch'] else '否'}",
                           "evidence": "E2", "settle": "—", "cost": "—"})
    turnover = sum(abs(t["to"] - t["from"]) for t in trades if t["side"] == "evaluate_sell") / 2
    has_stock = any(p.get("asset_type") == "STOCK" for p in rv["positions"])
    cost_est = sum(
        EO._fee(abs(t["to"] - t["from"]) / 100 * NOMINAL_CAPITAL_CNY,
                asset_by_secid.get(t["secid"], "ETF"), "卖出")
        for t in trades if t["side"] == "evaluate_sell"
    )
    return {"plan_id": f"RP-{rv['as_of'].replace('-', '')}-{pid.upper()}", "portfolio": pid,
            "generated_by": "engine_a+engine_b（规则版）", "as_of": rv["as_of"],
            "diff_trades": trades, "turnover_est": f"{turnover:.1f}%（单边）",
            "cost_est_cny": round(cost_est),
            "cost_note": ("股票：双边佣金万2.5×2+印花税卖出0.05%" if has_stock
                          else "ETF/LOF：双边佣金万2.5×2（免印花税）"),
            "status": "draft → 待用户确认（引擎永不直接动仓位）"}


def portfolio_exec_orders(args, ctx):
    """调仓执行单：rebalance_plan → 委托指令（目标权重→金额→整手股数），待人工确认、不自动下单"""
    from . import exec_orders as EO
    pid = str(args.get("portfolio", "multi5"))
    capital = float(args.get("capital", 1_000_000))
    run_id = args.get("run_id")
    art, review, plan = None, None, None
    if run_id:
        art = ctx.artifacts_dir / run_id
    else:   # 取该组合最近一次含 rebalance_plan 的 run
        for p in reversed(sorted(ctx.artifacts_dir.glob("*/rebalance_plan.json"))):
            if _load(p).get("portfolio") == pid:
                art = p.parent
                break
    if art is not None and (art / "rebalance_plan.json").exists() and (art / "review.json").exists():
        plan, review, run_id = _load(art / "rebalance_plan.json"), _load(art / "review.json"), art.name
    else:   # 无历史 run → 按当前数据现算
        review = portfolio_review({"portfolio": pid}, ctx)
        if "error" in review:
            return review
        plan = portfolio_rebalance_plan({"portfolio": pid}, ctx)
        run_id = new_run_id("exec")
        art = ctx.artifacts_dir / run_id
    sheet = EO.build_exec_orders(review, plan, capital)
    paths = EO.write_all(sheet, art)
    return {"exec_id": sheet["exec_id"], "run_id": run_id, "portfolio": pid,
            "capital_cny": capital, "status": sheet["status"],
            "summary": sheet["summary"], "cash": sheet["cash"], "fees": sheet["fees"],
            "orders": [o for o in sheet["orders"] if o["order_shares"] != 0],
            "artifacts": paths}


# ── backtest ────────────────────────────────────────────
def backtest_run(args, ctx):
    """组合回测：等权·月度再平衡（含换手成本）vs 沪深300ETF 买入持有 → HTML+MD+JSON"""
    from . import backtest as BT
    from . import report as RP
    pid = str(args.get("portfolio", "multi5"))
    pf_path = ctx.local_repo / "portfolio" / f"{pid}.json"
    if not pf_path.exists():
        return {"error": f"组合不存在：{pid}（{pf_path}）"}
    pf = _load(pf_path)
    bars = _load(ctx.local_repo / "market" / "daily_bars.json")
    bt = BT.run_backtest(bars, pf, start=args.get("start"), end=args.get("end"),
                         rf=float(args.get("rf", 0.0)),
                         weighting=str(args.get("weighting") or "equal"),
                         er_window=int(args.get("er_window") or 250),
                         max_weight=(float(args["max_weight"]) if args.get("max_weight") else None))
    run_id = new_run_id("bt")
    paths = BT.write_all(bt, ctx.artifacts_dir / run_id, RP.CSS)
    return {"run_id": run_id, "portfolio": pid, "start": bt["start"], "end": bt["end"],
            "n_days": bt["n_days"], "n_rebalances": bt["n_rebalances"],
            "strategy_metrics": bt["strategy_metrics"],
            "benchmark": ({"secid": bt["benchmark"]["secid"], "name": bt["benchmark"]["name"],
                           "metrics": bt["benchmark"]["metrics"]} if bt["benchmark"] else None),
            "excess": bt["excess"], "cost": bt["cost"], "artifacts": paths}


# ── research（R1 参数敏感性）────────────────────────────
def research_sensitivity(args, ctx):
    """R1：策略级参数敏感性扫描（OAT + 双参数热力图 + 尖峰 flag）→ artifacts/sens_*"""
    from . import sensitivity as SEN
    pid = str(args.get("portfolio", "cn4"))
    pids = ["cn4", "multi5"] if pid == "all" else [pid]
    bars = _load(ctx.local_repo / "market" / "daily_bars.json")
    fund = _load(ctx.local_repo / "factors" / "fundamental_values.json")
    results = []
    for p in pids:
        pf_path = ctx.local_repo / "portfolio" / f"{p}.json"
        if not pf_path.exists():
            return {"error": f"组合不存在：{p}（{pf_path}）"}
        rep = SEN.run_scan(_load(pf_path), bars, fund,
                           start=args.get("start"), end=args.get("end"))
        run_id = new_run_id("sens")
        paths = SEN.write_all(rep, ctx.artifacts_dir / run_id)
        results.append({"run_id": run_id, "portfolio": p, "start": rep["start"], "end": rep["end"],
                        "baseline": rep["baseline_cell"], "anchor_check": rep["anchor_check"],
                        "beta_state_consistency": rep["beta_state_consistency"],
                        "flagged": rep["flagged"],
                        "oat_verdicts": {k: v["verdict"] for k, v in rep["oat"].items()},
                        "artifacts": paths})
    return {"scanned": len(results), "results": results}


# ── research（R2 walk-forward 样本外协议）───────────────
def research_walkforward(args, ctx):
    """R2：walk-forward 样本外验证（扩展窗口滚动 + lockbox 封存）→ artifacts/wf_*"""
    from . import walkforward as WF
    factor = str(args.get("factor", "all"))
    bars = _load(ctx.local_repo / "market" / "daily_bars.json")
    fund = _load(ctx.local_repo / "factors" / "fundamental_values.json")
    registry = _load(ctx.local_repo / "factors" / "registry.json")
    known = {f["id"] for f in registry["factors"]}
    if factor != "all" and factor not in known:
        return {"error": f"因子未注册：{factor}"}
    cfg = {k: int(args[k]) for k in ("train_min_months", "valid_months", "step_months", "horizon_days")
           if args.get(k)}
    ids = [f["id"] for f in registry["factors"]] if factor == "all" else [factor]
    results = []
    for fid in ids:
        rep = WF.run_validation(fid, bars, fund, registry, cfg or None,
                                start=args.get("start"), end=args.get("end"))
        if "error" in rep:
            results.append({"factor": fid, "error": rep["error"]})
            continue
        run_id = new_run_id(f"wf_{fid}")
        paths = WF.write_all(rep, ctx.artifacts_dir / run_id)
        results.append({"run_id": run_id, "factor": fid, "n_folds": rep["n_folds"],
                        "lockbox": rep["lockbox"], "verdict": rep["verdict"],
                        "summary": rep["summary"], "artifacts": paths})
    return {"validated": len(results), "results": results}


def research_audit_split(args, ctx):
    """R2：audit_split 四查（字段完备 / 取数越界 / lockbox 一次性 / 折叠无重叠）"""
    from . import walkforward as WF
    return WF.audit_split(ctx)


def research_delay_decay(args, ctx):
    """R4：时间延迟敏感性实证（0/1/2 日执行衰减表 + 降级联动）→ artifacts/decay_*"""
    from . import delay_decay as DD
    bars = _load(ctx.local_repo / "market" / "daily_bars.json")
    fp = ctx.local_repo / "factors" / "registry.json"
    reg = _load(fp)
    rep = DD.run_decay(bars, args.get("signals", "all"),
                       horizon=int(args.get("horizon") or DD.HORIZON),
                       start=args.get("start"), end=args.get("end"))
    changes = DD.apply_registry(reg, rep)
    rep["registry_changes"] = changes
    if any(c.get("applied") for c in changes):
        fp.write_text(json.dumps(reg, ensure_ascii=False, indent=2), encoding="utf-8")
    run_id = new_run_id("decay")
    paths = DD.write_all(rep, ctx.artifacts_dir / run_id)
    return {"run_id": run_id, "n_events": rep["n_events"], "n_signals": rep["n_signals"],
            "signals": [{k: s[k] for k in ("signal_id", "type", "n_events", "flag")}
                        for s in rep["signals"]],
            "registry_changes": changes, "artifacts": paths}


# ── research（P0 风险模型最小集）────────────────────────
def research_risk_model(args, ctx):
    """P0：风险模型最小集（0.1 波动目标 / 0.2 等风险 / 0.3 敞口 / 0.4 限额）→ artifacts/risk_*"""
    from . import risk as RISK
    pid = str(args.get("portfolio", "cn4"))
    pf_path = ctx.local_repo / "portfolio" / f"{pid}.json"
    if not pf_path.exists():
        return {"error": f"组合不存在：{pid}（{pf_path}）"}
    pf = _load(pf_path)
    bars = _load(ctx.local_repo / "market" / "daily_bars.json")
    dims = {d["secid"]: d for d in _load(ctx.local_repo / "dim" / "security.json")}
    mc_path = ctx.local_repo / "dim" / "market_cap.json"
    market_cap = _load(mc_path) if mc_path.exists() else {}
    rep = RISK.run_risk_model(bars, pf, dims, market_cap,
                              start=args.get("start"), end=args.get("end"))
    run_id = new_run_id("risk")
    paths = RISK.write_all(rep, ctx.artifacts_dir / run_id)
    return {"run_id": run_id, "portfolio": pid, "start": rep["start"], "end": rep["end"],
            "n_days": rep["n_days"], "dod": rep["dod"], "dod_all": rep["dod_all"],
            "p0_1": rep["p0_1_vol_target"], "p0_2": rep["p0_2_equal_risk"],
            "p0_3": rep["p0_3_exposure"], "p0_4": rep["p0_4_limits"],
            "artifacts": paths}


# ── research（P1 因子补齐验收）──────────────────────────
def research_p1_eval(args, ctx):
    """P1：因子补齐验收（增量相关 |ρ|<0.6 + A/B 条件门对照）→ artifacts/p1_*"""
    from . import factor_eval as PE
    pid = str(args.get("portfolio", "all"))
    pids = ["cn4", "multi5"] if pid == "all" else [pid]
    bars = _load(ctx.local_repo / "market" / "daily_bars.json")
    fund = _load(ctx.local_repo / "factors" / "fundamental_values.json")
    registry = _load(ctx.local_repo / "factors" / "registry.json")

    def loader(p):
        fp = ctx.local_repo / "portfolio" / f"{p}.json"
        return _load(fp) if fp.exists() else None

    damp = args.get("gate_damp")
    rep = PE.run_p1_eval(pids, bars, fund, registry, loader,
                         start=args.get("start"), end=args.get("end"),
                         gate_damp=(0.5 if damp is None else float(damp)),
                         gate_redistribute=bool(args.get("gate_redistribute")))
    run_id = new_run_id("p1")
    paths = PE.write_all(rep, ctx.artifacts_dir / run_id)
    return {"run_id": run_id, "dod": rep["dod"], "incremental": rep["incremental"],
            "gate_config": rep.get("gate_config"),
            "ab": [{k: a[k] for k in ("portfolio", "gain_total_return", "dd_improvement",
                                      "gate_damp", "gate_redistribute", "threshold", "dod", "gate")
                    if k in a} for a in rep["ab"]], "artifacts": paths}


# ── hypothesis（R3 研报→假设登记通道）───────────────────
def hypothesis_register(args, ctx):
    """R3：登记研报→假设（来源/可检验命题/预期方向）→ repo/hypothesis/registry.json"""
    return H.hypothesis_register(args, ctx)


def hypothesis_list(args, ctx):
    """R3：假设列表；--factor 走反向血缘（一键点回因子来源）"""
    return H.hypothesis_list(args, ctx)


def hypothesis_link(args, ctx):
    """R3：建立因子↔假设血缘（双向写入，冲突拒绝）"""
    return H.hypothesis_link(args, ctx)


def hypothesis_set_status(args, ctx):
    """R3：假设状态机迁移（proposed→testing→supported/refuted→retired）"""
    return H.hypothesis_set_status(args, ctx)


def hypothesis_review(args, ctx):
    """R3：巡检假设复查（weakening/invalid 必答 + healthy 20% 抽查）→ hp_review_*"""
    return H.hypothesis_review(args, ctx)


# ── factor（引擎C 宏观流动性 T-C3）──────────────────────


def factor_macro(args, ctx):
    """引擎C：宏观流动性三市场温度计（只读 regime 查询）。

    只读红线：不写任何状态（仅读 sandbox/repo）；不决策：输出为市场条件变量，
    不产生买卖信号、不参与权重、不携带任何信号字段（grep 红线）。
    缺数据 → status=unavailable 透明降级（不抛异常、不虚构档位）。
    """
    f = ctx.local_repo / "market" / "macro_liquidity.json"
    if not f.exists():
        return {"status": "unavailable",
                "reason": "macro_liquidity.json 不存在（先 ap fetch 采集宏观指标并 ap clone）"}
    payload = _load(f)
    indicators = payload.get("indicators") or {}
    # as_of 链（不取未来）：显式参数 → payload 落盘 as_of → 旧文件兜底=各指标最新可用 dt
    as_of = args.get("as_of") or payload.get("as_of")
    as_of_src = "args" if args.get("as_of") else "payload（T-C1 落盘 as_of）"
    if not as_of and indicators:
        as_of = max((v.get("last_dt") for v in indicators.values() if v.get("last_dt")),
                    default=None)
        as_of_src = "兜底：indicators 最新 dt（旧文件无顶层 as_of）"
    if not indicators or not as_of:
        return {"status": "unavailable",
                "reason": "indicators 为空或缺 as_of（数据层未采集或拉取全失败降级）"}
    out = F.compute_macro_liquidity(indicators, as_of)
    if out["global_score"] is None:
        out["status"] = "unavailable"
        out["reason"] = "全市场无可用指标（scorer 全路径 insufficient/no_data 降级）"
    else:
        out["status"] = "ok"
    out["notes"] = F.macro_scorer_notes(indicators, out, as_of, as_of_src)   # 口径注记单一来源
    return out


def factor_activate(args, ctx):
    """R3/M5 8.1：draft→active 激活门禁（无 hypothesis_id 拒绝，无旁路）"""
    return H.factor_activate(args, ctx)


# ── report ──────────────────────────────────────────────
def report_generate(args, ctx, run: dict | None = None):
    from . import report as RP
    pid = str(args.get("portfolio", "cn4"))
    review = portfolio_review({"portfolio": pid}, ctx)
    plan = portfolio_rebalance_plan({"portfolio": pid}, ctx)
    run_id = (run or {}).get("run_id") or new_run_id()
    verify = (run or {}).get("verify") or {}
    art = ctx.artifacts_dir / run_id
    art.mkdir(parents=True, exist_ok=True)
    paths = RP.render_all(review, plan, ctx, run_id, art, verify=verify)
    return {"run_id": run_id, "artifacts": paths, "verify_embedded": bool(verify)}


def report_read(args, ctx):
    run_id = args.get("run_id")
    if run_id:
        p = ctx.artifacts_dir / run_id / "report.md"
    else:
        runs = sorted(ctx.artifacts_dir.glob("*/report.md"))
        if not runs:
            return {"error": "无报告"}
        p = runs[-1]
    return {"path": str(p), "content": p.read_text(encoding="utf-8")[:4000]}


# ── checkpoint ──────────────────────────────────────────
def checkpoint_save(ctx: Ctx, run_id: str, manifest: dict):
    cps = json.loads(ctx.checkpoints_file.read_text()) if ctx.checkpoints_file.exists() else {"next": 1, "items": []}
    cps["items"].append({"cp_id": f"CP-{cps['next']:04d}", "run_id": run_id, **manifest})
    cps["next"] += 1
    ctx.checkpoints_file.write_text(json.dumps(cps, ensure_ascii=False, indent=2), encoding="utf-8")
    return cps["items"][-1]["cp_id"]


def checkpoint_list(args, ctx):
    if not ctx.checkpoints_file.exists():
        return {"items": []}
    return {"items": json.loads(ctx.checkpoints_file.read_text())["items"]}


def checkpoint_restore(args, ctx):
    """α：恢复=输出重放清单（生产：按四元组重建环境重跑）"""
    cid = args.get("cp")
    for it in checkpoint_list({}, ctx)["items"]:
        if it["cp_id"] == cid:
            task = it.get("task", "review_cn4_once")
            return {"restored": True, "manifest": it,
                    "replay": f"ap fetch && ap clone && ap run tasks/{task}.yaml --as-of {it.get('as_of')}"}
    return {"restored": False, "reason": "checkpoint 不存在"}


# ── 工具注册表（MCP 暴露面）─────────────────────────────
def _tool(fn, desc):
    return {"fn": fn, "desc": desc}


# ── engine.metrics 四工具（T-R9）：引擎指标元数据 CRUD ────────────────────
# 红线：工具 = 写保护之上的薄层（add 幂等不覆盖 / remove orphan 不物理删）；
# 操作留痕不可关闭（registry.operations 逐次登记）。
def _registry_path(ctx):
    return ctx.local_repo / "factors" / "registry.json"


def _log_operation(reg: dict, op: str, mid: str, fields=None, actor: str = "mcp") -> dict:
    rec = {"op": op, "id": mid, "ts": _now_ts(),
           "actor": actor, "fields": sorted(fields) if fields else None}
    reg.setdefault("operations", []).append(rec)
    return rec


def engine_metrics_list(args, ctx):
    """list：因子/信号/宏观指标元数据全景（空态不炸；宏观指标从 DatasetSpec 声明派生）。"""
    reg = _load(_registry_path(ctx))
    out = {"factors": reg.get("factors", []),
           "n_factors": len(reg.get("factors", []))}
    try:
        specs = _DS.registry_specs()               # DatasetSpec 单点声明（T-R5）
        out["datasets"] = {k: {"path": v.path, "merge_policy": v.merge_policy}
                           for k, v in specs.items()}
        out["n_datasets"] = len(specs)
    except Exception:                            # noqa: BLE001 - spec 异常不影响因子列表
        out["datasets"] = {}
    return out


def _require_type(t):
    if t not in ("alpha", "signal", "macro"):
        raise ValueError(f"type 非法：{t!r}（须 alpha / signal / macro）")


def engine_metrics_add(args, ctx):
    """add：登记新指标。幂等（同 id 已存在 → 不覆盖既有条目=用户改动安全）。"""
    mid = str(args.get("id") or "").strip()
    if not mid:
        raise ValueError("add 须提供 id")
    _require_type(str(args.get("type") or ""))
    p = _registry_path(ctx)
    reg = _load(p)
    old = next((f for f in reg.get("factors", []) if f.get("id") == mid), None)
    if old is not None:
        return {"added": False, "id": mid,
                "reason": "id 已存在（幂等：不覆盖既有条目，用户改动安全）",
                "existing": {k: old.get(k) for k in ("status", "version", "name")}}
    entry = {k: v for k, v in args.items()
             if k not in ("actor",) and v is not None}
    entry["id"] = mid
    entry.setdefault("status", "draft")
    entry.setdefault("version", "v0.1")
    reg.setdefault("factors", []).append(entry)
    log = _log_operation(reg, "add", mid, fields=entry.keys(), actor=str(args.get("actor") or "mcp"))
    p.write_text(json.dumps(reg, ensure_ascii=False, indent=2), encoding="utf-8")
    return {"added": True, "id": mid, "status": entry["status"], "operation": log}


def engine_metrics_update(args, ctx):
    """update：字段级更新（id 不可改；仅显式列出的字段变动；留痕）。"""
    mid = str(args.get("id") or "").strip()
    fields = args.get("fields") or {}
    if not mid or not isinstance(fields, dict) or not fields:
        raise ValueError("update 须提供 id 与 fields{字段:值}")
    if "id" in fields:
        raise ValueError("id 不可修改（remove 重建）")
    p = _registry_path(ctx)
    reg = _load(p)
    old = next((f for f in reg.get("factors", []) if f.get("id") == mid), None)
    if old is None:
        return {"updated": False, "id": mid, "reason": "id 不存在（先 add）"}
    changed = {k: old.get(k) for k in fields if k in old and old[k] != fields[k]}
    for k, v in fields.items():
        old[k] = v
    log = _log_operation(reg, "update", mid, fields=fields.keys(),
                         actor=str(args.get("actor") or "mcp"))
    log["before"] = changed                       # 留痕：改了什么（旧值快照）
    p.write_text(json.dumps(reg, ensure_ascii=False, indent=2), encoding="utf-8")
    return {"updated": True, "id": mid, "changed": changed, "operation": log}


def engine_metrics_remove(args, ctx):
    """remove：orphan 标记不物理删（T-R4 三语义 ③；M5 生命周期接管后续处置）。"""
    mid = str(args.get("id") or "").strip()
    if not mid:
        raise ValueError("remove 须提供 id")
    p = _registry_path(ctx)
    reg = _load(p)
    old = next((f for f in reg.get("factors", []) if f.get("id") == mid), None)
    if old is None:
        return {"removed": False, "id": mid, "reason": "id 不存在"}
    if old.get("status") == "orphan":
        return {"removed": False, "id": mid, "reason": "已是 orphan（幂等）"}
    before = old.get("status")
    old["status"] = "orphan"
    old["removed_at"] = dt.date.today().isoformat()
    log = _log_operation(reg, "remove", mid, fields=["status", "removed_at"],
                         actor=str(args.get("actor") or "mcp"))
    log["before"] = {"status": before}
    p.write_text(json.dumps(reg, ensure_ascii=False, indent=2), encoding="utf-8")
    return {"removed": True, "id": mid, "status": "orphan",
            "physical": "条目保留（不物理删；M5 T-L3 生命周期接管）", "operation": log}


TOOLS = {
    "data.sync":       _tool(D.data_sync, "同步远端数据集（AKShare 真实数据）到本地 DuckDB"),
    "data.query":      _tool(D.data_query, "只读 SQL 点查（强制 LIMIT）"),
    "data.missing":    _tool(_data_missing, "T-R10 数据缺失检测（提示不阻断；按 DatasetSpec.validation 声明驱动）"),
    "factor.list":     _tool(factor_list, "列出因子注册中心（id/类型/状态/版本）"),
    "factor.compute":  _tool(factor_compute, "引擎B：20日信号因子（mom/vol/MA20突破/新高/距高点）"),
    "factor.compute_alpha": _tool(factor_compute_alpha, "引擎A：中长期因子（估值/质量/成长/动量/红利低波+五维）"),
    "portfolio.review": _tool(portfolio_review, "cn4 组合复盘：中长期因子+短期20日+下一交易日指引"),
    "portfolio.rebalance_plan": _tool(portfolio_rebalance_plan, "生成拟调仓/评估单（变更建议，需人工确认）"),
    "portfolio.exec_orders": _tool(portfolio_exec_orders, "调仓执行单：目标权重→金额→整手股数委托清单（待人工确认，不自动下单）"),
    "report.generate": _tool(report_generate, "生成完整报告（HTML+MD，含三建议）"),
    "backtest.run":    _tool(backtest_run, "组合回测（等权/等风险加权·月度再平衡 vs 沪深300ETF）→ HTML+MD+JSON"),
    "research.sensitivity": _tool(research_sensitivity, "R1：策略级参数敏感性扫描（OAT+热力图+尖峰flag）→ HTML+MD+JSON"),
    "research.walkforward": _tool(research_walkforward, "R2：walk-forward 样本外验证（扩展窗口滚动 + lockbox 封存）→ wf_*/validation.json+md"),
    "research.audit_split": _tool(research_audit_split, "R2：audit_split 四查（字段完备/取数越界/lockbox 一次性/折叠无重叠）"),
    "research.delay_decay": _tool(research_delay_decay, "R4：时间延迟敏感性实证（引擎B 0/1/2日执行衰减表 + 降级联动）→ decay_*"),
    "research.risk_model": _tool(research_risk_model, "P0：风险模型最小集（波动目标/等风险/敞口/限额）→ risk_*"),
    "research.p1_eval": _tool(research_p1_eval, "P1：因子补齐验收（增量相关 |ρ|<0.6 + A/B 条件门对照）→ p1_*"),
    "hypothesis.register": _tool(hypothesis_register, "R3：登记研报→假设（来源/可检验命题/预期方向）"),
    "hypothesis.list": _tool(hypothesis_list, "R3：假设列表；--factor 走反向血缘（点回因子来源）"),
    "hypothesis.link": _tool(hypothesis_link, "R3：建立因子↔假设血缘（双向写入，冲突拒绝）"),
    "hypothesis.set_status": _tool(hypothesis_set_status, "R3：假设状态机迁移（proposed→testing→supported/refuted→retired）"),
    "hypothesis.review": _tool(hypothesis_review, "R3：巡检假设复查（weakening/invalid 必答 + healthy 20% 抽查）→ hp_review_*"),
    "factor.activate": _tool(factor_activate, "R3/M5 8.1：draft→active 激活门禁（无 hypothesis_id 拒绝，无旁路）"),
    "engine.metrics.list": _tool(engine_metrics_list, "引擎指标元数据全景（因子/信号 + 数据集声明派生）"),
    "engine.metrics.add": _tool(engine_metrics_add, "登记新指标（幂等：同 id 不覆盖既有条目）"),
    "engine.metrics.update": _tool(engine_metrics_update, "字段级更新（id 不可改；留痕 before 快照）"),
    "engine.metrics.remove": _tool(engine_metrics_remove, "orphan 标记不物理删（M5 生命周期接管）"),
    "factor.macro": _tool(factor_macro, "引擎C：宏观流动性三市场温度计（只读 regime 查询，不决策不参与权重）"),
    "report.read":     _tool(report_read, "读取最近/指定报告 Markdown（供 agent 解读）"),
    "repo.clone":      _tool(R.repo_clone, "远端 main → 本地 sandbox（AKShare 真实数据）"),
    "repo.status":     _tool(R.repo_status, "本地工作区状态（dirty/changed）"),
    "repo.push":       _tool(R.repo_push, "提交变更 → 远端 CR（§15.2 门禁：须全绿 checkpoint，否则拒绝）"),
    "checkpoint.list": _tool(checkpoint_list, "检查点列表"),
    "checkpoint.restore": _tool(checkpoint_restore, "恢复检查点（α: 重放清单）"),
}


def call_tool(name: str, args: dict, ctx: Ctx, run: dict | None = None):
    if name not in TOOLS:
        return {"error": f"未知工具 {name}"}
    t = TOOLS[name]
    if name == "repo.push":
        return t["fn"](args, ctx, run_manifest=run)
    if name == "report.generate":
        return t["fn"](args, ctx, run=run)
    return t["fn"](args, ctx)
