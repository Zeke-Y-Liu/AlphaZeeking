"""R2 walk-forward 协议测试（《研究缺口R1-R4实施计划》§4.4 DoD）

- 切分引擎：给定区间与配置，folds 与协议表一致；月末/闰年边界正确
- 注入测试①：窗口越界的检验报告 → audit_split ② 取数越界 FAIL
- 注入测试②：lockbox 区间同版本两份验证报告 → audit_split ③ FAIL
- 端到端：run_validation 产物 + walk_forward_v1 注册表 → audit_split 全绿
运行：``cd alphapocket && python -m pytest tests/ -q``
"""
from __future__ import annotations

import datetime as dt
import math

from applib import walkforward as WF

CFG_246 = {"train_min_months": 24, "valid_months": 6, "step_months": 6}


# ── 切分引擎 ───────────────────────────────────────────
def test_make_folds_matches_protocol_table():
    r = WF.make_folds(("2021-01-04", "2026-09-30"), CFG_246)
    assert r["n_folds"] == 6
    assert r["folds"][0] == {"fold": 1, "train": ["2021-01-01", "2022-12-31"],
                             "valid": ["2023-01-01", "2023-06-30"]}
    assert r["folds"][5] == {"fold": 6, "train": ["2021-01-01", "2025-06-30"],
                             "valid": ["2025-07-01", "2025-12-31"]}
    assert r["lockbox"] == ["2026-01-01", "2026-09-30"]
    # 扩展窗口：train 起点固定、valid 等长前滚、互不重叠
    assert all(f["train"][0] == "2021-01-01" for f in r["folds"])
    for a, b in zip(r["folds"], r["folds"][1:]):
        assert b["valid"][0] > a["valid"][1]
        assert b["train"][1] < b["valid"][0]
        assert b["train"][1][:7] < b["valid"][0][:7]


def test_make_folds_prod_config_and_insufficient_data():
    prod = WF.make_folds(("2016-01-01", "2026-09-30"),
                         {"train_min_months": 60, "valid_months": 12, "step_months": 12})
    assert prod["n_folds"] == 4
    assert prod["lockbox"] == ["2025-01-01", "2026-09-30"]
    # 数据不足（train_min 后无完整 valid+lockbox）→ 0 折，无可验证样本外
    empty = WF.make_folds(("2021-01-04", "2023-06-30"), CFG_246)
    assert empty["n_folds"] == 0
    assert empty["lockbox"] == ["2023-01-01", "2023-06-30"]


def test_month_end_boundaries_leap_year():
    assert WF._month_end("2024-02") == "2024-02-29"   # 闰年 2 月
    assert WF._month_end("2023-02") == "2023-02-28"
    assert WF._month_end("2021-12") == "2021-12-31"
    assert WF._add_months("2021-11", 2) == "2022-01"  # 跨年进位
    assert WF._add_months("2021-01", -1) == "2020-12"


# ── 合成数据（价格类因子，无需财报）─────────────────────
def _synth_bars(n: int = 6) -> dict:
    days, d, end = [], dt.date(2021, 1, 4), dt.date(2026, 9, 30)
    while d <= end:
        if d.weekday() < 5:
            days.append(d.isoformat())
        d += dt.timedelta(days=1)
    bars = {}
    for k in range(n):
        px, arr = 100.0, []
        for i, day in enumerate(days):
            px *= 1 + 0.002 * math.sin((i + k) / 11.0) + 0.0004 * ((i + k) % 5 - 2)
            arr.append({"dt": day, "close": round(px, 4),
                        "high": round(px * 1.01, 4), "low": round(px * 0.99, 4)})
        bars[f"S{k}.XX"] = arr
    return bars


def _wf_registry() -> dict:
    split = WF.make_folds(("2021-01-04", "2026-09-30"), CFG_246)
    return {"factors": [{"id": "mom_20d", "name": "20日动量", "type": "signal",
                         "status": "active", "version": "v1.0", "universe": ["CN"],
                         "horizon_days": 20,
                         "validation": {"protocol": "walk_forward_v1", "folds": split["folds"],
                                        "lockbox": split["lockbox"], "lockbox_used_by": None}}]}


# ── 端到端：合规产物 → 全绿 ─────────────────────────────
def test_run_validation_and_audit_pass():
    reg = _wf_registry()
    rep = WF.run_validation("mom_20d", _synth_bars(), {}, reg, CFG_246)
    assert rep["protocol"] == "walk_forward_v1"
    assert rep["n_folds"] == 6 and rep["lockbox"] == ["2026-01-01", "2026-09-30"]
    for f in rep["folds"]:                       # 数据窗口 ⊆ 对应 train/valid，未触 lockbox
        assert f["train"][0] <= f["data_window"][0]
        assert f["data_window"][1] <= f["valid"][1]
    assert any(f["metrics"]["n_dates"] > 0 for f in rep["folds"])
    res = WF.audit_records(reg, [rep])
    assert res["audit_split"] == "pass", res["audit_split"]


