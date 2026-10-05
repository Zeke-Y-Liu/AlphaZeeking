"""真实数据源接入（AKShare 公开接口）→ remote/repo（云端替身）

V4-α 真实化：以 AKShare 免费公开接口为唯一数据源，替换 seed 模拟数据。
- 行情：新浪 stock_zh_a_daily（qfq 前复权 + raw 不复权，双份留存供复权校验）
- 交叉源：腾讯 stock_zh_a_hist_tx（独立行情源，供跨源验证）
- 财务：新浪 stock_financial_analysis_indicator（2019 起逐报告期）
- 分红：新浪 stock_history_dividend_detail（除权除息日 + 每股派息）
- 交易日历：新浪 tool_trade_date_hist_sina
- 基金（ETF/LOF，multi5 组合）：新浪 fund_etf_hist_sina（对 ETF 与 LOF 均适用；无复权接口，
  序列即不复权 raw）；交叉源为腾讯 stock_zh_a_hist_tx（独立源，对 ETF/LOF 同样适用；
  东财 push2his 接口在当前网络出口不可达，弃用）

纪律：仅调用公开函数（无下划线内部函数）；全部数据附溯源 provenance（接口/参数/
行数/sha256）；财务报告期按监管披露截止日保守推算可用人时点（PIT）。
"""
from __future__ import annotations
import json, time, hashlib, datetime as dt
from pathlib import Path

# ── 组合定义（用户指定四标的；未提供权重 → 等权 25%）──────────────
CN4 = [
    {"code": "601058", "secid": "601058.SH", "sina": "sh601058", "name": "赛轮集团", "industry": "汽车零部件·轮胎"},
    {"code": "002014", "secid": "002014.SZ", "sina": "sz002014", "name": "永新股份", "industry": "塑料软包装"},
    {"code": "600660", "secid": "600660.SH", "sina": "sh600660", "name": "福耀玻璃", "industry": "汽车零部件·汽车玻璃"},
    {"code": "002027", "secid": "002027.SZ", "sina": "sz002027", "name": "分众传媒", "industry": "广告传媒"},
]

# ── 组合定义二：multi5 多资产五标的（ETF/LOF；用户 2026-10-04 指定，等权 20%）──
# 注：A股无纳斯达克综合指数 ETF，以纳指100ETF(513100) 近似（行业标注 QDII-纳指100）
MULTI5 = [
    {"code": "510300", "secid": "510300.SH", "sina": "sh510300", "name": "沪深300ETF",
     "industry": "A股宽基指数", "ftype": "etf", "settle": "T+1", "cost": "ETF 免印花税·佣金约万2.5"},
    {"code": "501018", "secid": "501018.SH", "sina": "sh501018", "name": "南方原油LOF",
     "industry": "原油(QDII)", "ftype": "lof", "settle": "T+0(跨境LOF)", "cost": "LOF 免印花税·佣金约万2.5"},
    {"code": "518880", "secid": "518880.SH", "sina": "sh518880", "name": "黄金ETF",
     "industry": "黄金", "ftype": "etf", "settle": "T+0", "cost": "ETF 免印花税·佣金约万2.5"},
    {"code": "513100", "secid": "513100.SH", "sina": "sh513100", "name": "纳指ETF",
     "industry": "美股指数(QDII-纳指100)", "ftype": "etf", "settle": "T+0(跨境)", "cost": "ETF 免印花税·佣金约万2.5"},
    {"code": "511260", "secid": "511260.SH", "sina": "sh511260", "name": "十年国债ETF",
     "industry": "国债", "ftype": "etf", "settle": "T+0", "cost": "ETF 免印花税·佣金约万2.5"},
]

# 财务报告期 → 可用人时点（PIT）：按监管/交易所披露截止日保守推后
PIT_LAG_DAYS = {3: 30, 6: 62, 9: 31, 12: 120}

FIN_COLS = {
    "eps_ytd":  "摊薄每股收益(元)",
    "bps":      "每股净资产_调整前(元)",   # 调整后列对部分标的（如002027）上游数据异常，统一取调整前
    "roe":      "净资产收益率(%)",
    "roe_w":    "加权净资产收益率(%)",
    "rev_g":    "主营业务收入增长率(%)",
    "np_g":     "净利润增长率(%)",
    "margin":   "主营业务利润率(%)",
}


def _iso(x) -> str | None:
    if x is None:
        return None
    s = str(x)[:10]
    return s if len(s) == 10 and s[4] == "-" else None


