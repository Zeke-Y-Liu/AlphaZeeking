"""T-R4：引擎B 配置化测试——signals/defaults 注册表驱动 + 快路径统一。

钉死四件事：
1. signal_specs 注入与默认四信号对拍（默认输出逐位一致红线 + 黄金文案）；
2. inactive 信号 → signals 缺席 + iter_signal_events 跳过 + delay_decay 报告无该信号；
3. registry defaults/signals 驱动（band/band_sigma/cooldown/启停）+ overrides 优先；
4. sensitivity._beta_series 统一后与旧 O(n) 公式逐位对拍 + run_scan 自检通过。
"""
from __future__ import annotations

import copy
import datetime as dt
import json
import math

from applib import delay_decay as DD
from applib import engines as E
from applib import factors as F
from applib import sensitivity as SEN
from applib.engines import BetaEngine
from applib.engines.base import export_registry_v2


# ── 合成行情（确定性趋势，无随机）────────────────────────────


def _mk(n=300, drift=0.009, base=10.0, start="2025-01-01", amp=0.0):
    d0 = dt.date.fromisoformat(start)
    px, arr = base, []
    for i in range(n):
        px *= 1 + (drift + amp * math.sin(i / 5.0))
        arr.append({"dt": (d0 + dt.timedelta(days=i)).isoformat(), "open": px,
                    "high": px * 1.005, "low": px * 0.995, "close": px,
                    "volume": 1e6, "amount": 1e7, "pct": drift})
    return arr


def _up(n=300):
    return _mk(n=n, drift=0.009)     # above + high20_touch + bias_ext(pos)


def _down(n=300):
    return _mk(n=n, drift=-0.01)     # below + bias_ext(neg)


def _mixed():
    return _mk(drift=0.0, amp=0.004, start="2021-01-04")   # 震荡 → vol_stop 间或触发


def _write_registry(tmp_path, reg):
    d = tmp_path / "factors"
    d.mkdir(parents=True, exist_ok=True)
    (d / "registry.json").write_text(json.dumps(reg, ensure_ascii=False), encoding="utf-8")


# ── ① signal_specs 注入与默认四信号对拍 ──────────────────────


def test_signal_specs_default_parity_and_golden_texts():
    for bars in (_up(), _down(), _mixed()):
        r0 = F.compute_beta("X", bars)
        r1 = F.compute_beta("X", bars, signal_specs=copy.deepcopy(F._BETA_SIGNALS))
        assert r0 == r1                                   # 注入内置 == 默认（逐位）
        ratio, close, n = r0["ma20_break"], r0["close"], r0["ma_window"]
        got = {s["signal_id"]: s for s in r0["signals"]}
        if r0["state"] == "below":
            assert got["sig_ma20_break"] == {
                "signal_id": "sig_ma20_break", "type": "卖出预警",
                "detail": f"收盘低于 MA20 {ratio:+.1%}（±2% 确认带外）", "evidence": "E2",
                "action": "生成减仓评估单（不自动卖出）", "cooldown": "10 交易日"}
        if r0["state"] == "above":
            assert got["sig_ma20_break"] == {
                "signal_id": "sig_ma20_break", "type": "买入/持有确认",
                "detail": f"收盘高于 MA20 {ratio:+.1%}（±2% 确认带外）", "evidence": "E2",
                "action": "引擎A目标仓位执行确认（模式③）", "cooldown": "10 交易日"}
        if got.get("sig_high20_touch"):
            assert got["sig_high20_touch"]["detail"] == f"收盘创 {n} 日收盘新高（{close:.2f}）"
        if got.get("sig_bias_ext"):
            pos = ratio > 0
            assert got["sig_bias_ext"]["detail"] == (
                f"乖离率 {ratio:+.1%}（|乖离| ≥ 8%，{'超买' if pos else '超卖'}极端）")
            assert got["sig_bias_ext"]["type"] == ("卖出预警" if pos else "买入/持有确认")


def test_signals_order_and_key_contract():
    r = F.compute_beta("X", _up())
    assert [s["signal_id"] for s in r["signals"]] == [
        "sig_ma20_break", "sig_high20_touch", "sig_bias_ext"]        # 注册表规范序
    for s in r["signals"]:
        assert list(s.keys()) == ["signal_id", "type", "detail", "evidence", "action", "cooldown"]


# ── ② inactive 信号：三处同步缺席 ────────────────────────────


