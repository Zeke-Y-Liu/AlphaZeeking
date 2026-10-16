"""注册表形态兼容回归（防「盘上 v2 / 消费方按 v1 强取 factors」分歧）。

真实 workspace 注册表是 v2（``{version, engines, changelog}``，**无顶层 ``factors``**），
而历史消费方按 v1 旧形态 ``reg["factors"]`` 强取 → 静默取空/KeyError。本测试钉死统一视图
兼容层（``engines.base.factor_entries`` / ``find_factor_entry``）：v1/v2 下消费方行为一致，
且写回型消费方（假设血缘、延迟衰减降级）命中盘上底层条目、可就地落盘。

运行：``cd alphapocket && python -m pytest tests/ -q``
"""
from __future__ import annotations

from applib.engines.base import export_registry_v2, factor_entries, find_factor_entry


def _v1_reg() -> dict:
    return {"factors": [
        {"id": "val_pe_pct", "name": "PE五年分位", "type": "alpha", "status": "active"},
        {"id": "new_sig", "name": "新信号", "type": "signal", "status": "draft"},
    ]}


# ── 视图层：v2 摊平 / v1 直取 ────────────────────────────────
def test_factor_entries_v2_flattens_all_engines():
    ids = {e["id"] for e in factor_entries(export_registry_v2())}
    # alpha/beta 指标 + beta 信号 + macro 指标 全部可见（不再静默取空）
    assert {"val_pe_ttm", "ma20_break", "sig_bias_ext", "m2_yoy"} <= ids
    assert all("engine" in e for e in factor_entries(export_registry_v2()))


def test_factor_entries_v1_passthrough():
    assert [e["id"] for e in factor_entries(_v1_reg())] == ["val_pe_pct", "new_sig"]


def test_factor_entries_bad_shape_empty():
    assert factor_entries({}) == []
    assert factor_entries(None) == []          # type: ignore[arg-type]


def test_find_factor_entry_v2_returns_bottom_reference():
    reg = export_registry_v2()
    for fid in ("val_pe_ttm", "ma20_break", "sig_bias_ext"):    # alpha 指标 / beta 指标 / beta 信号
        assert find_factor_entry(reg, fid) is not None, fid
    find_factor_entry(reg, "ma20_break")["status"] = "weakening"   # 原地写回
    beta = {m["id"]: m for m in reg["engines"]["beta"]["metrics"]}
    assert beta["ma20_break"]["status"] == "weakening"


def test_find_factor_entry_v1_returns_bottom_reference():
    reg = _v1_reg()
    find_factor_entry(reg, "new_sig")["status"] = "active"
    assert reg["factors"][1]["status"] == "active"


# ── 消费方：v1/v2 行为一致（回归钉）──────────────────────────
def test_hypothesis_factor_ref_resolves_v2():
    from applib import hypothesis as HB
    reg = export_registry_v2()
    find_factor_entry(reg, "val_pe_ttm")["hypothesis_id"] = "H-0001"
    assert HB.factor_ref(reg, "val_pe_ttm")["hypothesis_id"] == "H-0001"
    assert HB.factor_ref(reg, "nope") is None


def test_delay_decay_apply_registry_writes_back_v2():
    from applib import delay_decay as DD
    reg = export_registry_v2()
    rep = {"horizon": 20, "signals": [
        {"signal_id": "sig_ma20_break", "type": "买入/持有确认", "n_events": 40, "flag": "fast_decay",
         "mean": {"t0_close": 0.10, "t1_open": 0.01}, "decay_rate": {"t1_open": 0.80}}]}
    changes = DD.apply_registry(reg, rep)
    assert changes[0]["applied"] and changes[0]["registry_id"] == "ma20_break"
    entry = find_factor_entry(reg, "ma20_break")
    assert entry["decay_flag"] == "fast_decay" and entry["status"] == "weakening"


def test_walkforward_run_validation_resolves_v2_meta():
    from applib import walkforward as WF
    reg = export_registry_v2()
    out = WF.run_validation("mom_20d", {}, {}, reg)       # 空行情 → 仅「无行情数据」，非「未注册」
    assert out.get("error") != "因子未注册：mom_20d"


def test_factor_eval_incremental_checks_sees_v2_registry():
    from applib import factor_eval as FE
    reg = export_registry_v2()
    base = find_factor_entry(reg, "mom_12_1")
    inc = FE.incremental_checks(base, ["qua_fcf"], reg, [], {}, {})
    assert inc["rows"][0].get("error") is None            # 条目已解析（不再「因子未注册」）
