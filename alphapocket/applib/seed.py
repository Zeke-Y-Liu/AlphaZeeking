"""远端仓库种子数据（云端 main 分支替身）——与 prototype-v2 演示数据同口径"""
from __future__ import annotations

import json
import math
from pathlib import Path

AS_OF = "2026-09-30"

# ── 行情生成（确定性，30 个交易日）────────────────────────────
SEC_META = [
    # secid, 名称, 市场, 资产类型, 行业, 基价, 漂移/日, 波幅
    ("600519.SH", "贵州茅台",   "A",   "STOCK", "食品饮料", 1450, 0.0007, 0.006),
    ("00700.HK",  "腾讯控股",   "H",   "STOCK", "资讯科技", 415,  -0.0018, 0.009),
    ("AAPL.US",   "苹果",       "US",  "STOCK", "信息技术", 228,   0.0006, 0.007),
    ("NVDA.US",   "英伟达",     "US",  "STOCK", "信息技术", 138,   0.0035, 0.012),
    ("510300.SH", "沪深300ETF", "A",   "ETF",   "宽基指数", 4.10,  0.0008, 0.004),
    ("000001.OF", "华夏成长混合","A",  "OEF",   "偏股混合", 1.321, 0.0002, 0.001),
    ("300750.SZ", "某新能源",   "A",   "STOCK", "电力设备", 245,  -0.0028, 0.014),
]

def _dates(n=30):
    # 2026-08-18 起的交易日（跳过周末，足够 30 天）
    import datetime
    d, out = datetime.date(2026, 8, 18), []
    while len(out) < n:
        if d.weekday() < 5:
            out.append(d.isoformat())
        d += datetime.timedelta(days=1)
    return out


def gen_bars() -> dict:
    """确定性价格序列；末端后处理保证信号可复现（00700 下穿 MA20、NVDA 创 20 日新高）"""
    dates = _dates()
    bars = {}
    for i, (secid, name, mkt, at, ind, base, drift, vol) in enumerate(SEC_META):
        closes = []
        for t in range(len(dates)):
            w = math.sin((t + i * 3) / 3.7) * vol
            closes.append(round(base * (1 + drift * t + w), 3 if base < 10 else 2))
        # ── 信号后处理（确定性）──
        if secid == "00700.HK":   # 收盘压到 ma20 的 ~0.975 → 下穿 −2%（±2% 确认带外）
            m = sum(closes[-20:]) / 20
            closes[-1] = round(m * 0.975, 2)
        if secid == "NVDA.US":    # 最后一日 = 近 20 日最高 ×1.01
            closes[-1] = round(max(closes[-20:]) * 1.01, 2)
        bars[secid] = [
            {"dt": dates[t], "close": c, "volume": int(2e6 + 7e5 * math.sin(t + i))}
            for t, c in enumerate(closes)
        ]
    return bars