def test_inactive_signal_dropped_everywhere():
    bars = _up()
    specs = [dict(s, status="inactive") if s["id"] == "sig_high20_touch" else dict(s)
             for s in F._BETA_SIGNALS]
    r = F.compute_beta("X", bars, signal_specs=specs)
    assert "sig_high20_touch" not in [s["signal_id"] for s in r["signals"]]
    assert "sig_ma20_break" in [s["signal_id"] for s in r["signals"]]   # 其余不受影响
    # iter_signal_events 同步跳过
    evs = F.iter_signal_events("X", bars, "all", signal_specs=specs)
    assert evs and all(e["signal_id"] != "sig_high20_touch" for e in evs)
    assert F.iter_signal_events("X", bars, "sig_high20_touch", signal_specs=specs) == []
    # delay_decay：报告无该信号行（signal_specs 过滤扫描面）
    rep = DD.run_decay({"X": bars}, signal_specs=specs)
    assert all(s["signal_id"] != "sig_high20_touch" for s in rep["signals"])
    assert any(s["signal_id"] == "sig_ma20_break" for s in rep["signals"])


def test_run_decay_default_equals_builtin_full_set():
    E.get_engine("beta").registry = None          # 单例回归内置默认（防跨用例污染）
    bars = {"X": _up()}
    assert DD.run_decay(bars)["signals"] == DD.run_decay(bars, signal_specs=F._BETA_SIGNALS)["signals"]


# ── ③ registry defaults/signals 驱动 + overrides 优先 ─────────


def test_registry_defaults_and_signals_drive_run(tmp_path):
    reg = export_registry_v2()
    reg["engines"]["beta"]["defaults"]["band"] = 0.01     # 收窄确认带 → 更易触发
    reg["engines"]["beta"]["defaults"]["band_sigma"] = 2.0
    reg["engines"]["beta"]["signals"][0]["status"] = "inactive"       # sig_ma20_break 停用
    _write_registry(tmp_path, reg)
    eng = BetaEngine().load(tmp_path)
    bars = _mk(drift=-0.0016)                     # ratio ≈ −1.7%：band=0.01 → below；默认 0.02 → neutral
    r0 = F.compute_beta("X", bars)
    assert r0["state"] == "neutral"
    r = eng.run("X", bars)
    assert r["band"] == 0.01 and r["band_sigma"] == 2.0
    assert r["state"] == "below" and r0["state"] != "below"
    # band_sigma 注入 → band_low/band_high 与默认逐位可对（同参直调一致）
    assert r["band_low"] == F.compute_beta("X", bars, band_sigma=2.0)["band_low"]
    # sig_ma20_break 停用：state 仍按公式（市场状态≠信号），但 signals 缺该条
    assert r["state"] == "below"
    assert "sig_ma20_break" not in [s["signal_id"] for s in r["signals"]]
    # overrides 优先于 registry：带宽覆盖生效（放宽 → 回 neutral）；注入内置 specs → 停用信号恢复
    r2 = eng.run("X", bars, overrides={"band": 0.05})
    assert r2["band"] == 0.05 and r2["state"] == "neutral"
    r3 = eng.run("X", _up(), overrides={"signal_specs": F._BETA_SIGNALS})
    assert "sig_ma20_break" in [s["signal_id"] for s in r3["signals"]]
    r4 = eng.run("X", _up())                       # registry signals：sig_ma20_break 停用
    assert "sig_ma20_break" not in [s["signal_id"] for s in r4["signals"]]
    assert r4["state"] == "above"                  # state 仍按公式（市场状态≠信号启停）


def test_detail_text_syncs_with_injected_band():
    bars = _mk(drift=-0.003)
    r = F.compute_beta("X", bars, band=0.05)      # ratio ≈ −2.9% < −5%? 否 → 用更陡趋势保证触发
    if r["state"] != "below":
        bars = _mk(drift=-0.006)
        r = F.compute_beta("X", bars, band=0.05)
    assert r["state"] == "below"
    s = next(s for s in r["signals"] if s["signal_id"] == "sig_ma20_break")
    assert "±5%" in s["detail"] and "±2%" not in s["detail"]          # 文案数值随注入参数


