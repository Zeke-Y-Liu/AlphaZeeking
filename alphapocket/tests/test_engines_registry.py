"""T-R1：指标注册表 v2 导出器/校验器/加载器测试。

钉死三件事：
1. 导出值与 factors.py 现有常量/函数内字面量逐项一致（16/6/13 指标、锚点五组、信号四条）；
2. 校验器对各类非法输入报错、合法输入零错误；
3. 加载器三分支透明降级（无文件/坏 JSON/版本不符）+ 深拷贝隔离。
"""
import json

import pytest

from applib import factors as F
from applib.engines.base import (ALPHA_DIMS, BETA_SIGNAL_IDS, KNOWN_FORMULAS,
                                 MACRO_DIMS, REGISTRY_VERSION, export_registry_v2,
                                 load_registry, validate_registry)


# ── 导出结构完整性 ────────────────────────────────────────────

def test_export_three_engines_and_counts():
    r = export_registry_v2()
    assert r["version"] == REGISTRY_VERSION == 2
    assert set(r["engines"].keys()) == {"alpha", "beta", "macro"}
    assert len(r["engines"]["alpha"]["metrics"]) == len(F.ENGINE_A_FACTORS) == 19
    assert len(r["engines"]["beta"]["metrics"]) == len(F.ENGINE_B_FACTORS) == 6
    assert len(r["engines"]["macro"]["metrics"]) == 13   # 10 macro_liquidity + 3 hk_rates
    assert r["changelog"] == []


def test_export_matches_factors_constants():
    r = export_registry_v2()
    # id 清单与常量一致
    assert [m["id"] for m in r["engines"]["alpha"]["metrics"]] == F.ENGINE_A_FACTORS
    assert [m["id"] for m in r["engines"]["beta"]["metrics"]] == F.ENGINE_B_FACTORS
    # 权重表逐项一致
    assert r["engines"]["alpha"]["dim_weights"] == F.DEFAULT_DIM_WEIGHTS
    assert r["engines"]["macro"]["dim_weights"] == F.MACRO_DIM_WEIGHTS
    # beta 默认参数与 compute_beta 签名一致
    assert r["engines"]["beta"]["defaults"] == {
        "win": 20, "band": 0.02, "band_sigma": 1.5,
        "bias_thresh": 0.08, "vpd_thresh": 0.30, "pct_window": 250}
    # regime 阈值与 factors 常量一致
    rg = r["engines"]["macro"]["regime"]
    assert rg["peg_strong"] == F.PEG_STRONG and rg["peg_weak"] == F.PEG_WEAK
    assert rg["carry_widen_pt"] == F.HK_CARRY_WIDEN_PT


def test_export_alpha_anchors_and_dims():
    r = export_registry_v2()
    anc = r["engines"]["alpha"]["anchors"]
    # 锚点五组，值与 compute_alpha L225-231 逐点一致
    assert anc["s_qua"] == [[0, 10], [5, 35], [10, 55], [15, 75], [20, 90], [30, 100]]
    assert anc["s_gro"] == [[-20, 10], [0, 40], [10, 60], [20, 75], [35, 90], [60, 100]]
    assert anc["s_mom"] == [[-30, 10], [-10, 30], [0, 45], [10, 60], [30, 80], [60, 100]]
    assert anc["s_dvd"] == [[0, 10], [1, 35], [2, 55], [3, 70], [5, 90], [7, 100]]
    assert anc["s_lvol"] == [[15, 100], [20, 85], [30, 60], [40, 40], [60, 20]]
    # 维度归属抽验（估值/质量/动量/红利低波）
    dims = {m["id"]: m["dim"] for m in r["engines"]["alpha"]["metrics"]}
    assert dims["val_pe_ttm"] == "估值" and dims["qua_roe_ttm"] == "质量"
    assert dims["mom_12_1"] == "动量" and dims["rev_60d"] == "动量"
    assert dims["div_yield_ttm"] == "红利低波" and dims["qua_fcf"] == "质量"
    # 每条 formula 可在 KNOWN_FORMULAS 中找到
    for m in r["engines"]["alpha"]["metrics"]:
        assert m["formula"].startswith("builtin:")
        assert m["formula"].split(":", 1)[1] in KNOWN_FORMULAS


def test_export_macro_dim_of_and_scorers():
    r = export_registry_v2()
    ms = {m["id"]: m for m in r["engines"]["macro"]["metrics"]}
    # 维度归属与 _MACRO_DIM_OF 一致（反演抽验）
    assert ms["rrr_big"]["market"] == "A" and ms["rrr_big"]["dim"] == "D1"
    assert ms["m2_yoy"]["dim"] == "D2" and ms["shibor_1m"]["dim"] == "D3"
    assert ms["cn_us_10y"]["dim"] == "D4" and ms["us_10y_2y"]["market"] == "US"
    assert ms["fed_target"]["dim"] == "D1" and ms["us_cpi_surprise"]["dim"] == "D1"
    # hk_rates 三条：HK 市场 + 打分器
    for mid in ("hibor_1m", "carry_hk_us", "peg_pressure"):
        assert ms[mid]["dataset"] == "hk_rates" and ms[mid]["market"] == "HK"
    assert ms["carry_hk_us"]["scorer"] == {"type": "clip", "arg": -1.0}
    assert ms["hibor_1m"]["scorer"] == {"type": "quant", "arg": None}
    # us_cpi_yoy 为背景指标（无 scorer 不打分）
    assert ms["us_cpi_yoy"]["dim"] is None and ms["us_cpi_yoy"]["scorer"] is None
    # 打分器与 _MACRO_SCORERS 逐条一致
    for mid, sc in F._MACRO_SCORERS.items():
        assert ms[mid]["scorer"] == {"type": sc[0], "arg": sc[1]}, mid