def _num(x) -> float | None:
    try:
        f = float(x)
        return f if f == f else None      # NaN → None
    except (TypeError, ValueError):
        return None


def _sha256_file(p: Path) -> str:
    return hashlib.sha256(p.read_bytes()).hexdigest()


def _retry(fn, n=3, wait=2.0):
    last = None
    for i in range(n):
        try:
            return fn()
        except Exception as e:            # 上游偶发限流/超时：退避重试
            last = e
            time.sleep(wait * (i + 1))
    raise RuntimeError(f"akshare 重试 {n} 次仍失败：{last}")


def _announce_date(period: str) -> str:
    d = dt.date.fromisoformat(period)
    return (d + dt.timedelta(days=PIT_LAG_DAYS[d.month])).isoformat()


def fetch_all(ctx, start: str = "2021-01-01", fin_start_year: str = "2019",
              universe=None, incremental_from=None) -> dict:
    """拉取行情/财务/分红/日历 → remote/repo（云端替身）。

    T0.3 参数化（默认行为与历史逐字节一致）：
    - universe=None       → 默认全量 CN4 + MULTI5（现行为不变）
    - universe=[secid,…]  → 仅拉取这些 secid（在 CN4+MULTI5 定义中按 secid 匹配；
                            未匹配的 secid 跳过并在返回体 universe_missing 中登记）
    - incremental_from={secid: "YYYY-MM-DD"} → 该 secid 行情自「次日」起增量拉取；
                            未列出的 secid 仍按 start 全量
    """
    import akshare as ak
    today = dt.date.today().isoformat()
    repo = ctx.repo_dir
    if repo.exists():
        import shutil
        shutil.rmtree(repo)               # 真实数据切换：整体重建云端 main（α）
    for sub in ("market", "factors", "portfolio", "dim"):
        (repo / sub).mkdir(parents=True, exist_ok=True)

    # ── universe 选择（universe=None → 全量，保持现行为）──
    all_defs = CN4 + MULTI5
    if universe is None:
        selected = [u["secid"] for u in all_defs]
        universe_missing = []
    else:
        try:
            requested = [str(s) for s in universe]
        except TypeError:
            return {"fetched": False, "error": f"universe 必须是 secid 列表：{universe!r}"}
        known = {u["secid"] for u in all_defs}
        universe_missing = [s for s in requested if s not in known]   # 未匹配：跳过并登记
        selected = [s for s in requested if s in known]
    sel_cn4 = [u for u in CN4 if u["secid"] in selected]
    sel_multi5 = [u for u in MULTI5 if u["secid"] in selected]
    universe_fetched = [u["secid"] for u in sel_cn4] + [u["secid"] for u in sel_multi5]

    def _start_for(secid: str) -> str:
        # 增量：自上次最后交易日的「次日」起拉取；未列出 → 全量 start
        if incremental_from and secid in incremental_from and incremental_from[secid]:
            d = dt.date.fromisoformat(str(incremental_from[secid])[:10])
            return (d + dt.timedelta(days=1)).isoformat()
        return start

    prov = {"fetched_at": dt.datetime.now().isoformat(timespec="seconds"),
            "akshare_version": ak.__version__, "today": today,
            "universe": [u["secid"] for u in CN4 + MULTI5] if universe is None else list(selected),
            "sources": [], "pit_rules": {
                "financial_report_lag_days": PIT_LAG_DAYS,
                "note": "报告期+保守披露窗口=可用人时点（Q1→+30d，H1→+62d，Q3→+31d，FY→+120d）；α 未逐笔核对公告日，取监管截止日兜底"},
            "methods": {
                "pe_ttm": "close/TTM摊薄EPS；TTT EPS=上年FY+本年YTD−上年同期YTD（PIT 阶梯函数，报告期按 announce_date 对齐）",
                "pb": "close/最新可用人报告期每股净资产(调整前；新浪'调整后'列对部分标的异常，弃用)",
                "div_yield_ttm": "近365日除权除息（闭区间[as_of-365d, as_of]）每股派息合计/close",
                "vol": "日收益率样本标准差(ddof=1)年化×√252",
                "mom_12_1": "close[t-21]/close[t-252]−1（剔除最近一月）",
                "mom_20d": "close[-1]/close[-20]−1（20收盘窗口）",
                "ma20_break": "close/MA20−1，±2% 确认带",
                "fund_bars": "ETF/LOF（multi5）：新浪 fund_etf_hist_sina（对 ETF/LOF 均适用）无复权接口，"
                             "序列即不复权 raw；无财报/分红数据 → 估值/质量/成长因子与股息率不适用，动量/波动照常；"
                             "交叉源为腾讯 stock_zh_a_hist_tx（东财接口当前网络出口不可达，弃用）"},
            "note": "永新=永新股份(002014.SZ)；cn4 未提供成本与权重 → 等权名义组合(每标的25%)；"
                    "multi5 五资产等权 20%×5，纳指以纳指100ETF(513100)近似（A股无综指ETF）"}

    def _reg(dataset, interface, upstream, params, rows, relpath):
        p = repo / relpath
        (repo / relpath).parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(rows, ensure_ascii=False), encoding="utf-8") if relpath.endswith(".json") else None
        prov["sources"].append({"dataset": dataset, "interface": interface, "upstream": upstream,
                                "params": params, "file": relpath, "sha256": _sha256_file(p),
                                "bytes": p.stat().st_size})

    # ── ① 交易日历 ──
    cal = _retry(lambda: ak.tool_trade_date_hist_sina())
    dates = [_iso(d) for d in cal["trade_date"].tolist()]
    _reg("dwd_trade_calendar", "ak.tool_trade_date_hist_sina()", "新浪财经", {}, dates, "market/trade_calendar.json")

    # ── ② 四标的：行情(qfq/raw) + 交叉源(腾讯) + 财务 + 分红 ──
    bars_qfq, bars_raw, cross_tx, fundamentals = {}, {}, {}, {}
    for u in sel_cn4:
        s0 = _start_for(u["secid"])       # T0.3：增量时自次日；否则 = start（默认不变）
        qfq = _retry(lambda: ak.stock_zh_a_daily(symbol=u["sina"], start_date=s0.replace("-", ""),
                                                 end_date=today.replace("-", ""), adjust="qfq"))
        raw = _retry(lambda: ak.stock_zh_a_daily(symbol=u["sina"], start_date=s0.replace("-", ""),
                                                 end_date=today.replace("-", ""), adjust=""))
        tq = (dt.date.today() - dt.timedelta(days=120)).isoformat().replace("-", "")
        tx = _retry(lambda: ak.stock_zh_a_hist_tx(symbol=u["sina"], start_date=tq, end_date=today.replace("-", "")))

        recs, prev = [], None
        for _, r in qfq.iterrows():
            c = _num(r["close"])
            pct = (c / prev - 1) if (prev and c and prev) else None
            recs.append({"dt": _iso(r["date"]), "open": _num(r["open"]), "high": _num(r["high"]),
                         "low": _num(r["low"]), "close": c, "volume": int(r["volume"]),
                         "amount": _num(r["amount"]), "pct": pct})
            prev = c
        bars_qfq[u["secid"]] = recs
        bars_raw[u["secid"]] = [{"dt": _iso(r["date"]), "close": _num(r["close"])} for _, r in raw.iterrows()]
        cross_tx[u["secid"]] = [{"dt": _iso(r["date"]), "close": _num(r["close"])} for _, r in tx.iterrows()]

        fin = _retry(lambda: ak.stock_financial_analysis_indicator(symbol=u["code"], start_year=fin_start_year))
        reports = []
        for _, r in fin.iterrows():
            period = _iso(r["日期"])
            if not period:
                continue
            reports.append({"period": period, "announce_date": _announce_date(period),
                            **{k: _num(r.get(c)) for k, c in FIN_COLS.items()}})
        dvd = _retry(lambda: ak.stock_history_dividend_detail(symbol=u["code"], indicator="分红"))
        dividends = [{"ex_date": _iso(r["除权除息日"]), "announce": _iso(r["公告日期"]),
                      "dps": (_num(r["派息"]) or 0) / 10, "song": _num(r["送股"]), "zhuang": _num(r["转增"])}
                     for _, r in dvd.iterrows()
                     if _iso(r["除权除息日"]) and str(r.get("进度")) == "实施"]
        fundamentals[u["secid"]] = {"reports": sorted(reports, key=lambda x: x["period"]),
                                    "dividends": sorted(dividends, key=lambda x: x["ex_date"])}
        print(f"  ✓ {u['name']} {u['secid']}：bars={len(recs)} raw={len(bars_raw[u['secid']])} "
              f"tx={len(cross_tx[u['secid']])} 报告期={len(reports)} 分红={len(dividends)}")

    # ── ②' multi5 基金标的：新浪 fund_etf_hist_sina（ETF/LOF 通用，不复权）+ 腾讯交叉源 ──
    tq8 = (dt.date.today() - dt.timedelta(days=120)).isoformat().replace("-", "")
    corp_actions = []   # 份额折算/拆分登记（价格断层保护）
    for u in sel_multi5:
        s0 = _start_for(u["secid"])       # T0.3：增量时自次日；否则 = start（默认不变）
        hist = _retry(lambda: ak.fund_etf_hist_sina(symbol=u["sina"]))
        has_amt = "amount" in getattr(hist, "columns", [])
        recs, prev = [], None
        for _, r in hist.iterrows():
            c = _num(r["close"])
            pct = (c / prev - 1) if (prev and c and prev) else None
            if pct is not None and abs(pct) > 0.33:
                # ETF/LOF 单日涨跌幅远小于 33%：此类跳变为份额折算/拆分，非真实收益 →
                # pct 置 None（持有人份额同比例变化、市值连续，组合净值按 0 收益处理）
                corp_actions.append({"secid": u["secid"], "dt": _iso(r["date"]),
                                     "prev_close": prev, "close": c, "chg": round(pct, 4),
                                     "note": "疑似份额折算/拆分（新浪无复权接口），该日 pct 置 None"})
                pct = None
            v = _num(r["volume"])
            recs.append({"dt": _iso(r["date"]), "open": _num(r["open"]), "high": _num(r["high"]),
                         "low": _num(r["low"]), "close": c, "volume": int(v) if v is not None else None,
                         "amount": _num(r["amount"]) if has_amt else None, "pct": pct})
            prev = c
        recs = [r for r in recs if r["dt"] and r["dt"] >= s0]   # 全量历史 → 起始日切片（pct 链完整保留）
        bars_qfq[u["secid"]] = recs                                # 基金无复权接口：主序列即 raw
        tx = _retry(lambda: ak.stock_zh_a_hist_tx(symbol=u["sina"], start_date=tq8,
                                                  end_date=today.replace("-", "")))
        cross_tx[u["secid"]] = [{"dt": _iso(r["date"]), "close": _num(r["close"])}
                                for _, r in tx.iterrows() if _iso(r["date"])]
        print(f"  ✓ {u['name']} {u['secid']}：bars={len(recs)}（sina 不复权） tx_cross={len(cross_tx[u['secid']])}")

    _reg("dwd_daily_bar", "ak.stock_zh_a_daily(adjust='qfq') + ak.fund_etf_hist_sina", "新浪财经",
         {"start": start, "end": today, "note": "股票 qfq 前复权；ETF/LOF 无复权接口（raw）"}, bars_qfq, "market/daily_bars.json")
    _reg("dwd_daily_bar_raw", "ak.stock_zh_a_daily(adjust='')", "新浪财经",
         {"start": start, "end": today, "note": "仅股票（基金无复权对，由 cross_source 双源覆盖）"}, bars_raw, "market/daily_bars_raw.json")
    _reg("cross_check_tx", "ak.stock_zh_a_hist_tx()", "腾讯证券",
         {"window_days": 120, "note": "股票与基金（ETF/LOF）均对腾讯（独立源交叉）"}, cross_tx, "market/cross_check_tx.json")
    _reg("dwd_financial_report", "ak.stock_financial_analysis_indicator()", "新浪财经",
         {"start_year": fin_start_year}, fundamentals, "factors/fundamental_values.json")

    # ── ③ 维度表 / 组合 / 因子注册中心 / 汇率占位 ──
    (repo / "dim" / "security.json").write_text(json.dumps(
        [{"secid": u["secid"], "name": u["name"], "market": "A", "asset_type": "STOCK",
          "industry": u["industry"], "source": "组合定义（行业为申万口径常识标注）"} for u in CN4] +
        [{"secid": u["secid"], "name": u["name"], "market": "A",
          "asset_type": "ETF" if u["ftype"] == "etf" else "LOF",
          "industry": u["industry"], "source": "组合定义二 multi5（多资产）"} for u in MULTI5],
        ensure_ascii=False, indent=2), encoding="utf-8")
    (repo / "portfolio" / "cn4.json").write_text(json.dumps({
        "id": "cn4", "name": "cn4 · 赛轮/永新/福耀/分众", "base_ccy": "CNY",
        "strategy_binding": "cn4_quality_v1",
        "note": "用户指定四标的（永新=永新股份002014.SZ）；未提供权重 → 等权名义组合 25%×4",
        "positions": [{"secid": u["secid"], "name": u["name"], "market": "A",
                       "weight": 25.0, "trade_ccy": "CNY"} for u in CN4]},
        ensure_ascii=False, indent=2), encoding="utf-8")
    (repo / "portfolio" / "multi5.json").write_text(json.dumps({
        "id": "multi5", "name": "multi5 · 沪深300/南方原油/黄金/纳指/十年国债", "base_ccy": "CNY",
        "strategy_binding": "multi_asset_v1",
        "note": "五资产等权名义组合 20%×5（A股宽基/原油/黄金/美股/国债）；全部 A 股上市、人民币计价"
                "（QDII 为人民币份额，无直接汇率敞口，存在场内溢价/折价风险）；"
                "A股无纳斯达克综合指数ETF，以纳指100ETF(513100)近似",
        "positions": [{"secid": u["secid"], "name": u["name"], "market": "A",
                       "asset_type": "ETF" if u["ftype"] == "etf" else "LOF",
                       "weight": 20.0, "trade_ccy": "CNY",
                       "settle": u["settle"], "cost": u["cost"]} for u in MULTI5]},
        ensure_ascii=False, indent=2), encoding="utf-8")
    alpha_factors = [("val_pe_ttm", "市盈率TTM"), ("val_pb", "市净率"), ("val_pe_pct", "PE五年分位"),
                     ("val_pb_pct", "PB五年分位"), ("div_yield_ttm", "股息率TTM"),
                     ("qua_roe_ttm", "ROE TTM"), ("qua_margin", "主营业务利润率"),
                     ("grow_rev_yoy", "营收同比"), ("grow_np_yoy", "净利同比"),
                     ("mom_12_1", "12-1月动量"), ("vol_250", "250日年化波动")]
    signal_factors = [("mom_20d", "20日动量"), ("vol_20d", "20日年化波动"), ("ma20_break", "MA20突破"),
                      ("high20_touch", "20日新高"), ("dist_high20", "距20日高点")]
    registry = {"factors":
                [{"id": f, "name": n, "type": "alpha", "status": "active", "version": "v1.0", "universe": ["CN"]}
                 for f, n in alpha_factors] +
                [{"id": f, "name": n, "type": "signal", "status": "active", "version": "v1.0",
                  "universe": ["CN"], "horizon_days": 20} for f, n in signal_factors]}
    (repo / "factors" / "registry.json").write_text(json.dumps(registry, ensure_ascii=False, indent=2),
                                                    encoding="utf-8")
    (repo / "market" / "fx.json").write_text(json.dumps({
        "as_of": None, "HKD_CNY": None, "USD_CNY": None,
        "note": "cn4/multi5 均为 A 股上市人民币组合，无直接汇率敞口（QDII 品种风险体现在场内溢价/折价而非汇率）；α 不注入模拟汇率"}, ensure_ascii=False, indent=2),
        encoding="utf-8")

    # ── ④ 溯源登记（自哈希最后登记）──
    if corp_actions:
        prov["fund_corporate_actions"] = corp_actions
    (repo / "market" / "provenance.json").write_text(
        json.dumps(prov, ensure_ascii=False, indent=2), encoding="utf-8")

    # 数据切换 → 远端基线重置（git 语义：新 main，旧 refs 作废）
    for f in (ctx.refs_file, ctx.crs_file):
        f.unlink(missing_ok=True)
    # T0.3：返回体增量扩展（既有字段 universe/start/fetched 保持原值不变）
    per_secid = {s: {"bars": len(arr), "last_dt": arr[-1]["dt"] if arr else None}
                 for s, arr in bars_qfq.items()}
    return {"fetched": True, "universe": [u["secid"] for u in CN4], "start": start,
            "datasets": [s["file"] for s in prov["sources"]],
            "provenance": str(repo / "market" / "provenance.json"),
            "as_of_bars": max((r["dt"] for arr in bars_qfq.values() for r in arr), default=None),
            "universe_fetched": universe_fetched, "universe_missing": universe_missing,
            "incremental_from": dict(incremental_from) if incremental_from else {},
            "per_secid": per_secid}
