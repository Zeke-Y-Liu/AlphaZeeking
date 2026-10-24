"""M1 注册中心测试：YAML 真源加载、字段校验、状态机单向迁移、依赖解析、回测起点。

运行：``cd alphapocket && python -m pytest tests/test_factorlib_registry.py -q``
"""
from __future__ import annotations

import json
from datetime import date
from pathlib import Path

import pytest
import yaml

import factorlib.registry as R
from factorlib.registry import (
    CATEGORIES,
    ENGINES,
    FactorMeta,
    RegistryError,
    load_factor,
    load_registry,
    resolve_backtest_start,
    resolve_dependencies,
    set_status,
    validate,
)

_IDS = ("val_pe_ttm", "val_pb_lf", "val_ps_ttm", "val_ev_ebitda", "val_div_yield",
        "mom_20d_fl", "sig_mom20_rank_fl", "eps_revise_fl",
        "qua_roe_ttm", "qua_gross_margin", "qua_accruals",
        "qua_ocf_to_np", "qua_debt_to_asset",
        "gro_rev_yoy", "gro_np_yoy", "gro_rev_cagr_3y", "gro_stability",
        # M7 T-F5：动量+低波红利族 6 因子
        "mom_12_1", "mom_6_1", "mom_industry_adj",
        "low_vol_252d", "low_beta", "div_payout_streak")


def _pkg_yaml(fid: str) -> Path:
    return min((Path(R.__file__).parent / "defs").rglob(f"{fid}.yaml"))


# ── YAML 真源加载 ─────────────────────────────────────
def test_load_registry_from_package_defs(tmp_path):
    """repo 无 defs 目录 → 回落包内 defs；4 因子唯一且校验通过。"""
    metas = load_registry(tmp_path)
    assert [m.id for m in metas] == sorted(_IDS)
    for m in metas:
        assert m.engine in ENGINES and m.category in CATEGORIES
        assert m.pit is True and m.status == "draft"
        assert m.source_path and m.source_path.endswith(".yaml")


def test_load_factor_merges_state_file(tmp_path):
    set_status(tmp_path, "val_pe_ttm", "active", who="tester", why="golden 通过",
               gate={"ok": True, "reasons": []})                    # M5：draft→active 须门禁结论
    assert load_factor(tmp_path, "val_pe_ttm").status == "active"     # 状态文件覆盖 YAML
    assert load_factor(tmp_path, "mom_20d_fl").status == "draft"      # 未迁移因子不受影响


def test_load_factor_missing_raises(tmp_path):
    with pytest.raises(RegistryError, match="不存在"):
        load_factor(tmp_path, "no_such_factor")


def test_duplicate_id_rejected(tmp_path):
    d = tmp_path / "factors" / "defs"
    d.mkdir(parents=True)
    text = _pkg_yaml("val_pe_ttm").read_text(encoding="utf-8")
    (d / "a.yaml").write_text(text, encoding="utf-8")
    (d / "b.yaml").write_text(text, encoding="utf-8")
    with pytest.raises(RegistryError, match="重复"):
        load_registry(tmp_path)


def test_yaml_missing_required_field_rejected(tmp_path):
    d = tmp_path / "factors" / "defs"
    d.mkdir(parents=True)
    base = yaml.safe_load(_pkg_yaml("val_pe_ttm").read_text(encoding="utf-8"))
    del base["pit"]                                    # 非法：缺 PIT 声明
    (d / "bad.yaml").write_text(yaml.dump(base), encoding="utf-8")
    with pytest.raises(RegistryError, match="缺少必填字段"):
        load_registry(tmp_path)


# ── 字段校验（validate 违例列表）────────────────────────
def _valid_meta(**kw) -> FactorMeta:
    base = {"id": "f2_val_x", "name": "x", "category": "F2_fundamental", "factor_type": "alpha",
                "engine": "sql", "direction": "low", "universe": ["CN"], "inputs": ["fl_daily_bar"],
                "lookback_days": 0, "pit": True, "version": "1.0.0", "status": "draft",
                "description": "d", "sql": "SELECT 1"}
    base.update(kw)
    return FactorMeta(**base)


def test_validate_clean_meta():
    assert validate(_valid_meta()) == []


def test_validate_rejects_non_pit():
    assert any("pit" in e for e in validate(_valid_meta(pit=False)))


def test_validate_rejects_bad_enum_fields():
    for field, bad in (("category", "F9_x"), ("engine", "excel"), ("direction", "up"),
                       ("status", "archived"), ("factor_type", "beta")):
        assert len(validate(_valid_meta(**{field: bad}))) >= 1, field