def test_export_beta_signals():
    # T-R4 起 signals 直接引用 factors._BETA_SIGNALS 运行时形状（真源唯一，消除双份字面量）
    r = export_registry_v2()
    sigs = {s["id"]: s for s in r["engines"]["beta"]["signals"]}
    assert set(sigs) == set(BETA_SIGNAL_IDS) == {
        "sig_ma20_break", "sig_high20_touch", "sig_bias_ext", "sig_vol_price_div"}
    assert sigs == {s["id"]: s for s in F._BETA_SIGNALS}
    assert all(s["cooldown"] == "10 交易日" and s["status"] == "active" for s in sigs.values())
    assert sigs["sig_high20_touch"]["type"] == "持有确认"
    assert "不自动卖出" in str(sigs["sig_ma20_break"]["action"])
    assert "{ratio:+.1%}" in sigs["sig_ma20_break"]["detail"]["below"]   # detail 模板运行时格式化
    assert validate_registry(r) == []                                    # 新形状过校验器


# ── 校验器 ────────────────────────────────────────────────────

def test_validate_default_ok():
    assert validate_registry(export_registry_v2()) == []


def _mutated(fn):
    r = export_registry_v2()
    fn(r)
    return r


def test_validate_rejects_bad_entries():
    # 坏 status
    e = validate_registry(_mutated(lambda r: r["engines"]["alpha"]["metrics"][0].update(status="dead")))
    assert any("status" in x for x in e)
    # 坏 scorer.type
    e = validate_registry(_mutated(lambda r: r["engines"]["macro"]["metrics"][1].update(scorer={"type": "zzz", "arg": 1})))
    assert any("scorer" in x for x in e)
    # alpha 维度非法
    e = validate_registry(_mutated(lambda r: r["engines"]["alpha"]["metrics"][0].update(dim="D9")))
    assert any("dim" in x for x in e)
    # macro market 非法
    e = validate_registry(_mutated(lambda r: r["engines"]["macro"]["metrics"][0].update(market="JP")))
    assert any("market" in x for x in e)
    # 重复 id
    e = validate_registry(_mutated(lambda r: r["engines"]["beta"]["metrics"].append(dict(r["engines"]["beta"]["metrics"][0]))))
    assert any("重复" in x for x in e)
    # 缺引擎节
    e = validate_registry(_mutated(lambda r: r["engines"].pop("macro")))
    assert any("alpha/beta/macro" in x for x in e)
    # version 不符
    e = validate_registry(_mutated(lambda r: r.update(version=1)))
    assert any("version" in x for x in e)
    # 未知 beta 信号
    e = validate_registry(_mutated(lambda r: r["engines"]["beta"]["signals"].append({"id": "sig_new"})))
    assert any("signals" in x for x in e)


# ── 加载器（透明降级 + 隔离）──────────────────────────────────


def _write_registry(tmp_path, obj):
    d = tmp_path / "factors"
    d.mkdir(parents=True, exist_ok=True)
    (d / "registry.json").write_text(
        obj if isinstance(obj, str) else json.dumps(obj, ensure_ascii=False),
        encoding="utf-8")


def test_load_fallback_no_file(tmp_path):
    r = load_registry(tmp_path)
    assert r["version"] == 2 and len(r["engines"]["alpha"]["metrics"]) == 19


def test_load_fallback_bad_json(tmp_path):
    _write_registry(tmp_path, "{not json")
    r = load_registry(tmp_path)
    assert len(r["engines"]["macro"]["metrics"]) == 13


def test_load_fallback_v1(tmp_path):
    _write_registry(tmp_path, {"version": 1, "factors": []})
    r = load_registry(tmp_path)
    assert r["version"] == 2 and validate_registry(r) == []


def test_load_valid_file(tmp_path):
    reg = export_registry_v2()
    reg["engines"]["macro"]["metrics"][1]["status"] = "inactive"   # m2_yoy 停用
    _write_registry(tmp_path, reg)
    r = load_registry(tmp_path)
    assert r["engines"]["macro"]["metrics"][1]["status"] == "inactive"


def test_load_valid_but_invalid_content_falls_back(tmp_path):
    reg = export_registry_v2()
    reg["engines"]["alpha"]["metrics"][0]["status"] = "bogus"      # 校验不过
    _write_registry(tmp_path, reg)
    r = load_registry(tmp_path)
    assert r["engines"]["alpha"]["metrics"][0]["status"] == "active"


def test_deepcopy_isolation():
    r1 = export_registry_v2()
    r1["engines"]["alpha"]["dim_weights"]["估值"] = 0.99
    r1["engines"]["macro"]["metrics"][0]["status"] = "inactive"
    r2 = export_registry_v2()
    assert r2["engines"]["alpha"]["dim_weights"]["估值"] == 0.25
    assert r2["engines"]["macro"]["metrics"][0]["status"] == "active"
    # load 返回值同样隔离
    loaded = load_registry("/nonexistent-xyz")
    loaded["engines"]["beta"]["defaults"]["win"] = 999
    assert export_registry_v2()["engines"]["beta"]["defaults"]["win"] == 20
