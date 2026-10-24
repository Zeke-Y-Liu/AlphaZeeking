"""T-R2：引擎包装器测试——包装器与 factors 直调逐位对拍 + 查询面 + 降级。

红线：包装器零公式（一行委托）；portfolio_review 输出契约不变（集成层用例在
test_macro_liquidity.py / test_datasource_merge.py 等处已是安全网）。
"""
import pytest

from applib import factors as F
from applib.engines import (
    ENGINES,
    AlphaEngine,
    BetaEngine,
    MacroEngine,
    get_engine,
    list_engines,
)

# ── 合成数据（最小充分集）─────────────────────────────────────


def _bars(n=300, start="2025-01-01", base=10.0):
    import datetime as dt
    d0 = dt.date.fromisoformat(start)
    out = []
    px = base
    for i in range(n):
        d = (d0 + dt.timedelta(days=i)).isoformat()
        px *= 1 + ((i % 7) - 3) / 500.0        # 温和锯齿走势
        out.append({"dt": d, "open": px, "high": px * 1.01, "low": px * 0.99,
                    "close": px, "volume": 1e6, "amount": 1e7, "pct": ((i % 7) - 3) / 500.0})
    return out


def _reports():
    return [
        {"period": "2025-12-31", "announce_date": "2026-03-30", "eps_ytd": 1.20, "bps": 12.0,
         "margin": 18.0, "rev_g": 12.0, "np_g": 15.0, "total_assets": 100.0, "total_liab": 45.0,
         "ocf": 20.0, "capex": 5.0, "revenue": 80.0, "net_profit": 12.0},
        {"period": "2026-06-30", "announce_date": "2026-08-28", "eps_ytd": 0.70, "bps": 12.4,
         "margin": 19.0, "rev_g": 14.0, "np_g": 17.0, "total_assets": 102.0, "total_liab": 45.5,
         "ocf": 22.0, "capex": 5.0, "revenue": 85.0, "net_profit": 13.0},
    ]


def _dividends():
    return [{"ex_date": "2026-06-15", "dps": 0.5}]


def _macro_series():
    # 覆盖 quant/mom/step/clip/pulse 五种打分器的最小序列
    return {
        "shibor_1m": [{"dt": f"2026-{m:02d}-01", "v": 2.0 + m * 0.01} for m in range(1, 10)],
        "m2_yoy": [{"dt": f"2026-{m:02d}-28", "v": 8.0 + m * 0.05} for m in range(1, 10)],
        "lpr_1y": [{"dt": "2026-01-20", "v": 3.05}, {"dt": "2026-05-20", "v": 2.95}],
        "rrr_big": [{"dt": "2026-02-05", "v": 9.5}, {"dt": "2026-09-06", "v": 9.0}],
        "cn_us_10y": [{"dt": f"2026-{m:02d}-01", "v": 1.6 + m * 0.02} for m in range(1, 10)],
        "us_10y_2y": [{"dt": f"2026-{m:02d}-01", "v": 0.3 - m * 0.01} for m in range(1, 10)],
        "fed_target": [{"dt": "2026-01-29", "v": 4.5}, {"dt": "2026-07-30", "v": 4.25}],
        "us_cpi_surprise": [{"dt": "2026-08-12", "v": 0.1}, {"dt": "2026-09-11", "v": -0.05}],
        "us_cpi_yoy": [{"dt": f"2026-{m:02d}-12", "v": 3.0 - m * 0.02} for m in range(1, 10)],
        "tsf_new_12m": [{"dt": "2026-08-15", "v": 21000.0}, {"dt": "2026-09-15", "v": 21500.0}],
    }


# ── 查询面 ────────────────────────────────────────────────────


def test_get_engine_unknown_raises():
    with pytest.raises(ValueError, match="未知引擎"):
        get_engine("nope")


def test_list_engines_three():
    ds = list_engines()
    assert [d["id"] for d in ds] == ["alpha", "beta", "macro"]
    assert all({"name", "kind", "metric_count", "active_count"} <= set(d) for d in ds)


