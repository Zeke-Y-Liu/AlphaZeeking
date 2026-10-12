"""M7 多市场扩展位测试（实施计划 §10 任务 10.3 + DoD：空跑演练一键触发）。"""
import json

import pytest

from factorlib.evaluate import _EVAL_DDLS, build_universe_db
from factorlib.multimarket import (MARKETS, REQUIRED_TABLES, SCHEMA_MAPS,
                                   market_readiness)


def _con():
    con = build_universe_db("alphapocket/remote/repo")
    return con


def test_schema_maps_interface():
    """科目映射接口：统一科目全市场覆盖（缺市场 = 接口缺口，注册时失败）。"""
    assert set(SCHEMA_MAPS) >= {"eps_ytd", "revenue", "net_profit", "ocf",
                                "period", "announce_date"}
    for field, mkt_map in SCHEMA_MAPS.items():
        assert set(mkt_map) == set(MARKETS), f"{field} 缺市场映射"
    # HK/US 科目为准则口径显式命名（非 CN 复制）
    assert SCHEMA_MAPS["net_profit"]["HK"] == "profit_attributable"
    assert SCHEMA_MAPS["net_profit"]["US"] == "net_income"


def test_market_readiness():
    """就绪检查：CN ready（dim_security market=A 归一），HK/US no_source。"""
    con = _con()
    rep = market_readiness(con)
    assert rep["CN"]["status"] == "ready" and rep["CN"]["n_secs"] > 0
    assert rep["HK"]["status"] == "no_source"
    assert rep["US"]["status"] == "no_source"
    assert not rep["CN"]["missing_tables"]


def test_market_readiness_missing_tables():
    """必需表缺失 → missing_tables 状态（降级而不是 crash）。"""
    con = _con()
    con.execute("DROP TABLE dwd_dividend")
    rep = market_readiness(con)
    assert all(r["status"] == "missing_tables" and "dwd_dividend" in r["missing_tables"]
               for r in rep.values())


def test_dry_run_val_pe_ttm_hk(tmp_path):
    """DoD 10.3 空跑演练：val_pe_ttm universe 加 HK → 流程一键触发；
    HK 无源 SKIP 降级登记，CN 覆盖不变，PIT 审计通过。"""
    from factorlib.multimarket import dry_run
    rep = dry_run("alphapocket/remote/repo", markets=("CN", "HK"),
                  factor_ids=("val_pe_ttm",), out_dir=tmp_path)
    f = rep["factors"][0]
    assert f["factor_id"] == "val_pe_ttm" and f["universe"] == ["CN", "HK"]
    assert f["n_rows"] > 0 and f["coverage_by_market"]["CN"] > 0
    assert "HK" not in f["coverage_by_market"]              # HK 无源 → 空覆盖
    assert f["skipped_markets"] == ["HK"]
    assert f["pit_audit"]["pass"] is True
    assert rep["readiness"]["CN"]["status"] == "ready"
    assert rep["readiness"]["HK"]["status"] == "no_source"
    # 落盘（MD + JSON）
    assert (tmp_path / "multimarket_dryrun.md").exists()
    assert json.loads((tmp_path / "multimarket_dryrun.json").read_text())["factors"]
