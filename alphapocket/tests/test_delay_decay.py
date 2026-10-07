"""R4 时间延迟敏感性实证测试（《研究缺口R1-R4实施计划》§6.4 DoD）

- 三个信号事件类型各产出衰减表（全历史 + 分年度）
- 注入测试：单日脉冲有效的假信号 → T+1 衰减率 > 50% 正确识别
- 降级演练：fast_decay → registry 流转 weakening/decay_flag + 报告 caveat 生效
- 小样本保护：事件数 < 30 → 只标注不降级
运行：``cd alphapocket && python -m pytest tests/ -q``
"""
from __future__ import annotations

import datetime as dt

import pytest

from applib import delay_decay as DD
from applib import factors as F
from applib import report as RP


def _pulse_events(n: int, sid: str = "sig_fake_pulse", stype: str = "买入/持有确认",
                  t0: float = 100.0, t1: float = 108.0, t2: float = 109.0, exit_: float = 110.0) -> list[dict]:
    """构造仅单日脉冲有效的假信号事件（T+1 起均值回归）"""
    return [{"secid": "X", "dt": f"2023-{i % 12 + 1:02d}-02", "signal_id": sid, "type": stype,
             "exec_prices": {"t0_close": t0, "t1_open": t1, "t1_close": t1, "t2_open": t2},
             "exit_close": exit_} for i in range(n)]


def _synthetic_bars(n: int = 80) -> list[dict]:
    """合成 K 线：先跌后涨，确保触发 ma20 破位与 20 日新高信号"""
    d0, px, out = dt.date(2023, 1, 2), 100.0, []
    for i in range(n):
        if 30 <= i < 36:
            px *= 0.985
        elif 36 <= i < 70:
            px *= 1.012
        out.append({"dt": (d0 + dt.timedelta(days=i)).isoformat(), "open": round(px, 4),
                    "high": round(px, 4), "low": round(px, 4), "close": round(px, 4),
                    "volume": 1, "amount": 1.0, "pct": 0.0})
    return out


# ── 方向口径 ───────────────────────────────────────────
def test_sell_warning_uses_drawdown_protection_direction():
    ev = {"secid": "X", "dt": "2023-01-02", "signal_id": "sig_ma20_break", "type": "卖出预警",
          "exec_prices": {"t0_close": 100.0, "t1_open": 100.0, "t1_close": 100.0, "t2_open": 100.0},
          "exit_close": 90.0}
    r = DD.event_returns(ev)
    assert r["t0_close"] == pytest.approx(0.10)     # 价格跌 10% → 跌幅保护口径 +10%


# ── 三个信号事件类型各产出衰减表（全历史 + 分年度）──────
def test_run_decay_produces_table_for_real_signals():
    rep = DD.run_decay({"SYN": _synthetic_bars()})
    assert rep["n_events"] > 0 and rep["n_signals"] >= 1
    for s in rep["signals"]:
        assert len(s["mean"]) == 4 and len(s["decay_rate"]) == 4     # 0/1/2 日四种执行时点
        assert s["by_year"] and all("n" in d and "decay" in d for d in s["by_year"].values())
        assert s["flag"] in ("ok", "insufficient", "fast_decay")
    assert DD.render_md(rep) and DD.render_html(rep)
    assert "时间延迟衰减报告" in DD.render_md(rep)


# ── 注入测试：单日脉冲假信号 → T+1 衰减率 > 50% ─────────
def test_injection_pulse_signal_flagged_fast_decay():
    rep = DD.analyze_events(_pulse_events(40), horizon=20)
    s = rep["signals"][0]
    assert s["flag"] == "fast_decay"
    assert s["decay_rate"]["t1_open"] > 0.50
    # t0 捕获 +10%，T+1 开盘仅捕获 110/108-1≈1.85% → 衰减≈81%
    assert s["decay_rate"]["t1_open"] == pytest.approx((0.10 - (110 / 108 - 1)) / 0.10, rel=1e-3)


# ── 小样本保护：事件数 < 30 → 只标注不降级 ──────────────
def test_small_sample_not_downgraded():
    rep = DD.analyze_events(_pulse_events(12), horizon=20)
    assert rep["signals"][0]["flag"] == "insufficient"
    assert "待全量历史补强" in rep["signals"][0]["verdict"]
    reg = {"factors": [{"id": "sig_fake_pulse", "name": "假信号", "type": "signal", "status": "active"}]}
    changes = DD.apply_registry(reg, rep)
    assert changes[0]["applied"] and changes[0]["flag"] == "insufficient"
    assert reg["factors"][0]["status"] == "active"        # 未降级
    assert "decay_flag" not in reg["factors"][0]


# ── 降级演练：fast_decay → registry 流转 + 报告 caveat ──
def test_downgrade_drill_registry_and_caveat():
    rep = DD.analyze_events(_pulse_events(40), horizon=20)
    reg = {"factors": [{"id": "sig_fake_pulse", "name": "假信号", "type": "signal", "status": "active"}]}
    changes = DD.apply_registry(reg, rep)
    f = reg["factors"][0]
    assert changes[0]["status"] == "active→weakening"
    assert f["status"] == "weakening" and f["decay_flag"] == "fast_decay"
    assert f["delay_decay"]["flag"] == "fast_decay" and f["delay_decay"]["n_events"] == 40
    # 报告 caveat 生效
    review = {"decay_caveats": [{"signal_id": f["id"], "name": f["name"],
                                 "decay_rate": f["delay_decay"]["decay_rate"], "n_events": 40}]}
    md, html = RP.decay_caveat_md(review), RP.decay_caveat_html(review)
    assert "信号延迟衰减 caveat" in md and f["id"] in md
    assert "信号延迟衰减 caveat" in html and "weakening" in html
    assert RP.decay_caveat_md({}) == "" and RP.decay_caveat_html({"decay_caveats": []}) == ""


# ── 映射：sig_ma20_break / sig_high20_touch → 既有 signal 条目 ──
def test_registry_mapping_targets_existing_signal_entries():
    assert DD.SIGNAL_REGISTRY_MAP == {"sig_ma20_break": "ma20_break", "sig_high20_touch": "high20_touch",
                                      "sig_bias_ext": "sig_bias_ext",
                                      "sig_vol_price_div": "sig_vol_price_div"}
    rep = DD.analyze_events(_pulse_events(40, sid="sig_ma20_break", stype="买入/持有确认"))
    reg = {"factors": [{"id": "ma20_break", "name": "MA20突破", "type": "signal", "status": "active"}]}
    changes = DD.apply_registry(reg, rep)
    assert changes[0]["registry_id"] == "ma20_break" and reg["factors"][0]["decay_flag"] == "fast_decay"


# ── iter_signal_events 与 compute_beta 同源（抽样一致性）──
def test_iter_signal_events_matches_compute_beta():
    bars = _synthetic_bars()
    evs = F.iter_signal_events("SYN", bars)
    for e in evs[:20]:
        b = F.compute_beta("SYN", bars, e["dt"])
        assert any(s["signal_id"] == e["signal_id"] and s["type"] == e["type"] for s in b["signals"])
        assert e["exec_prices"]["t0_close"] == b["close"]