def test_validate_rejects_bad_id_and_inputs():
    assert any("命名" in e for e in validate(_valid_meta(id="Val-PE!")))
    assert any("inputs" in e for e in validate(_valid_meta(inputs=[])))
    assert any("lookback" in e for e in validate(_valid_meta(lookback_days=-1)))


def test_validate_engine_body_required():
    assert any("sql" in e for e in validate(_valid_meta(engine="sql", sql="")))
    assert any("udf" in e for e in validate(_valid_meta(engine="python_udf")))
    assert any("expr" in e for e in validate(_valid_meta(engine="qlib_expr")))


def test_validate_signal_horizon_window():
    assert validate(_valid_meta(factor_type="signal", horizon_days=20)) == []
    for h in (5, 31, None):
        assert any("horizon" in e
                   for e in validate(_valid_meta(factor_type="signal", horizon_days=h)))


# ── 状态机（draft → active → deprecated 单向）──────────────
def test_state_machine_happy_path(tmp_path):
    r1 = set_status(tmp_path, "val_pe_ttm", "active", who="t1", why="上线",
                    gate={"ok": True, "reasons": []})
    assert (r1["from"], r1["to"]) == ("draft", "active")
    r2 = set_status(tmp_path, "val_pe_ttm", "deprecated", who="t2", why="退役")
    assert (r2["from"], r2["to"]) == ("active", "deprecated")
    st = json.loads((tmp_path / "factors" / "factor_registry_state.json").read_text(encoding="utf-8"))
    hist = st["val_pe_ttm"]["history"]
    assert [h["to"] for h in hist] == ["active", "deprecated"]
    assert hist[0]["who"] == "t1" and hist[1]["why"] == "退役"


def test_state_machine_illegal_transitions(tmp_path):
    with pytest.raises(RegistryError, match="非法迁移"):
        set_status(tmp_path, "val_pe_ttm", "deprecated")          # draft → deprecated 跳级
    set_status(tmp_path, "val_pe_ttm", "active", gate={"ok": True, "reasons": []})
    with pytest.raises(RegistryError, match="非法迁移"):
        set_status(tmp_path, "val_pe_ttm", "draft")               # 回退被拒
    with pytest.raises(RegistryError, match="已处于"):
        set_status(tmp_path, "val_pe_ttm", "active")              # 同态无迁移
    with pytest.raises(RegistryError, match="目标状态非法"):
        set_status(tmp_path, "val_pe_ttm", "zombie")


# ── 依赖解析与回测起点 ─────────────────────────────────
def test_resolve_dependencies(fl_db):
    db, _ = fl_db
    con_ok = _valid_meta(inputs=["fl_daily_bar", "fl_dws_financial_ttm"])
    con_missing = _valid_meta(inputs=["fl_daily_bar", "fl_no_such"])
    import duckdb
    con = duckdb.connect(str(db), read_only=True)
    try:
        assert resolve_dependencies(con_ok, con) == []
        assert resolve_dependencies(con_missing, con) == ["fl_no_such"]
    finally:
        con.close()


def test_resolve_backtest_start(fl_db):
    """起点 = max(各因子 lookback 满足日)：mom/sig lookback 20 → 2021-01-04 + 20 = 2021-01-24。"""
    db, _ = fl_db
    import duckdb
    con = duckdb.connect(str(db), read_only=True)
    try:
        assert resolve_backtest_start(con, metas_of()) == date(2021, 1, 24)
    finally:
        con.close()


def metas_of():
    return [
        FactorMeta(id="val_pe_ttm", name="x", category="F2_fundamental", factor_type="alpha",
                   engine="sql", direction="low", universe=["CN"],
                   inputs=["fl_daily_bar", "fl_dws_financial_ttm"], lookback_days=0,
                   pit=True, version="1", status="draft", description="d", sql="s"),
        FactorMeta(id="mom_20d_fl", name="x", category="F1_technical", factor_type="alpha",
                   engine="qlib_expr", direction="high", universe=["CN"],
                   inputs=["fl_daily_bar"], lookback_days=20,
                   pit=True, version="1", status="draft", description="d", expr="e"),
        FactorMeta(id="eps_revise_fl", name="x", category="F2_fundamental", factor_type="alpha",
                   engine="python_udf", direction="high", universe=["CN"],
                   inputs=["fl_fin_report"], lookback_days=0,
                   pit=True, version="1", status="draft", description="d", udf="u"),
    ]