def test_metrics_counts_and_active():
    assert get_engine("alpha").metrics() and len(get_engine("alpha").metrics()) == 19
    assert len(get_engine("beta").metrics()) == 6
    assert len(get_engine("macro").metrics()) == 13
    for eng in ENGINES.values():
        assert eng.active_ids() == {m.id for m in eng.metrics()}      # 全默认 active
        assert eng.describe()["active_count"] == eng.describe()["metric_count"]
    assert ENGINES["macro"].describe()["dim_weights"] == F.MACRO_DIM_WEIGHTS


def test_metric_spec_fields():
    m = {x.id: x for x in get_engine("macro").metrics()}
    assert m["m2_yoy"].scorer == {"type": "mom", "arg": 0.5}
    assert m["m2_yoy"].dataset == "macro_liquidity" and m["m2_yoy"].market == "A"
    a = {x.id: x for x in get_engine("alpha").metrics()}
    assert a["val_pe_ttm"].dim == "估值" and a["val_pe_ttm"].formula == "builtin:pe_ttm_pit"


# ── 对拍：包装器与 factors 直调逐位一致 ───────────────────────


def test_alpha_parity():
    bars, reps, dvd = _bars(), _reports(), _dividends()
    for kw in ({}, {"as_of": "2026-09-01"}, {"weights": {"估值": .4, "质量": .2, "成长": .2, "动量": .1, "红利低波": .1}}):
        assert AlphaEngine().run("600019.SH", bars, reps, dvd, **kw) == \
            F.compute_alpha("600019.SH", bars, reps, dvd, **kw)


def test_beta_parity():
    bars = _bars(60)
    for kw in ({}, {"as_of": "2026-02-20"}, {"win": 10, "band": 0.05}, {"band_sigma": 2.0, "pct_window": 60}):
        assert BetaEngine().run("600019.SH", bars, **kw) == F.compute_beta("600019.SH", bars, **kw)


def test_hk_regime_parity():
    series = [{"dt": f"2026-{m:02d}-01", "hibor1m": 3.9 - m * 0.02, "fed": 4.5,
               "usdhkd": 7.81 + m * 0.004} for m in range(1, 11)]
    assert BetaEngine().run_hk_regime(series) == F.compute_hk_regime(series)
    assert BetaEngine().run_hk_regime(series, as_of="2026-06-01", chg_win=5) == \
        F.compute_hk_regime(series, as_of="2026-06-01", chg_win=5)


def test_macro_parity():
    series = _macro_series()
    assert MacroEngine().run(series) == F.compute_macro_liquidity(series)
    assert MacroEngine().run(series, as_of="2026-06-30", market_weights={"A": 2, "US": 1}) == \
        F.compute_macro_liquidity(series, as_of="2026-06-30", market_weights={"A": 2, "US": 1})


# ── 注册表降级：坏文件不影响运行 ─────────────────────────────


def test_engine_run_with_broken_registry(tmp_path):
    (tmp_path / "factors").mkdir()
    (tmp_path / "factors" / "registry.json").write_text("{broken", encoding="utf-8")
    eng = BetaEngine().load(tmp_path)      # load 透明降级 → 内置默认
    assert eng.registry["version"] == 2    # 降级为默认而非 None
    bars = _bars(60)
    assert eng.run("600019.SH", bars) == F.compute_beta("600019.SH", bars)


def test_engine_load_reflects_registry_status(tmp_path):
    from applib.engines.base import export_registry_v2
    reg = export_registry_v2()
    reg["engines"]["macro"]["metrics"][0]["status"] = "inactive"   # m2_yoy 停用
    (tmp_path / "factors").mkdir()
    (tmp_path / "factors" / "registry.json").write_text(
        __import__("json").dumps(reg, ensure_ascii=False), encoding="utf-8")
    eng = MacroEngine().load(tmp_path)
    assert "m2_yoy" not in eng.active_ids()
    assert eng.describe()["active_count"] == 12