def seed_remote(remote_repo: Path):
    """写入云端 main 分支内容（幂等：已存在则跳过）。
    V4-α 真实化：remote/repo 若含 AKShare 真实数据（provenance 标记）则不再注入模拟种子。"""
    if (remote_repo / "market" / "provenance.json").exists():
        return False
    if (remote_repo / "portfolio" / "global.json").exists():
        return False
    for sub in ("market", "factors", "portfolio", "signals", "strategy", "benchmarks", "dim"):
        (remote_repo / sub).mkdir(parents=True, exist_ok=True)

    (remote_repo / "market" / "daily_bars.json").write_text(
        json.dumps(gen_bars(), ensure_ascii=False), encoding="utf-8")
    (remote_repo / "market" / "fx.json").write_text(json.dumps({
        "as_of": AS_OF, "HKD_CNY": 0.9163, "USD_CNY": 7.118,
        "source": "央行/FRED 中间价（模拟）"}, ensure_ascii=False, indent=2), encoding="utf-8")

    (remote_repo / "dim" / "security.json").write_text(json.dumps(
        [{"secid": s, "name": n, "market": m, "asset_type": at, "industry": ind}
         for s, n, m, at, ind, *_ in SEC_META], ensure_ascii=False, indent=2), encoding="utf-8")

    (remote_repo / "factors" / "registry.json").write_text(json.dumps({
        "factors": [
            {"id": "val_pe_ttm",     "name": "市盈率TTM",   "type": "alpha", "status": "active",    "version": "v1.0", "universe": ["CN"]},
            {"id": "qua_roe_ttm",    "name": "ROE TTM",     "type": "alpha", "status": "active",    "version": "v1.1", "universe": ["CN"]},
            {"id": "mom_12_1",       "name": "12-1月动量",  "type": "alpha", "status": "weakening", "version": "v1.0",
             "universe": ["CN"], "fail_label": "大盘成长环境失效（2026H1 起IC衰减62%）"},
            {"id": "sig_ma20_break", "name": "MA20突破",    "type": "signal", "status": "active",   "version": "v1.0", "universe": ["CN"], "horizon_days": 20},
            {"id": "sig_high20_touch","name": "20日新高",   "type": "signal", "status": "active",   "version": "v1.0", "universe": ["CN"], "horizon_days": 20},
            {"id": "sig_atr_regime", "name": "波动Regime",  "type": "signal", "status": "active",   "version": "v1.0", "universe": ["CN"], "horizon_days": 20},
        ]}, ensure_ascii=False, indent=2), encoding="utf-8")

    (remote_repo / "factors" / "fundamental_values.json").write_text(json.dumps({
        "as_of": AS_OF, "announce_date": "2026-08-29",
        "values": {
            "600519.SH": {"pe": 24.1, "roe": 31.2, "div_yield": 3.4},
            "00700.HK":  {"pe": 21.8, "roe": 19.6, "div_yield": 1.1},
            "AAPL.US":   {"pe": 33.5, "roe": 55.1, "div_yield": 0.5},
            "NVDA.US":   {"pe": 48.2, "roe": 42.8, "div_yield": 0.03},
            "300750.SZ": {"pe": 35.1, "roe": 12.4, "div_yield": 0.2},
        }}, ensure_ascii=False, indent=2), encoding="utf-8")

    # ── 组合快照（多市场，与原型 v3.1 同口径）──
    (remote_repo / "portfolio" / "global.json").write_text(json.dumps({
        "id": "global", "name": "全球成长", "base_ccy": "CNY", "as_of": AS_OF,
        "total_value_cny": 986400, "day_pnl_cny": 8340, "day_ret": 0.0085,
        "strategy_binding": "global_growth_v1",
        "positions": [
            {"secid": "600519.SH", "name": "贵州茅台",   "market": "A",   "weight": 22.0, "trade_ccy": "CNY",
             "grades": {"估值": "B", "成长": "C+", "质量": "A", "动量": "B+", "预期": "B"}, "you": "A-"},
            {"secid": "00700.HK",  "name": "腾讯控股",   "market": "H",   "weight": 14.0, "trade_ccy": "HKD",
             "flags": ["数据陈旧(半年报)"],
             "grades": {"估值": "B+", "成长": "B+", "质量": "A", "动量": "B", "预期": "C+"}, "you": "B-"},
            {"secid": "AAPL.US",   "name": "苹果",       "market": "US",  "weight": 12.0, "trade_ccy": "USD",
             "flags": ["non-GAAP 口径"],
             "grades": {"估值": "C+", "成长": "B+", "质量": "A-", "动量": "A", "预期": "B"}, "you": "B-"},
            {"secid": "NVDA.US",   "name": "英伟达",     "market": "US",  "weight": 7.0, "trade_ccy": "USD",
             "grades": {"估值": "D+", "成长": "A+", "质量": "B+", "动量": "A+", "预期": "B+"}, "you": None},
            {"secid": "510300.SH", "name": "沪深300ETF", "market": "A",   "weight": 16.5, "trade_ccy": "CNY",
             "asset_type": "ETF", "fund_checkup": "A"},
            {"secid": "000001.OF", "name": "华夏成长混合","market": "A",  "weight": 7.4, "trade_ccy": "CNY",
             "asset_type": "OEF", "fund_checkup": "B+", "flags": ["净值 09-29"]},
            {"secid": "300750.SZ", "name": "某新能源",   "market": "A",   "weight": 3.1, "trade_ccy": "CNY",
             "grades": {"估值": "D+", "成长": "B-", "质量": "B", "动量": "D", "预期": "D"}, "you": "D"},
        ],
        "health": {"score": 74, "delta": "+2", "positions": 7, "ge_b": 5, "review_pending": 1, "fx_contrib": -0.007},
        "market_split": [["A", 25], ["H", 14], ["US", 19], ["ETF", 17], ["OF", 7], ["CASH", 18]],
        "performance": {"年化收益": "+16.2%", "最大回撤": "-11.8%", "夏普": "1.18",
                        "年化波动": "13.9%", "换手率": "38%", "股息率": "2.3%"},
    }, ensure_ascii=False, indent=2), encoding="utf-8")

    (remote_repo / "portfolio" / "attribution.json").write_text(json.dumps({
        "as_of": AS_OF, "benchmark": "复合（沪深300×45+恒生×20+纳指100×30+现金×5）",
        "bench_ret_1y": 0.112, "excess_1y": 0.050,
        "items": [
            {"name": "市场配置", "bp": +4.2, "w": 42},
            {"name": "行业配置", "bp": +3.1, "w": 31},
            {"name": "个股选择", "bp": +8.6, "w": 86},
            {"name": "汇率效应", "bp": -2.7, "w": 27},
            {"name": "交易成本", "bp": -1.4, "w": 14},
        ]}, ensure_ascii=False, indent=2), encoding="utf-8")

    (remote_repo / "portfolio" / "fund_checkups.json").write_text(json.dumps({
        "510300.SH": {"grade": "A",  "items": [["跟踪误差", "0.8%"], ["折溢价", "+0.12%"], ["日均成交", "¥18.6亿"], ["规模", "¥1,554亿"], ["综合费率", "0.50%"]]},
        "000001.OF": {"grade": "B+", "items": [["同类分位", "前38%"], ["最大回撤", "-14.2%"], ["经理任期", "4.2年"], ["规模/申购", "开放"], ["赎回费", "<7日 1.5%"]]},
    }, ensure_ascii=False, indent=2), encoding="utf-8")

    (remote_repo / "signals" / "regime.json").write_text(json.dumps({
        "as_of": AS_OF, "atr_regime": "high", "position_coef": 0.95,
        "note": "sig_atr_regime 高波 Regime · 月内第 1 次 · 微调上限 ±10%"}, ensure_ascii=False, indent=2), encoding="utf-8")

    (remote_repo / "strategy" / "global_growth.json").write_text(json.dumps({
        "id": "global_growth_v1", "name": "全球成长",
        "universe": "CN+HK+US 多市场", "rebalance": "monthly", "exec": "next_day_vwap",
        "factors": ["val_pe_ttm", "qua_roe_ttm", "mom_12_1"],
        "engine_b": {"enabled": True, "modes": ["入场确认", "执行择时", "卖出触发"]},
    }, ensure_ascii=False, indent=2), encoding="utf-8")
    return True
