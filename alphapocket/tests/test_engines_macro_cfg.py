"""引擎C 配置化测试（T-R3）：dim_of/scorers/dim_weights/active_ids 注入 + registry 装配。

- 对拍红线：默认注册表（内置常量）下引擎 run 与 factors 直调输出逐位一致；
- 注入 dim_of/scorers（= 内置常量值）与默认路径输出相等；
- active_ids 白名单：停用指标 missing="inactive"、维度 n_total 缩分母、市场分按手算变化；
- dim_weights 自定义/缺失维度重归一化；registry 真实文件驱动；overrides 优先于 registry；
- compute_hk_regime 阈值（peg_strong/peg_weak/carry_widen_pt）注入生效。

运行：``cd alphapocket && python -m pytest tests/test_engines_macro_cfg.py -q``
"""
from __future__ import annotations

import copy
import datetime as _d
import json

import pytest

from applib import engines as E
from applib import factors as F


def _mk(key, vals, dt_start="2026-06-01", freq="M"):
    """构造 {key: [{dt, v}]}：升序合成序列（月频取每月 1 日）。"""
    cur = _d.date.fromisoformat(dt_start)
    rows = []
    for v in vals:
        rows.append({"dt": cur.isoformat(), "v": v})
        if freq == "M":
            cur = _d.date(cur.year + (cur.month == 12), 1 if cur.month == 12 else cur.month + 1, 1)
        else:
            cur += _d.timedelta(days=1)
    return {key: rows}


def _hk(n=10, hibor=4.0, fed=3.0, usdhkd=7.78, hibor_step=0.0):
    """hk_rates 逐日序列（hibor_step：逐日下行 → 息差走阔）。"""
    return [{"dt": f"2026-09-{i + 1:02d}", "hibor1m": hibor - hibor_step * i,
             "fed": fed, "usdhkd": usdhkd} for i in range(n)]


def _fixture() -> dict:
    """A 市场合成：D1=0 / D2=0 / D3=1（仅 lpr，shibor 缺数据）/ D4=0 → A 分手算 = 0.25。"""
    series = {}
    series.update(_mk("rrr_big", [9.0, 9.0]))                              # step 未动 → 0
    series.update(_mk("m2_yoy", [6.0, 6.0, 6.0]))                          # mom 0 → 0
    series.update(_mk("tsf_new_12m", [200000.0] * 3))                      # mom 0 → 0
    series.update(_mk("lpr_1y", [3.5, 3.5, 3.0]))                          # step 降 → +1
    series.update(_mk("cn_us_10y", [0.0], "2026-09-01", "D"))              # clip 0/1.5 → 0
    return series


ALL_MACRO_IDS = set(F.MACRO_FACTORS) | {"hibor_1m", "carry_hk_us", "peg_pressure"}


# ── 1. 注入 dim_of/scorers 与默认常量对拍（同输入输出相等）────────
def test_inject_default_config_matches_builtin():
    series, hk = _fixture(), _hk()
    r0 = F.compute_macro_liquidity(series, hk_series=hk)
    r1 = F.compute_macro_liquidity(
        series, hk_series=hk,
        dim_of=copy.deepcopy(F._MACRO_DIM_OF), scorers=dict(F._MACRO_SCORERS),
        dim_weights=dict(F.MACRO_DIM_WEIGHTS))
    assert r0 == r1


# ── 2. 不传新参：引擎（默认注册表）与直调逐位一致 ─────────────────
def test_engine_default_registry_bitwise_equal_direct_call():
    series, hk = _fixture(), _hk()
    assert E.MacroEngine().run(series, hk_series=hk) \
        == F.compute_macro_liquidity(series, hk_series=hk)


# ── 3. active_ids 白名单：missing 注记 + n_total 缩分母 + 市场分手算 ──
def test_active_ids_excludes_metric():
    series = _fixture()
    base = F.compute_macro_liquidity(series)
    assert base["markets"]["A"]["score"] == pytest.approx(0.25)   # 手算基线
    assert base["markets"]["A"]["dims"]["D3"]["n_total"] == 2

    r = F.compute_macro_liquidity(series, active_ids=ALL_MACRO_IDS - {"lpr_1y"})
    ind = r["indicators"]["A"]["lpr_1y"]
    assert ind["score"] is None and ind["missing"] == "inactive"
    assert ind["value"] == 3.0                                    # value 仍登记
    d3 = r["markets"]["A"]["dims"]["D3"]
    assert d3["n_total"] == 1 and d3["n_used"] == 0 and d3["score"] is None
    # D3 出局（权重 0.25 剔除、重归一化）→ A 分 0.25 → 0.0（手算：仅剩 0 分维度）
    assert r["markets"]["A"]["score"] == pytest.approx(0.0)
    assert r["global"]["score"] == pytest.approx(0.0)
    # notes：停用进专行，不混入「缺失指标」
    assert "指标 lpr_1y 已停用（inactive），不参与打分" in r["notes"]
    assert "缺失指标：shibor_1m" in r["notes"] and "lpr_1y" not in \
        next(n for n in r["notes"] if n.startswith("缺失指标"))