# ── 停牌日：某标的缺失月末 K 线 → 该观测跳过、不报错、审计仍合规 ──
def test_suspension_days_skipped():
    bars = _synth_bars()
    month_ends = set(WF._month_end_dates([r["dt"] for r in bars["S1.XX"]]))
    bars["S0.XX"] = [r for r in bars["S0.XX"] if r["dt"] not in month_ends]   # S0 月末停牌
    reg = _wf_registry()
    rep = WF.run_validation("mom_20d", bars, {}, reg, CFG_246)
    assert rep["n_folds"] == 6
    assert any(f["metrics"]["n_dates"] > 0 for f in rep["folds"])
    for f in rep["folds"]:
        assert f["valid"][0] <= f["data_window"][0] <= f["data_window"][1] <= f["valid"][1]
    assert WF.audit_records(reg, [rep])["audit_split"] == "pass"


# ── 审计 ① 字段完备 ────────────────────────────────────
def test_injection_missing_validation_field():
    reg = {"factors": [{"id": "val_pe_ttm", "name": "市盈率TTM", "type": "alpha",
                        "status": "active", "version": "v1.0", "universe": ["CN"]}]}
    res = WF.audit_records(reg, [])
    assert not res["checks"]["fields"]["ok"]
    assert "val_pe_ttm" in res["checks"]["fields"]["missing"]
    assert res["audit_split"].startswith("FAIL")


# ── 注入测试① 取数越界：手工构造越界报告 → audit_split ② FAIL ──
def test_injection_bounds_violation():
    reg = _wf_registry()
    bad = {"factor_id": "rev_20d", "version": "v1.0", "lockbox": ["2026-01-01", "2026-09-30"],
           "lockbox_used": False,
           "folds": [{"fold": 1, "train": ["2021-01-01", "2022-12-31"],
                      "valid": ["2023-01-01", "2023-06-30"],
                      "data_window": ["2023-04-01", "2024-03-31"]}]}   # 越出 valid 末端 9 个月
    res = WF.audit_records(reg, [bad])
    assert not res["checks"]["bounds"]["ok"]
    assert any("rev_20d" in v for v in res["checks"]["bounds"]["violations"])
    assert res["audit_split"].startswith("FAIL") and "bounds" in res["audit_split"]


# ── 注入测试② lockbox 一次性：同版本两份验证报告 → audit_split ③ FAIL ──
def test_injection_lockbox_double_use():
    reg = _wf_registry()

    def mk(tag):
        return {"factor_id": "mom_20d", "version": "v1.0", "generated": tag,
                "lockbox": ["2026-01-01", "2026-09-30"], "lockbox_used": True,
                "folds": [{"fold": 6, "train": ["2021-01-01", "2025-06-30"],
                           "valid": ["2025-07-01", "2025-12-31"],
                           "data_window": ["2025-07-01", "2025-11-28"]}]}   # 数据窗口合规、显式使用 lockbox
    res = WF.audit_records(reg, [mk("r1"), mk("r2")])
    assert res["checks"]["bounds"]["ok"]                 # ② 合规，隔离出 ③
    assert not res["checks"]["lockbox"]["ok"]
    assert "mom_20d@v1.0" in res["checks"]["lockbox"]["duplicates"]
    assert res["audit_split"].startswith("FAIL") and "lockbox" in res["audit_split"]


# ── 审计 ④ 折叠重叠（step < valid 造成的 valid 交叠）─────
def test_audit_fold_overlap_detected():
    bad_folds = [{"train": ["2021-01-01", "2022-12-31"], "valid": ["2023-01-01", "2023-06-30"]},
                 {"train": ["2021-01-01", "2023-03-31"], "valid": ["2023-04-01", "2023-09-30"]}]
    reg = {"factors": [{"id": "x", "status": "active", "type": "alpha", "version": "v1.0",
                        "validation": {"protocol": "walk_forward_v1", "folds": bad_folds,
                                       "lockbox": ["2026-01-01", None]}}]}
    res = WF.audit_records(reg, [])
    assert not res["checks"]["folds"]["ok"]
    assert res["checks"]["fields"]["ok"]


# ── grandfathered 存量因子：④ 跳过、① 通过 ─────────────
def test_grandfathered_registry_passes():
    reg = {"factors": [{"id": "val_pe_ttm", "status": "active", "type": "alpha", "version": "v1.0",
                        "validation": {"protocol": "inherited_fixed_split", "grandfathered": True}}]}
    res = WF.audit_records(reg, [])
    assert res["audit_split"] == "pass"
