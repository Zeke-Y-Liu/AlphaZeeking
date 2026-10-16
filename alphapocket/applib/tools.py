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
from .engines.base import apply_metric_change, factor_entries


def _load(p):
    return json.loads(p.read_text(encoding="utf-8"))


def _engine(eid: str, ctx=None):
    """取引擎包装器实例并加载 workspace 注册表（T-R2：引擎统一入口）。

    注册表缺失/损坏由 load_registry 透明降级（内置默认），绝不阻断评估；
    ctx=None 时保持内置默认（纯函数调用场景）。
    """
    from . import engines as E
    eng = E.get_engine(eid)
    if ctx is not None:
        eng.load(ctx.local_repo)
    return eng


# ── factor ─────────────────────────────────────────────
def factor_list(args, ctx):
    return _load(ctx.local_repo / "factors" / "registry.json")


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
    beta_eng = _engine("beta", ctx)          # 引擎入口（T-R2）：经包装器调用
    out, signals = [], []
    as_of = args.get("as_of") or "auto"
    for secid, arr in bars.items():
        if not arr:                          # 上游失败标的（0 根 K 线）：跳过，防 IndexError
            continue
        b = beta_eng.run(secid, arr, None if as_of == "auto" else as_of)
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
    alpha_eng = _engine("alpha", ctx)        # 引擎入口（T-R2）：经包装器调用
    out = []
    as_of = args.get("as_of") or "auto"
    for secid, arr in bars.items():
        if not arr:                          # 上游失败标的（0 根 K 线）：跳过，防 IndexError
            continue
        fd = fund.get(secid, {})
        a = alpha_eng.run(secid, arr, fd.get("reports", []), fd.get("dividends", []),
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


def factor_regime(args, ctx):
    """港美利率/汇率 regime 条件变量（H-0006）：carry_hk_us / peg_pressure → 市场级择时状态

    数据源 ``market/hk_rates.json``（``ap fetch`` 时 universe 含港股则同步；HIBOR1M / 联邦基金 /
    USDHKD 逐日序列）。非横截面选股因子：输出市场级 ``state``（outflow_pressure / neutral），
    仅供择时参考。
    """
    p = ctx.local_repo / "market" / "hk_rates.json"
    if not p.exists():
        return {"error": "港美利率数据未就绪：请先 `ap fetch`（universe 含港股时同步 market/hk_rates.json）"}
    data = _load(p)
    series = data.get("series") or []
    if not series:
        return {"error": f"港美利率序列为空：{data.get('error') or '数据源返回 0 行'}"}
    r = _engine("beta", ctx).run_hk_regime(series, as_of=args.get("as_of"),
                                           chg_win=(int(args["chg_win"]) if args.get("chg_win") else None))
    return {"as_of": r["as_of"], "source": data.get("source"), "chg_win": r["chg_win"],
            "state": r["state"], "factors": {fid: r[fid] for fid in F.HK_REGIME_FACTORS},
            "detail": r}


def factor_macro(args, ctx):
    """引擎C：宏观流动性 regime（A/HK/US 三市场温度计与维度分，市场级条件变量）。

    数据源 ``market/macro_liquidity.json``（``ap fetch`` 恒同步）+ ``market/hk_rates.json``
    （HK 维度输入，缺失则 HK 市场降级 unavailable）。非横截面选股因子：输出市场级
    ``state``（tight_stress / neutral / easy_froth）与三市场档位，仅供择时参考；
    **不产生 signals 条目、不参与权重决策**（引擎C 纪律红线）。
    """
    p = ctx.local_repo / "market" / "macro_liquidity.json"
    if not p.exists():
        return {"error": "宏观流动性数据未同步（先执行数据同步）"}
    data = _load(p)
    hk_p = ctx.local_repo / "market" / "hk_rates.json"
    hk_series = (_load(hk_p).get("series") if hk_p.exists() else None)
    if not data.get("series"):
        return {"error": "宏观流动性序列为空（上游拉取失败，重跑同步）"}
    return _engine("macro", ctx).run(data["series"], hk_series,
                                     as_of=args.get("as_of"),
                                     meta=data.get("meta"))


# ── engine.metrics：指标全生命周期（T-R7；写 sandbox 注册表，ap push 经门禁回流）──
def _registry_read(ctx) -> dict:
    """加载 sandbox 注册表（缺失/损坏 → 内置默认透明降级；写回时建立完整文件）。"""
    from .engines.base import load_registry
    return load_registry(ctx.local_repo)


def _registry_write(ctx, reg: dict):
    p = ctx.local_repo / "factors" / "registry.json"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(reg, ensure_ascii=False, indent=2), encoding="utf-8")