def test_cooldown_and_copy_override():
    bars = _up()
    specs = [dict(s, cooldown="5 交易日") if s["id"] == "sig_high20_touch" else dict(s)
             for s in F._BETA_SIGNALS]
    r = F.compute_beta("X", bars, signal_specs=specs)
    s = next(s for s in r["signals"] if s["signal_id"] == "sig_high20_touch")
    assert s["cooldown"] == "5 交易日"
    # spec 字段缺省 → 回退内置文案
    bare = [{"id": "sig_high20_touch", "status": "active"}]
    r2 = F.compute_beta("X", bars, signal_specs=bare)
    got = [x for x in r2["signals"] if x["signal_id"] == "sig_high20_touch"]
    assert got and got[0]["cooldown"] == "10 交易日" and got[0]["evidence"] == "E2"


def test_engine_signal_query_face(tmp_path):
    eng = BetaEngine()
    assert eng.active_signal_ids() == set(F.ENGINE_B_SIGNAL_IDS)
    reg = export_registry_v2()
    reg["engines"]["beta"]["signals"][1]["status"] = "experimental"   # 非 active 视同停用
    _write_registry(tmp_path, reg)
    eng.load(tmp_path)
    assert "sig_high20_touch" not in eng.active_signal_ids()
    assert "sig_high20_touch" not in [s["signal_id"] for s in eng.run("X", _up())["signals"]]


# ── ④ hk_regime：registry macro.regime 阈值注入 ──────────────


def test_hk_regime_registry_thresholds(tmp_path):
    reg = export_registry_v2()
    reg["engines"]["macro"]["regime"]["peg_weak"] = 7.80
    _write_registry(tmp_path, reg)
    eng = BetaEngine().load(tmp_path)
    series = [{"dt": "2026-09-30", "hibor1m": 3.0, "fed": 4.0, "usdhkd": 7.82}]
    assert eng.run_hk_regime(series)["peg_weak_side"] is True       # 7.82 ≥ 7.80（registry 阈值）
    assert F.compute_hk_regime(series)["peg_weak_side"] is False    # 内置 7.85 → 未触保证
    assert eng.run_hk_regime(series, overrides={"peg_weak": 7.85})["peg_weak_side"] is False
    # 未加载 → 内置默认，与直调逐位一致（含显式 chg_win）
    assert BetaEngine().run_hk_regime(series, chg_win=5) == F.compute_hk_regime(series, chg_win=5)


# ── ⑤ sensitivity 快路径统一：与旧 O(n) 公式逐位对拍 ─────────


def _legacy_beta_series(dts, closes, n, band, band_sigma):
    """改造前的 _beta_series 独立实现（原文照抄，作对拍基准）"""
    out = {}
    for i, d in enumerate(dts):
        if i < n:
            out[d] = {"ma_stop": False, "vol_stop": False, "scale": 1.0}
            continue
        w = closes[i - n + 1:i + 1]
        ma = sum(w) / n
        rets = [closes[j] / closes[j - 1] - 1 for j in range(i - n + 1, i + 1)]
        mu = sum(rets) / len(rets)
        sd = (sum((x - mu) ** 2 for x in rets) / (len(rets) - 1)) ** 0.5
        ratio = closes[i] / ma - 1
        ma_stop = ratio < -band
        vol_stop = closes[i] < ma * (1 - band_sigma * sd)
        out[d] = {"ma_stop": ma_stop, "vol_stop": vol_stop,
                  "scale": 0.6 if (ma_stop or vol_stop) else 1.0}
    return out


def test_sensitivity_beta_series_parity_with_legacy():
    for bars in (_up(), _down(), _mixed()):
        own = SEN._own_closes(bars)
        dts, closes = [d for d, _ in own], [c for _, c in own]
        for win, band, bs in [(20, 0.02, 1.5), (12, 0.01, 1.0), (30, 0.03, 2.25)]:
            assert SEN._beta_series("X", bars, win, band, bs) == \
                _legacy_beta_series(dts, closes, win, band, bs), (win, band, bs)


def test_run_scan_anchor_and_beta_consistency():
    pf = {"id": "TST", "name": "测试组合",
          "positions": [{"secid": "A.XX", "name": "A"}, {"secid": "B.XX", "name": "B"}]}
    bars = {"A.XX": _mixed(), "B.XX": _up(n=260)}
    rep = SEN.run_scan(pf, bars, {})
    assert rep["anchor_check"]["ok"] is True                       # V4-α 锚点未破坏
    c = rep["beta_state_consistency"]
    assert c["ok"] is True and c["samples"] > 0                    # 统一路径无自相矛盾
    assert rep["baseline_cell"]["cagr"] is not None