# ── 4. dim_weights 自定义 + 缺失维度重归一化 ─────────────────────
def test_dim_weights_custom_and_renormalize():
    series = _fixture()
    base = F.compute_macro_liquidity(series)
    # D1 提到 0.5（其余不变，Σw=1.2）→ s = 0.25/1.2 ≈ 0.2083 ≠ 0.25
    r = F.compute_macro_liquidity(
        series, dim_weights={"D1": 0.5, "D2": 0.25, "D3": 0.25, "D4": 0.2})
    assert r["markets"]["A"]["score"] == pytest.approx(0.25 / 1.2)
    assert r["markets"]["A"]["score"] != pytest.approx(base["markets"]["A"]["score"])
    # 只留 D3 → 重归一化后 s == D3 分（1.0）
    r2 = F.compute_macro_liquidity(series, dim_weights={"D3": 1.0})
    assert r2["markets"]["A"]["score"] == pytest.approx(1.0)
    assert r2["markets"]["A"]["dims"]["D3"]["score"] == pytest.approx(1.0)


# ── 5. registry 真实文件驱动：m2_yoy 标 inactive → 引擎 run 生效 ────
def test_registry_file_drives_config(tmp_path):
    reg = E.export_registry_v2()
    for m in reg["engines"]["macro"]["metrics"]:
        if m["id"] == "m2_yoy":
            m["status"] = "inactive"
    (tmp_path / "factors").mkdir()
    (tmp_path / "factors" / "registry.json").write_text(
        json.dumps(reg, ensure_ascii=False), encoding="utf-8")
    assert E.validate_registry(json.loads(
        (tmp_path / "factors" / "registry.json").read_text(encoding="utf-8"))) == []

    r = E.MacroEngine().load(tmp_path).run(_fixture())
    ind = r["indicators"]["A"]["m2_yoy"]
    assert ind["missing"] == "inactive" and ind["score"] is None
    assert r["markets"]["A"]["dims"]["D2"]["n_total"] == 1        # 分母只计 active
    assert r["markets"]["A"]["dims"]["D2"]["n_used"] == 1         # tsf 仍打分
    assert "指标 m2_yoy 已停用（inactive），不参与打分" in r["notes"]
    assert r["indicators"]["A"]["rrr_big"]["score"] == pytest.approx(0.0)   # 其余不受影响


# ── 6. overrides 优先于 registry（调试注入不动盘）────────────────
def test_overrides_beat_registry(tmp_path):
    reg = E.export_registry_v2()
    for m in reg["engines"]["macro"]["metrics"]:
        if m["id"] == "m2_yoy":
            m["status"] = "inactive"
    (tmp_path / "factors").mkdir()
    (tmp_path / "factors" / "registry.json").write_text(json.dumps(reg), encoding="utf-8")
    eng = E.MacroEngine().load(tmp_path)

    series = _fixture()
    r = eng.run(series, overrides={"active_ids": None})           # 临时恢复全量参与
    assert r["indicators"]["A"]["m2_yoy"]["missing"] is False
    assert r["indicators"]["A"]["m2_yoy"]["score"] == pytest.approx(0.0)
    r2 = eng.run(series)                                          # 不带 overrides → 仍停用
    assert r2["indicators"]["A"]["m2_yoy"]["missing"] == "inactive"


# ── 7. compute_hk_regime 阈值注入（PEG_STRONG / carry_widen_pt）────
def test_hk_regime_threshold_injection():
    # (a) PEG：7.84 默认不触弱方（peg=0.9）；收紧区间 [7.80, 7.84] → peg=1.0 触发
    s = _hk(5, usdhkd=7.84)
    assert F.compute_hk_regime(s)["state"] == "neutral"
    r = F.compute_hk_regime(s, peg_strong=7.80, peg_weak=7.84)
    assert r["state"] == "outflow_pressure" and r["peg_pressure"] == pytest.approx(1.0)
    assert r["peg_weak_side"] is True
    # (b) 息差急扩：20 日变化 −0.6 → 默认阈值 0.5 触发；放宽到 1.0 → 不触发
    s2 = _hk(30, hibor=5.0, hibor_step=0.03, usdhkd=7.78)
    r2 = F.compute_hk_regime(s2)
    assert r2["carry_chg20"] == pytest.approx(-0.6) and r2["carry_widen"] is True
    assert r2["state"] == "outflow_pressure"
    r3 = F.compute_hk_regime(s2, carry_widen_pt=1.0)
    assert r3["carry_widen"] is False and r3["state"] == "neutral"