def _locate_metric(reg: dict, mid: str, engine: str | None):
    """跨引擎定位指标 id → (engine_id, None) | (None, 中文错误)。"""
    hits = [eid for eid, sec in reg["engines"].items()
            if (not engine or eid == engine)
            and any(isinstance(m, dict) and m.get("id") == mid for m in sec.get("metrics", []))]
    if not hits:
        return None, f"指标不存在：{mid}"
    if len(hits) > 1:
        return None, f"指标 {mid} 在多个引擎中存在（{hits}），请指定 engine"
    return hits[0], None


def engine_metrics_list(args, ctx):
    """指标清单（按引擎/状态过滤；含 changelog 计数）。"""
    reg = _registry_read(ctx)
    out = []
    for eid, sec in reg["engines"].items():
        if args.get("engine") and eid != args["engine"]:
            continue
        for m in sec.get("metrics", []):
            if args.get("status") and m.get("status", "active") != args["status"]:
                continue
            out.append({**{k: m.get(k) for k in
                           ("id", "formula", "dim", "market", "dataset", "scorer",
                            "params", "status", "version", "hypothesis_id", "note")},
                        "engine": eid})
    return {"metrics": out, "changelog_n": len(reg.get("changelog") or [])}


def engine_metrics_add(args, ctx):
    """新增指标（spec=完整声明，formula 须内置清单；hypothesis_id 必填）。"""
    eid, spec = args.get("engine"), args.get("spec")
    if not eid:
        return {"error": "缺少 engine（alpha/beta/macro）"}
    if not isinstance(spec, dict) or not spec.get("id"):
        return {"error": "缺少 spec.id（新指标完整声明，formula 必填）"}
    try:
        reg = _registry_read(ctx)
        new_reg, rec = apply_metric_change(reg, eid, {
            "op": "add", "id": spec["id"],
            "patch": {k: v for k, v in spec.items() if k != "id"},
            "hypothesis_id": args.get("hypothesis_id"), "reason": args.get("reason") or ""})
    except ValueError as e:
        return {"error": str(e)}
    _registry_write(ctx, new_reg)
    return {"ok": True, "metric": rec["after"],
            "warnings": ["已写入 sandbox 注册表；`ap push` 提交变更（需六项验证全绿）"]}


def engine_metrics_update(args, ctx):
    """修改指标（patch=部分字段；params/scorer 深合并；status 翻转需 hypothesis_id）。"""
    mid, patch = args.get("id"), args.get("patch")
    if not mid or not isinstance(patch, dict) or not patch:
        return {"error": "需要 id 与非空 patch（部分字段）"}
    try:
        reg = _registry_read(ctx)
        eid, err = _locate_metric(reg, mid, args.get("engine"))
        if err:
            return {"error": err}
        new_reg, rec = apply_metric_change(reg, eid, {
            "op": "update", "id": mid, "patch": patch,
            "hypothesis_id": args.get("hypothesis_id"), "reason": args.get("reason") or ""})
    except ValueError as e:
        return {"error": str(e)}
    _registry_write(ctx, new_reg)
    return {"ok": True, "before": rec["before"], "after": rec["after"]}


def engine_metrics_remove(args, ctx):
    """移除指标（软删：status=inactive，条目保留；hypothesis_id 必填）。"""
    mid = args.get("id")
    if not mid:
        return {"error": "缺少 id"}
    try:
        reg = _registry_read(ctx)
        eid, err = _locate_metric(reg, mid, args.get("engine"))
        if err:
            return {"error": err}
        new_reg, rec = apply_metric_change(reg, eid, {
            "op": "remove", "id": mid,
            "hypothesis_id": args.get("hypothesis_id"), "reason": args.get("reason") or ""})
    except ValueError as e:
        return {"error": str(e)}
    _registry_write(ctx, new_reg)
    return {"ok": True, "metric": rec["after"],
            "warnings": ["软删完成：条目保留（status=inactive），可 update 复活"]}


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


