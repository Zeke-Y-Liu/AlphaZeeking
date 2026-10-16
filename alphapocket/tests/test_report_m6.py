"""M6 T-C5：报告升级 + 三大内置策略映射测试。

覆盖 DoD 必含项：三块渲染快照（年度分布/因子暴露/成本双列数值勾稽）、卡玛边界（回撤0→N/A）、
换手-频率匹配核对、因子暴露方向一致性、双轨缺失显式披露（禁止编 0）、
三份内置 YAML validate 通过 + approx 披露在档、老报告（close 路径）渲染零回归。
运行：``cd alphapocket && python -m pytest tests/test_report_m6.py -q``
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from applib import backtest as BT
from applib.factor_backtest import run_factor_backtest
from factorlib import load_factor, set_status
from factorlib.materialize import materialize

PKG_ROOT = Path(__file__).resolve().parents[1]
STRATEGY_DIR = PKG_ROOT / "sandbox" / "repo" / "strategies"


# ── 造数助手 ─────────────────────────────────────────────
def _mk_bt(series: list, gross: list | None = None, *, factors: list | None = None,
           exposure: list | None = None, cost: dict | None = None,
           four: dict | None = None, rebalance: str = "monthly") -> dict:
    """最小可渲染 bt（factor_spec 在场 → 触发 ⑤-⑧ M6 新节）。"""
    rets = [series[i][1] / series[i - 1][1] - 1 for i in range(1, len(series))]
    bt = {
        "portfolio": "p1", "portfolio_name": "T-C5 测试策略", "strategy": "equal",
        "start": series[0][0], "end": series[-1][0], "n_days": len(rets),
        "n_rebalances": len((cost or {}).get("events") or []), "rf": 0.0,
        "rebalance": rebalance,
        "series": series, "strategy_metrics": BT._metrics(series, rets),
        "benchmark": None, "excess": {"total": None, "cagr": None},
        "monthly": {}, "yearly": {},
        "cost": cost or {"n": 0, "total": 0.0, "avg_turnover": 0.0, "events": []},
        "factor_spec": {"factors": factors or [], "top_n": 3, "universe_n": 10},
    }
    if gross is not None:
        bt["series_gross"] = gross
    if exposure is not None:
        bt["factor_exposure"] = exposure
    if four is not None:
        bt["four_method_summary"] = four
    return bt


def _year_span(n_per_year: int = 24, years: int = 2) -> list:
    """双年日频净值序列（单调上升 + 年末扰动，产生两个年度）。"""
    out = []
    i = 0
    for y in (2021, 2022)[:years]:
        for k in range(n_per_year):
            i += 1
            out.append((f"{y}-{(k % 12) + 1:02d}-{(k % 27) + 1:02d}", 1.0 + 0.01 * i))
    return out


# ── 纯函数：分位 / 年度分布 / 卡玛边界 ────────────────────
def test_quantiles_exact_and_empty():
    """P10/P25/P50/P75/P90 线性插值手工对账；空 → 全 None（不编 0）。"""
    q = BT._quantiles([float(i) for i in range(1, 11)])
    assert q == {"P10": 1.9, "P25": 3.25, "P50": 5.5, "P75": 7.75, "P90": 9.1}
    assert all(v is None for v in BT._quantiles([]).values())


def test_yearly_from_series_cross_year_manual():
    """跨年逐日复利手工对账：2021 段 +10%，2022 段回吐 → 全年 0%。"""
    s = [("2021-12-30", 1.0), ("2021-12-31", 1.1),
         ("2022-01-04", 1.21), ("2022-03-01", 1.1)]
    assert BT._yearly_from_series(s) == {"2021": 0.1, "2022": 0.0}


def test_calmar_zero_drawdown_na():
    """回撤为 0 → 卡玛 N/A（None），其余指标照常（契约：回撤 0 → N/A）。"""
    s = [("2021-01-04", 1.0), ("2021-01-05", 1.01), ("2021-01-06", 1.03),
         ("2021-01-07", 1.02), ("2021-01-08", 1.04)]
    m = BT._metrics(s, [0.01, 0.0198, -0.0097, 0.0196])
    assert m["max_dd"] < 0 and m["calmar"] is not None
    # 严格单调 → 回撤 0 → 卡玛 N/A
    s2 = [("2021-01-04", 1.0), ("2021-01-05", 1.01), ("2021-01-06", 1.02),
          ("2021-01-07", 1.03), ("2021-01-08", 1.04)]
    m2 = BT._metrics(s2, [0.01, 0.0099, 0.0098, 0.0097])
    assert m2["max_dd"] == 0.0
    assert m2["calmar"] is None
    assert m2["sharpe"] is not None and m2["cagr"] > 0


# ── 成本双列（§11 #5 硬纪律）──────────────────────────────
def test_cost_dual_missing_gross_discloses():
    """无 series_gross → gross_available=False + gross=None（缺数据显式披露，不编 0）。"""
    s = _year_span()
    bt = _mk_bt(s)
    cd = BT._cost_dual(bt)
    assert cd["gross_available"] is False
    for k in BT._M6_KEYS:
        assert cd["rows"][k]["gross"] is None
        assert cd["rows"][k]["net"] == bt["strategy_metrics"][k]


def test_cost_dual_with_gross_drag_direction():
    """有双轨 → cagr/calmar gross>net、sharpe gross≥net（费用只拖累不增益）。"""
    s = _year_span()
    net = [(d, v - (0.015 if i % 5 == 0 else 0))          # 压低后回升 → 回撤非零、波动非零
           for i, (d, v) in enumerate(s)]
    gross = [(d, v * 1.0005 ** i) for i, (d, v) in enumerate(net)]   # 比例放大 → 收益更高、回撤更浅
    bt = _mk_bt(net, gross)
    cd = BT._cost_dual(bt)
    assert cd["gross_available"] is True
    assert cd["rows"]["cagr"]["gross"] > cd["rows"]["cagr"]["net"]
    assert cd["rows"]["sharpe"]["gross"] >= cd["rows"]["sharpe"]["net"]
    assert cd["rows"]["calmar"]["gross"] > cd["rows"]["calmar"]["net"]
    assert cd["rows"]["max_dd"]["gross"] > cd["rows"]["max_dd"]["net"]   # 均为负值：gross 回撤更浅
    assert cd["rows"]["sharpe"]["net"] == bt["strategy_metrics"]["sharpe"]


# ── 换手-频率匹配（DoD#4 后半句）─────────────────────────
def test_turnover_table_freq_match_monthly():
    """月频 12 期/年 → 匹配 ✓；期均换手 = Σturnover/n。"""
    events = [{"dt": f"2021-{m:02d}-26", "turnover": 0.2} for m in range(1, 13)]
    bt = _mk_bt(_year_span(years=1), cost={"n": 12, "total": 0.24, "avg_turnover": 0.2,
                                           "events": events})
    tn = BT._turnover_table(bt)
    assert tn["n_periods"] == 12 and tn["expect_per_year"] == 12
    assert tn["periods_per_year"] == pytest.approx(12.0, abs=0.3)
    assert tn["freq_match"] is True
    assert tn["avg_turnover"] == pytest.approx(0.2)


def test_turnover_table_buyhold_no_periods():
    """buyhold 无 events → n_periods=0、均值 None、不判匹配（不编 0）。"""
    bt = _mk_bt(_year_span(), rebalance="buyhold")
    tn = BT._turnover_table(bt)
    assert tn["n_periods"] == 0 and tn["avg_turnover"] is None and tn["freq_match"] is None


# ── 因子暴露方向一致性 ────────────────────────────────────
_F2 = [{"id": "f_asc", "direction": "asc"}, {"id": "f_desc", "direction": "desc"}]


def test_exposure_check_direction_consistency():
    """asc 持仓原始 rank 均值 <0.5 ✓；desc >0.5 ✓；asc 均值 0.9 → 协议违规。"""
    rows = [{"dt": "2021-02-26", "exposure": {"f_asc": 0.2, "f_desc": 0.8}},
            {"dt": "2021-03-31", "exposure": {"f_asc": 0.3, "f_desc": 0.7}}]
    ok = BT._exposure_check(_mk_bt(_year_span(), factors=_F2, exposure=rows))
    assert ok["check"]["f_asc"] == {"mean": 0.25, "direction": "asc", "consistent": True}
    assert ok["check"]["f_desc"] == {"mean": 0.75, "direction": "desc", "consistent": True}
    bad = [dict(r, exposure={"f_asc": 0.9, "f_desc": 0.1}) for r in rows]
    bad_chk = BT._exposure_check(_mk_bt(_year_span(), factors=_F2, exposure=bad))
    assert bad_chk["check"]["f_asc"]["consistent"] is False
    assert bad_chk["check"]["f_desc"]["consistent"] is False


def test_exposure_missing_factor_cell_skipped():
    """某期因子值缺失（None）→ 该期不计入均值；全缺 → 无 check 行（不编 0）。"""
    rows = [{"dt": "2021-02-26", "exposure": {"f_asc": None, "f_desc": 0.8}}]
    chk = BT._exposure_check(_mk_bt(_year_span(), factors=_F2, exposure=rows))
    assert "f_asc" not in chk["check"] and chk["check"]["f_desc"]["consistent"] is True
    assert BT._exposure_check(_mk_bt(_year_span(), factors=_F2)) is None


# ── 渲染快照：缺失披露 / 双列勾稽 / 四法并列 ──────────────
def test_render_md_missing_gross_and_exposure_disclosure():
    """单轨 bt → 双列与暴露节显式标「数据缺失」，禁止静默填 0。"""
    md = BT.render_md(_mk_bt(_year_span()))
    assert "数据缺失（T-C3 未启用双轨" in md
    assert "数据缺失（factor_exposure" in md
    assert "None" not in md


def test_render_md_dual_track_reconciliation():
    """双轨 bt → 年度表差额列 = gross − net（勾稽），含分位行与换手行。"""
    s = _year_span()
    gross = [(d, v * 1.0005 ** i) for i, (d, v) in enumerate(s)]
    bt = _mk_bt(s, gross, factors=_F2,
                exposure=[{"dt": "2021-06-30", "exposure": {"f_asc": 0.2, "f_desc": 0.8}}],
                cost={"n": 1, "total": 0.01, "avg_turnover": 0.3,
                      "events": [{"dt": "2021-01-04", "turnover": 0.3}]})
    md = BT.render_md(bt)
    assert "## ⑤ 年度收益分布" in md and "## ⑥ 因子暴露" in md
    assert "## ⑦ 成本双列与换手" in md
    yd = BT._yearly_dist(bt)
    for r in yd["rows"]:
        assert r["diff"] == pytest.approx(r["gross"] - r["net"], abs=1e-6)
    for y, r in yd["rows"].items() if isinstance(yd["rows"], dict) else []:
        pass
    assert "P50" in md and "期均单边换手" in md
    # 方向一致性行在档
    assert "f_asc（asc）" in md and "f_desc（desc）" in md


def test_render_md_four_method_summary_injected():
    """注入 four_method_summary → ⑧ 四法并列节（终值/夏普/回撤/卡玛 + 闸标记）。"""
    four = {"equal": {"total_return": 0.5, "sharpe": 1.1, "max_dd": -0.2, "calmar": 2.5,
                      "n_degraded_days": 0, "n_frozen_days": 0},
            "lasso": {"total_return": 0.4, "sharpe": 0.9, "max_dd": -0.25, "calmar": 1.6,
                      "n_degraded_days": 3, "n_frozen_days": 1}}
    md = BT.render_md(_mk_bt(_year_span(), four=four))
    assert "## ⑧ 四法并列汇总" in md and "lasso" in md
    assert "3" in md and "1" in md                      # 降级/冻结日
    html = BT.render_html(_mk_bt(_year_span(), four=four), "<style></style>")
    assert "⑨ 四法并列汇总" in html


def test_render_html_m6_sections_present():
    """html 同构三节：⑥年度/⑦暴露/⑧成本双列 + 缺失披露，非法字符不泄漏。"""
    html = BT.render_html(_mk_bt(_year_span()), "<style></style>")
    for h in ("⑥ 年度收益分布", "⑦ 因子暴露", "⑧ 成本双列与换手"):
        assert h in html
    assert "数据缺失" in html and "None" not in html


# ── 老报告渲染零回归（close 路径 cost/per_asset 形态）─────
def test_render_legacy_close_path_regression():
    """close 路径产物（cost.total/avg_turnover + per_asset + missing_bars）照常渲染。"""
    bt = _mk_bt(_year_span(),
                cost={"n": 2, "total": 0.01, "avg_turnover": 0.25,
                      "events": [{"dt": "2021-01-04", "turnover": 0.25, "cost": 0.005}]})
    bt["per_asset"] = [{"secid": "600001.SH", "name": "甲", "total_return": 0.1,
                        "cagr": 0.05, "ann_vol": 0.2, "max_dd": -0.05, "corr": 0.4}]
    bt["data_quality"] = {"missing_bars": {"600001.SH": 3}}
    md = BT.render_md(bt)
    assert "再平衡成本：2 次" in md and "平均单边换手 25.0%" in md
    assert "累计成本拖累 1.00%" in md and "| 甲 `600001.SH` |" in md
    html = BT.render_html(bt, "<style></style>")
    assert "再平衡累计成本拖累 1.00%" in html and "甲" in html


def test_render_vwap_path_disclosures():
    """next_vwap 产物（cost.total_fees + 无 per_asset + fill_basis 降级）→ 披露而非崩溃。"""
    bt = _mk_bt(_year_span(),
                cost={"n": 3, "total_fees": 123.45,
                      "events": [{"dt": "2021-01-04", "turnover": 0.5, "n_orders": 2}]})
    bt["data_quality"] = {"note": "份额制撮合", "fill_basis":
                          {"vwap": 10, "avg4_vwap_out_of_range": 4}}
    md = BT.render_md(bt)
    assert "标的贡献" in md and "数据缺失（next_vwap 撮合路径不产出标的贡献表" in md
    assert "成交价基准分布" in md and "avg4_vwap_out_of_range` 4 笔" in md
    html = BT.render_html(bt, "<style></style>")
    assert "成交价基准" in html and "降级 avg4" in html


# ── 集成：run_factor_backtest 产出 factor_exposure / turnover ──
FIDS = ("val_pe_ttm", "mom_20d_fl")

SPEC_CLOSE = {"id": "rep-strategy", "name": "报告策略",
              "factors": [{"id": "val_pe_ttm", "direction": "asc", "weight": 1.0},
                          {"id": "mom_20d_fl", "direction": "desc", "weight": 1.0}],
              "top_n": 3, "rebalance": "monthly", "benchmark": "510300.SH", "exec": "close"}
CONS_NV = {"t_plus_1": False, "limit_move": "defer", "suspension": "defer", "lot_size": 100}
SPEC_VWAP = {**SPEC_CLOSE, "id": "rep-strategy-vwap", "exec": "next_day_vwap",
             "constraints": CONS_NV, "capital": 1_000_000}


@pytest.fixture
def m5(fl_db, fl_repo):
    db, _ = fl_db
    for fid in FIDS:
        set_status(fl_repo, fid, "active", who="m6-report", why="T-C5 验收",
                   gate={"ok": True, "reasons": []})
        materialize(load_factor(fl_repo, fid), db, mode="full")
    return db, fl_repo


def _add_intraday_range(repo: Path, dirty: str | None = None) -> None:
    """golden 平 OHLC → tmp 副本加日内振幅与量额；dirty 标的 amount×2 → VWAP 越界（兜底触发）。"""
    p = repo / "market" / "daily_bars.json"
    bars = json.loads(p.read_text(encoding="utf-8"))
    for s, rows in bars.items():
        for r in rows:
            c = r["close"]
            r["open"] = round(c * 0.99, 4)
            r["high"] = round(c * 1.01, 4)
            r["low"] = round(c * 0.98, 4)
            mult = 2.0 if (dirty == "ALL" or s == dirty) else 1.0
            r["volume"], r["amount"] = 10000, c * 10000 * mult
    p.write_text(json.dumps(bars, ensure_ascii=False), encoding="utf-8")


def test_factor_exposure_fields_and_direction(m5):
    """factor_exposure 与 targets 期对齐；asc 暴露均值 <0.5、desc >0.5（方向一致性）。"""
    db, repo = m5
    bt = run_factor_backtest(db, repo, SPEC_CLOSE, start="2021-02-01")
    exp = bt["factor_exposure"]
    assert exp and [r["dt"] for r in exp] == [t["dt"] for t in bt["factor_targets"]]
    for r in exp:
        assert set(r["exposure"]) == {"val_pe_ttm", "mom_20d_fl"}
        assert all(v is None or 0.0 <= v <= 1.0 for v in r["exposure"].values())
    mv = [r["exposure"]["val_pe_ttm"] for r in exp if r["exposure"]["val_pe_ttm"] is not None]
    mm = [r["exposure"]["mom_20d_fl"] for r in exp if r["exposure"]["mom_20d_fl"] is not None]
    assert mv and mm
    assert sum(mv) / len(mv) < 0.5          # asc：低 PE 优先 → 原始 rank 低
    assert sum(mm) / len(mm) > 0.5          # desc：高动量优先 → 原始 rank 高
    chk = BT._exposure_check(bt)
    assert all(c["consistent"] for c in chk["check"].values())


def test_turnover_field_alignment_and_bounds(m5):
    """turnover 与 cost.events 逐期对齐（close 路径）、每期 ∈ [0,1]、期数 = 再平衡次数。"""
    db, repo = m5
    bt = run_factor_backtest(db, repo, SPEC_CLOSE, start="2021-02-01")
    ev = bt["cost"]["events"]
    assert bt["turnover"] == [{"dt": e["dt"], "turnover": e["turnover"]} for e in ev]
    assert len(bt["turnover"]) == bt["n_rebalances"] > 0
    assert all(0.0 <= t["turnover"] <= 1.0 for t in bt["turnover"])


def test_fill_price_fallback_out_of_range_unit():
    """纯单测：VWAP 越出当日 [low,high] → 降级 avg4（basis 标注）；干净行仍用真 VWAP。"""
    row = {"open": 9.8, "high": 10.1, "low": 9.6, "close": 10.0,
           "volume": 10000, "amount": 200000.0}            # VWAP=20 > high=10.1
    px, basis = BT._fill_price(row)
    assert basis == "avg4_vwap_out_of_range"
    assert px == pytest.approx((9.8 + 10.1 + 9.6 + 10.0) / 4)
    px2, b2 = BT._fill_price({**row, "amount": 100000.0})  # VWAP=10 ∈ [9.6, 10.1]
    assert b2 == "vwap" and px2 == pytest.approx(10.0)
    px3, b3 = BT._fill_price({**row, "amount": None})      # 缺 amount → avg4 既有兜底
    assert b3 == "avg4" and px3 == pytest.approx((9.8 + 10.1 + 9.6 + 10.0) / 4)


def test_vwap_fill_fallback_and_no_lookahead_pass(m5):
    """全体脏 amount（VWAP 越界）→ 全量降级 avg4：no_lookahead pass + basis 如实计数。"""
    db, repo = m5
    _add_intraday_range(repo, dirty="ALL")
    bt = run_factor_backtest(db, repo, SPEC_VWAP, start="2021-02-01")
    assert bt["no_lookahead"]["check"] == "pass"
    fb = bt["data_quality"]["fill_basis"]
    assert fb.get("avg4_vwap_out_of_range", 0) >= 1 and "vwap" not in fb
    assert bt["turnover"] == [{"dt": e["dt"], "turnover": e["turnover"]}
                              for e in bt["cost"]["events"]]
    chk = BT._exposure_check(bt)
    assert chk and all(c["consistent"] for c in chk["check"].values())


def test_vwap_clean_fill_all_vwap(m5):
    """干净 amount（VWAP=close ∈ [low,high]）→ 全量真 VWAP，零降级。"""
    db, repo = m5
    _add_intraday_range(repo)
    bt = run_factor_backtest(db, repo, SPEC_VWAP, start="2021-02-01")
    assert bt["no_lookahead"]["check"] == "pass"
    fb = bt["data_quality"]["fill_basis"]
    assert fb.get("vwap", 0) >= 1 and "avg4_vwap_out_of_range" not in fb


# ── 三大内置策略 YAML（池准入 + approx 披露在档）──────────
def _pkg_yaml(fid: str) -> Path:
    import factorlib.registry as R
    return sorted((Path(R.__file__).parent / "defs").rglob(f"{fid}.yaml"))[0]


def _activate(repo: Path, fid: str) -> None:
    p = repo / "factors" / "factor_registry_state.json"
    st = json.loads(p.read_text(encoding="utf-8")) if p.exists() else {}
    st[fid] = {"status": "active", "version": "1.0.0", "history": []}
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(st, ensure_ascii=False, indent=2), encoding="utf-8")


@pytest.fixture
def pool_repo(tmp_path) -> Path:
    """池内 3 因子（val_pe_ttm/mom_20d_fl/sig_mom20_rank_fl）active 的 repo。"""
    repo = tmp_path / "repo"
    d = repo / "factors" / "defs"
    d.mkdir(parents=True, exist_ok=True)
    for fid in ("val_pe_ttm", "mom_20d_fl", "sig_mom20_rank_fl"):
        base = _pkg_yaml(fid).read_text(encoding="utf-8")
        (d / f"{fid}.yaml").write_text(base, encoding="utf-8")
        _activate(repo, fid)
    return repo


def test_three_builtin_yaml_validate(pool_repo, tmp_path):
    """三份内置 YAML 全过 load_strategy（池准入）；四法 / top_n / 撮合口径在档。
    T-F9 演练 YAML（hk-dryrun-pe-ttm.yaml）排除：归 tests/test_multimarket_m7.py 管。"""
    from applib.strategy import load_strategy
    files = [p for p in sorted(STRATEGY_DIR.glob("*.yaml"))
             if p.name != "hk-dryrun-pe-ttm.yaml"]
    assert len(files) == 6, f"存量六份 YAML 应在档：{[p.name for p in files]}"
    for y in files:
        spec = load_strategy(y, pool_repo, tmp_path / "fl.duckdb")
        assert spec["methods"] == ["equal", "ic_12m", "ic_ir", "lasso"]
        assert spec["top_n"] == 3 and spec["exec"] == "next_day_vwap"
        assert spec["rebalance"] == "monthly"
        pool = {"val_pe_ttm", "mom_20d_fl", "sig_mom20_rank_fl"}
        assert {f["id"] for f in spec["factors"]} <= pool     # 红线：只准池内因子


def test_builtin_yaml_approx_disclosure_in_file():
    """v1 三份近似标注在档；M7 T-F7 升版 v2 三份（同目录演进，v1 归档冻结）：
    v2 缺腿/缺族披露在档、因子仍只准池内（上一测试统一校验）。"""
    texts = {p.name: p.read_text(encoding="utf-8") for p in sorted(STRATEGY_DIR.glob("*.yaml"))}
    assert set(texts) == {"lowvol-dividend-approx-v1.yaml", "pe-roe-approx-v1.yaml",
                          "multi-resonance-v1.yaml",
                          # M7 T-F7：v2 升版三份（lockbox 2022-01 恢复；v1 冻结不重跑）
                          "lowvol-dividend-v2.yaml", "pe-roe-v2.yaml", "multi-resonance-v2.yaml",
                          # M7 T-F9：多市场空跑演练策略（markets=["CN","HK"]，归 multimarket 测试管）
                          "hk-dryrun-pe-ttm.yaml"}
    for name in ("lowvol-dividend-approx-v1.yaml", "pe-roe-approx-v1.yaml"):
        assert "近似版" in texts[name] and "M7" in texts[name]
    assert "low_vol_252d" in texts["lowvol-dividend-approx-v1.yaml"]
    assert "qua_roe_ttm" in texts["pe-roe-approx-v1.yaml"]
    assert "active_pool" in texts["multi-resonance-v1.yaml"]      # 池内全量披露
    for name in ("lowvol-dividend-v2.yaml", "pe-roe-v2.yaml"):
        assert "门禁未过" in texts[name] and "披露" in texts[name]   # 缺腿披露
    assert "缺族披露" in texts["multi-resonance-v2.yaml"]           # 缺族披露
    assert "2022-01" in texts["lowvol-dividend-v2.yaml"]            # lockbox 计划口径核算在档