def portfolio_review(args, ctx):
    pid = str(args.get("portfolio", "cn4"))
    pf_path = ctx.local_repo / "portfolio" / f"{pid}.json"
    if not pf_path.exists():
        return {"error": f"组合不存在：{pid}（{pf_path}）"}
    pf = _load(pf_path)
    dims = {d["secid"]: d for d in _load(ctx.local_repo / "dim" / "security.json")}
    bars, fund = _bars_reports(ctx)
    alpha_eng = _engine("alpha", ctx)        # 引擎入口（T-R2）：三引擎均经包装器调用
    beta_eng = _engine("beta", ctx)
    positions, signals = [], []
    day_rets, members = {}, {}
    for p in pf["positions"]:
        secid = p["secid"]
        arr = bars[secid]
        fd = fund.get(secid, {})
        a = alpha_eng.run(secid, arr, fd.get("reports", []), fd.get("dividends", []))
        b = beta_eng.run(secid, arr)
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
    entries = factor_entries(reg)
    # R4：信号延迟衰减 caveat（fast_decay 的 engine B signal → 报告挂警示）
    decay_caveats = [{"signal_id": f["id"], "name": f.get("name"),
                      "decay_rate": (f.get("delay_decay") or {}).get("decay_rate"),
                      "t1_open": (f.get("delay_decay") or {}).get("t1_open"),
                      "n_events": (f.get("delay_decay") or {}).get("n_events")}
                     for f in entries
                     if (f.get("delay_decay") or {}).get("flag") == "fast_decay"]
    # 引擎C（宏观流动性，市场级条件变量；不产生信号、不参与权重决策，失败透明降级）
    macro_liquidity = {"status": "unavailable", "error": "unknown"}
    try:
        import json as _json
        mp = ctx.local_repo / "market" / "macro_liquidity.json"
        hp = ctx.local_repo / "market" / "hk_rates.json"
        data = _json.loads(mp.read_text(encoding="utf-8")) if mp.exists() else {}
        hk_series = (_json.loads(hp.read_text(encoding="utf-8")).get("series")
                     if hp.exists() else None)
        if data.get("series"):
            # 市场权重 = 组合持仓按市场计数（基金/ETF 计入 A；无持仓计数的市场剔除）
            mw = {}
            for p in pf["positions"]:
                mkt = (dims.get(p["secid"], {}).get("market")
                       or ("A" if not p["secid"].endswith((".HK", ".US")) else
                           "HK" if p["secid"].endswith(".HK") else "US"))
                mw[mkt] = mw.get(mkt, 0) + 1
            macro_liquidity = _engine("macro").run(
                data["series"], hk_series, as_of=port[-1][0],
                market_weights=mw or None, meta=data.get("meta"))
        else:
            macro_liquidity = {"status": "unavailable", "error": "宏观流动性序列为空"}
    except Exception as exc:                                   # noqa: BLE001 - 引擎C 失败不阻断评估
        macro_liquidity = {"status": "unavailable", "error": f"{type(exc).__name__}: {exc}"}
    return {
        "id": pf["id"], "name": pf["name"], "as_of": port[-1][0], "base_ccy": pf["base_ccy"],
        "note": pf["note"], "next_trade_date": next_td, "holiday_days": holiday_days,
        "portfolio_metrics": {"day_ret": port[-1][1], "ret_20d": ret(20),
                              "ret_ytd": series[-1][1] / series[ytd_base][1] - 1,
                              "ret_1y": ret(252), "max_dd_1y": mdd, "nav": series[-1][1],
                              "method": "等权·日再平衡·名义组合（真实行情合成；停牌/缺K线按0收益·权重冻结近似；最大回撤=近252交易日窗口）"},
        "macro_liquidity": macro_liquidity,
        "positions": positions, "signals": signals,
        "decay_caveats": decay_caveats,
        "factor_health": {"active": [f["id"] for f in entries if f.get("status") == "active"],
                          "weakening": [f for f in entries if f.get("status") == "weakening"]},
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
    registry = {"factors": factor_entries(
        _load(ctx.local_repo / "factors" / "registry.json"))}
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


def factor_activate(args, ctx):
    """R3/M5 8.1：draft→active 激活门禁（无 hypothesis_id 拒绝，无旁路）"""
    return H.factor_activate(args, ctx)


def factor_fl_activate(args, ctx):
    """M5 8.1：factorlib 中长期因子 draft→active 激活门禁（③④⑤⑥+血缘，无旁路）。

    与 factor.activate（α 引擎 registry）分工：本工具面向 factorlib 因子库
    （fl_* 治理表消费 + factor_registry_state.json 状态机）。
    """
    from factorlib.lifecycle import activate_factor
    return activate_factor(ctx.sandbox_dir / "data" / "fl.duckdb", ctx.local_repo,
                           str(args.get("factor", "")), who="mcp",
                           why=str(args.get("why", "")))


def factor_fl_inspect(args, ctx):
    """M5 8.2：季度巡检编排（三指标三态 + fl_factor_health 落库 + 状态同步）。

    drill / inject_decay 任一给出即演练态：不落库不写状态文件（artifact 标注 drill）。
    """
    from applib.factor_inspect import run_inspection
    inj = args.get("inject_decay")
    factor = args.get("factor")
    return run_inspection(ctx,
                          factors=[str(factor)] if factor else None,
                          as_of=args.get("as_of"),
                          drill=bool(args.get("drill")),
                          inject_decay=inj if isinstance(inj, list)
                          else ([str(inj)] if inj else None))


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


# ── 二线数据源（a-stock-data v3.10.1 摘取，不替换主源）──────
def _portfolio_secids(ctx) -> list[str]:
    """全部组合持仓 secid 去重（PIT 行业查询的默认标的集）。"""
    out: list[str] = []
    for p in sorted((ctx.local_repo / "portfolio").glob("*.json")):
        try:
            out += [x["secid"] for x in _load(p).get("positions", [])]
        except (KeyError, ValueError):
            continue
    return sorted(set(out))


def altdata_sync(args, ctx):
    """二线源同步：申万行业分类变迁史 → dim/industry_sw.json（含 provenance 登记）"""
    from . import altdata as A
    return A.fetch_sw_industry(ctx, allow_insecure_ssl=not args.get("strict_ssl"))


def altdata_industry_asof(args, ctx):
    """PIT 行业查询：只取 as_of 之前最后一次行业调整（无前视）。

    同时给出主源 ``dim/security.json`` 的静态标注，便于对照「用今天的行业套历史」的偏差。
    ``industries`` 字段即 ``factors.industry_neutralize`` 可直接消费的映射。
    """
    from . import altdata as A
    records = A.load_sw(ctx)
    if not records:
        return {"error": "申万行业未同步：请先运行 `ap altdata-sync`"}
    as_of = args.get("as_of") or dt.date.today().isoformat()
    level = args.get("level") or "l1"
    raw = args.get("codes")
    codes = ([c.strip() for c in str(raw).split(",") if c.strip()] if raw
             else _portfolio_secids(ctx))
    sp = ctx.local_repo / "dim" / "security.json"
    static = {s["secid"]: s.get("industry") for s in _load(sp)} if sp.exists() else {}
    rows, mapping = [], {}
    for c in codes:
        r = A.sw_industry_as_of(records, c, as_of, level)
        mapping[c] = r["industry"] if r else None
        rows.append({"secid": c, "industry_sw": r["industry"] if r else None,
                     "since": r["since"] if r else None,
                     "industry_static": static.get(c)})
    return {"as_of": as_of, "level": level, "source": A.PIN_SOURCE, "n": len(rows),
            "n_no_record": sum(1 for r in rows if r["industry_sw"] is None),
            "note": "industry_sw=申万代码（官方仅发布代码，无中文名）；industry_static=主源静态标注，"
                    "两者分类体系不同，不可直接等同",
            "industries": mapping, "rows": rows}


def research_source(args, ctx):
    """R3 研报来源（只读拉取）：东财研报（评级/目标价/三年EPS预测）+ 同花顺一致预期 EPS"""
    from . import altdata as A
    code = args.get("code")
    if not code:
        return {"error": "需提供 code（如 601058 / 601058.SH）"}
    strict = bool(args.get("strict_ssl"))
    reports, eps, errs = [], [], []
    try:
        reports = A.research_reports(code, max_pages=int(args.get("max_pages") or 1),
                                     allow_insecure_ssl=not strict)
    except Exception as e:                                   # 单源失败不阻断另一源
        errs.append(f"东财研报: {type(e).__name__}: {e}")
    try:
        eps = A.eps_consensus(code, allow_insecure_ssl=not strict)
    except Exception as e:
        errs.append(f"同花顺一致预期: {type(e).__name__}: {e}")
    return {"code": A.six(code), "source": A.PIN_SOURCE, "n_reports": len(reports),
            "reports": reports, "eps_consensus": eps, "errors": errs}


# ── 工具注册表（MCP 暴露面）─────────────────────────────
def _tool(fn, desc):
    return {"fn": fn, "desc": desc}


TOOLS = {
    "data.sync":       _tool(D.data_sync, "同步远端数据集（AKShare 真实数据）到本地 DuckDB"),
    "data.query":      _tool(D.data_query, "只读 SQL 点查（强制 LIMIT）"),
    "factor.list":     _tool(factor_list, "列出因子注册中心（id/类型/状态/版本）"),
    "factor.compute":  _tool(factor_compute, "引擎B：20日信号因子（mom/vol/MA20突破/新高/距高点）"),
    "factor.compute_alpha": _tool(factor_compute_alpha, "引擎A：中长期因子（估值/质量/成长/动量/红利低波+五维）"),
    "factor.regime":   _tool(factor_regime, "H-0006：港美利率/汇率 regime 条件变量（carry_hk_us/peg_pressure，市场级择时）"),
    "factor.macro":    _tool(factor_macro, "引擎C：宏观流动性 regime（A/HK/US 三市场温度计与维度分，市场级条件变量）"),
    "engine.metrics.list":   _tool(engine_metrics_list, "指标注册表清单（按引擎/状态过滤，含变更日志计数）"),
    "engine.metrics.add":    _tool(engine_metrics_add, "指标新增（formula 须 builtin 内置清单；hypothesis_id 必填；写 sandbox 注册表）"),
    "engine.metrics.update": _tool(engine_metrics_update, "指标修改（部分字段/参数深合并/启停；status 翻转需 hypothesis_id；version 自增）"),
    "engine.metrics.remove": _tool(engine_metrics_remove, "指标移除（软删：status=inactive 条目保留；需 hypothesis_id；可 update 复活）"),
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
    "altdata.sync": _tool(altdata_sync, "二线源：同步申万行业分类变迁史（消除行业前视偏差）→ dim/industry_sw.json"),
    "altdata.industry_asof": _tool(altdata_industry_asof, "二线源：PIT 行业查询（取 as_of 前最后一次调整，无前视），含中性化映射"),
    "research.source": _tool(research_source, "R3 研报来源：东财研报（评级/目标价/EPS预测）+ 同花顺一致预期"),
    "hypothesis.register": _tool(hypothesis_register, "R3：登记研报→假设（来源/可检验命题/预期方向）"),
    "hypothesis.list": _tool(hypothesis_list, "R3：假设列表；--factor 走反向血缘（点回因子来源）"),
    "hypothesis.link": _tool(hypothesis_link, "R3：建立因子↔假设血缘（双向写入，冲突拒绝）"),
    "hypothesis.set_status": _tool(hypothesis_set_status, "R3：假设状态机迁移（proposed→testing→supported/refuted→retired）"),
    "hypothesis.review": _tool(hypothesis_review, "R3：巡检假设复查（weakening/invalid 必答 + healthy 20% 抽查）→ hp_review_*"),
    "factor.activate": _tool(factor_activate, "R3/M5 8.1：draft→active 激活门禁（无 hypothesis_id 拒绝，无旁路）"),
    "factor.fl_activate": _tool(factor_fl_activate, "M5 8.1：factorlib 中长期因子激活门禁（③④⑤⑥+血缘，fl_* 治理表消费，无旁路）"),
    "factor.fl_inspect": _tool(factor_fl_inspect, "M5 8.2：季度巡检（三指标三态 + fl_factor_health 落库；drill 演练不落盘）→ fl_inspect_*"),
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
