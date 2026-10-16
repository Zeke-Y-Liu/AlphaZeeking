"""M7 T-F2：估值族 4 因子测试——TTM 累计口径 / ASOF 市值 / PIT 红线 / 缺口语义 / 假设绑定。

三层（与 test_data_ext_m7 同模式）：
- 合成 fixture（golden repo 注入市值/分红/扩展科目 → 重 prepare → 物化 → query 对账）；
  golden 算术独立手工核对（600001.SH equity=5e9、ttm_eps(2021Q1)=0.35+1.0−0.25=1.1 等）；
- 包内 defs 静态契约：4 估值因子 hypothesis 绑定 ↔ sandbox/repo hypothesis/registry；
- 真实库留证（T-F2 物化产物，文件缺失 → skip）：量级/量纲/覆盖披露/检验与门禁在档。

运行：``cd alphapocket && python -m pytest tests/test_factor_val_m7.py -q``
"""
from __future__ import annotations

import json
from pathlib import Path

import duckdb
import pytest

from factorlib.materialize import materialize, query
from factorlib.registry import load_factor

ROOT = Path(__file__).resolve().parents[1]
REAL_REPO = ROOT / "remote" / "repo"
REAL_DB = ROOT / "sandbox" / "data" / "fl.duckdb"
SB_REGISTRY = ROOT / "sandbox" / "repo" / "hypothesis" / "registry.json"

VAL4 = ("val_pb_lf", "val_ps_ttm", "val_ev_ebitda", "val_div_yield")
_HYPS = {"val_pb_lf": "H-0007", "val_ps_ttm": "H-0008",
         "val_ev_ebitda": "H-0009", "val_div_yield": "H-0010"}


# ── 注入 helpers（golden repo 可变副本上操作）─────────────────────────
def _write_market_cap(fl_repo: Path, mc: dict) -> None:
    p = fl_repo / "dim" / "market_cap.json"
    p.write_text(json.dumps(mc, ensure_ascii=False), encoding="utf-8")


def _edit_fund(fl_repo: Path, fn) -> None:
    """fn(secid -> dict) 原地修改 fundamental_values.json。"""
    fp = fl_repo / "factors" / "fundamental_values.json"
    fund = json.loads(fp.read_text(encoding="utf-8"))
    fn(fund)
    fp.write_text(json.dumps(fund, ensure_ascii=False), encoding="utf-8")


def _prep(fl_repo: Path, tmp_path: Path):
    """注入后重跑 prepare，返回新 db 路径。"""
    from factorlib.prepare import prepare
    db = tmp_path / "fl_val.duckdb"
    prepare(fl_repo, db)
    return db


def _pit(db: Path, fid: str, dt: str, secid: str):
    """PIT 元数据（used_period/used_announce）——物化层拆表 fl_factor_pit_meta。"""
    con = duckdb.connect(str(db), read_only=True)
    try:
        return con.execute("select used_period, used_announce from fl_factor_pit_meta "
                           "where factor_id = ? and dt = ? and secid = ?",
                           [fid, dt, secid]).fetchone()
    finally:
        con.close()


def _mat(db: Path, fl_repo: Path, *fids):
    """full 物化（defs 回落包内 factorlib/defs；fl_repo 仅用于 defs 目录解析）。"""
    out = {}
    for fid in fids:
        meta = load_factor(fl_repo, fid)
        out[fid] = materialize(meta, db, mode="full")
    return out


# ── 一、fl_market_cap 落表 + P/B（LF）─────────────────────────────────
def test_prepare_market_cap_table(fl_repo, tmp_path):
    """dim/market_cap.json → fl_market_cap 落表（周频快照原值，亿元）。"""
    _write_market_cap(fl_repo, {"600001.SH": [["2021-01-08", 50.0], ["2021-04-25", 60.0]],
                                "600002.SH": [["2021-01-08", 40.0]]})
    db = _prep(fl_repo, tmp_path)
    con = duckdb.connect(str(db), read_only=True)
    try:
        rows = con.execute("select secid, dt, mktcap_yi from fl_market_cap "
                           "order by secid, dt").fetchall()
    finally:
        con.close()
    assert [(r[0], str(r[1]), r[2]) for r in rows] == [
        ("600001.SH", "2021-01-08", 50.0), ("600001.SH", "2021-04-25", 60.0),
        ("600002.SH", "2021-01-08", 40.0)]


def test_val_pb_lf_golden_value(fl_repo, tmp_path):
    """P/B = 市值×1e8 /（assets−liab）；ASOF 取 ≤dt 最新市值点；QUALIFY 最新报告期。

    手工核对：600001.SH 2021-04-30，市值 ASOF=60 亿=6e9，equity(2021Q1)=1e10−5e9=5e9
    → P/B = 6e9/5e9 = 1.2，used_period=2021-03-31（Q1 公告日 2021-04-30 ≤ dt）。
    """
    _write_market_cap(fl_repo, {"600001.SH": [["2021-01-08", 50.0], ["2021-04-25", 60.0]]})
    db = _prep(fl_repo, tmp_path)
    _mat(db, fl_repo, "val_pb_lf")
    hits = query(db, "val_pb_lf", dt="2021-04-30", secid="600001.SH")
    assert len(hits) == 1
    assert hits[0]["value"] == pytest.approx(1.2, rel=1e-9)
    assert str(_pit(db, "val_pb_lf", "2021-04-30", "600001.SH")[0]) == "2021-03-31"
    assert str(_pit(db, "val_pb_lf", "2021-04-30", "600001.SH")[1]) == "2021-04-30"
    # ASOF：2021-01-11（50 亿点后首个交易日）→ P/B = 5e9/5e9 = 1.0
    early = query(db, "val_pb_lf", dt="2021-01-11", secid="600001.SH")
    assert early and early[0]["value"] == pytest.approx(1.0, rel=1e-9)


def test_val_pb_lf_pit_announce_tamper(fl_repo, tmp_path):
    """PIT 红线（篡改对象）：Q1 公告日改到未来 → 截面回退 2020FY 报告期，不见 Q1。"""
    _write_market_cap(fl_repo, {"600001.SH": [["2021-04-25", 60.0]]})

    def tamper(fund):
        for r in fund["600001.SH"]["reports"]:
            if r["period"] == "2021-03-31":
                r["announce_date"] = "2021-05-10"
    _edit_fund(fl_repo, tamper)
    db = _prep(fl_repo, tmp_path)
    _mat(db, fl_repo, "val_pb_lf")
    hits = query(db, "val_pb_lf", dt="2021-04-30", secid="600001.SH")
    assert len(hits) == 1
    pit = _pit(db, "val_pb_lf", "2021-04-30", "600001.SH")
    assert str(pit[0]) == "2020-12-31"                      # 未见 Q1（公告在 05-10）
    assert str(pit[1]) == "2021-03-31"
    # 公告日之后立即可见
    later = query(db, "val_pb_lf", dt="2021-05-28", secid="600001.SH")
    assert later and str(_pit(db, "val_pb_lf", "2021-05-28", "600001.SH")[0]) == "2021-03-31"


def test_val_pb_lf_negative_equity_absent(fl_repo, tmp_path):
    """资不抵债（净资产 ≤0）→ P/B 无意义，NULL 不落行；此前期间不受影响。"""
    _write_market_cap(fl_repo, {"600002.SH": [["2021-04-25", 40.0]]})

    def insolvent(fund):
        fund["600002.SH"]["reports"].append(
            {"period": "2021-03-31", "announce_date": "2021-04-30",
             "eps_ytd": 0.2, "net_profit": 2e6, "revenue": 2e8,
             "total_assets": 1e9, "total_liab": 2e9,        # equity = −1e9
             "ocf": 1e5, "capex": 5e4})
    _edit_fund(fl_repo, insolvent)
    db = _prep(fl_repo, tmp_path)
    _mat(db, fl_repo, "val_pb_lf")
    assert query(db, "val_pb_lf", dt="2021-04-30", secid="600002.SH") == []
    # 2021-04-29（Q1 未公告）仍用 2020FY：equity = 5e9 → P/B = 4e9/5e9 = 0.8
    early = query(db, "val_pb_lf", dt="2021-04-29", secid="600002.SH")
    assert early and early[0]["value"] == pytest.approx(0.8, rel=1e-9)


# ── 二、TTM 累计口径 ──────────────────────────────────────────────────
def test_ttm_cumulative_math(fl_db):
    """累计口径 TTM = YTD + FY(Y−1) − YTD(同期上年)；数据完整时与 4 期求和等价。

    600001.SH：ttm_eps(2020Q3) = 0.75 + 1.0(2019FY) − 0.75(2019Q3) = 1.0
              ttm_eps(2021Q1) = 0.35 + 1.0(2020FY) − 0.25(2020Q1) = 1.1
    """
    db, _ = fl_db
    con = duckdb.connect(str(db), read_only=True)
    try:
        rows = {str(p): v for p, v in con.execute(
            "select period, ttm_eps from fl_dws_financial_ttm "
            "where secid = '600001.SH'").fetchall()}
    finally:
        con.close()
    assert rows["2020-09-30"] == pytest.approx(1.0)
    assert rows["2020-12-31"] == pytest.approx(1.0)          # 12 月期直接 YTD
    assert rows["2021-03-31"] == pytest.approx(1.1)


def test_ttm_missing_dependency_null(fl_repo, tmp_path):
    """依赖项缺失 → 该列 NULL（不 mock 不填 0）：删 2019FY → 2020 全年 TTM 断链。"""
    def drop_fy2019(fund):
        fund["600001.SH"]["reports"] = [
            r for r in fund["600001.SH"]["reports"] if r["period"] != "2019-12-31"]
    _edit_fund(fl_repo, drop_fy2019)
    db = _prep(fl_repo, tmp_path)
    con = duckdb.connect(str(db), read_only=True)
    try:
        rows = {str(p): v for p, v in con.execute(
            "select period, ttm_eps from fl_dws_financial_ttm "
            "where secid = '600001.SH'").fetchall()}
    finally:
        con.close()
    assert "2019-12-31" not in rows                          # 报告行已删 → TTM 行不存在
    assert rows["2020-12-31"] == pytest.approx(1.0)          # 12 月期 YTD 不依赖上年
    assert rows["2020-09-30"] is None                        # 缺 2019FY → NULL
    assert rows["2021-03-31"] == pytest.approx(1.1)     # 依赖 2020FY/2020Q1（仍在）
    # 消费端（PE）在 2021-01-29 回退后无可算期 → 无行
    _mat(db, fl_repo, "val_pe_ttm")
    assert query(db, "val_pe_ttm", dt="2021-01-29", secid="600001.SH") == []


# ── 三、P/S、EV/EBITDA ───────────────────────────────────────────────
def test_val_ps_ttm_value(fl_repo, tmp_path):
    """P/S = 市值×1e8 / ttm_rev。

    600001.SH 2021-04-30：市值 60 亿；ttm_rev(2021Q1) = 3.5e8 + 1e9 − 2.5e8 = 1.1e9
    → P/S = 6e9/1.1e9 ≈ 5.4545。
    """
    _write_market_cap(fl_repo, {"600001.SH": [["2021-04-25", 60.0]]})
    db = _prep(fl_repo, tmp_path)
    _mat(db, fl_repo, "val_ps_ttm")
    hits = query(db, "val_ps_ttm", dt="2021-04-30", secid="600001.SH")
    assert len(hits) == 1
    assert hits[0]["value"] == pytest.approx(6e9 / 1.1e9, rel=1e-9)


def test_val_ps_ttm_nonpositive_revenue_absent(fl_repo, tmp_path):
    """TTM 营收 ≤0 → P/S 无意义 → 不落行（负 YTD ≠ 负 TTM：注入额须使 TTM 转负）。"""
    _write_market_cap(fl_repo, {"600002.SH": [["2021-04-25", 40.0]]})

    def neg_rev(fund):
        for r in fund["600002.SH"]["reports"]:
            if r["period"] == "2021-03-31":
                r["revenue"] = -1.5e9          # ttm_rev = −15e8 + 10e8 − 2.5e8 = −7.5e8
    _edit_fund(fl_repo, neg_rev)
    db = _prep(fl_repo, tmp_path)
    _mat(db, fl_repo, "val_ps_ttm")
    assert query(db, "val_ps_ttm", dt="2021-04-30", secid="600002.SH") == []
    early = query(db, "val_ps_ttm", dt="2021-04-29", secid="600002.SH")
    assert early and early[0]["value"] > 0                   # Q1 前仍用 2020FY


def test_val_ev_ebitda_golden_value(fl_repo, tmp_path):
    """EV/EBITDA 手工算术：EV = 60e8 + 15e8 − 10e8 = 65e8；
    EBITDA = ttm_ebit + ttm_dep = (1e8 + 4e8 − 1e8) + (5e7 + 2e8 − 5e7) = 4e8 + 2e8 = 6e8
    → 65e8/6e8 = 10.8333…
    """
    _write_market_cap(fl_repo, {"600001.SH": [["2021-04-25", 60.0]]})

    def inject(fund):
        for r in fund["600001.SH"]["reports"]:
            p = r["period"]
            r["short_borrow"] = 5e8
            r["long_borrow"] = 1e9
            r["bonds_payable"] = 0.0
            r["current_mature_debt"] = 0.0
            r["cash"] = 1e9
            if p == "2020-12-31":
                r["ebit"], r["dep_amort"] = 4e8, 2e8
            elif p == "2020-03-31":
                r["ebit"], r["dep_amort"] = 1e8, 5e7
            elif p == "2021-03-31":
                r["ebit"], r["dep_amort"] = 1e8, 5e7
            # 其余期（2019Q3/2019FY/2020H1/2020Q3）不注入 → ebit/dep NULL（如实）
    _edit_fund(fl_repo, inject)
    db = _prep(fl_repo, tmp_path)
    _mat(db, fl_repo, "val_ev_ebitda")
    hits = query(db, "val_ev_ebitda", dt="2021-04-30", secid="600001.SH")
    assert len(hits) == 1
    assert hits[0]["value"] == pytest.approx(65e8 / 6e8, rel=1e-9)
    assert str(_pit(db, "val_ev_ebitda", "2021-04-30", "600001.SH")[0]) == "2021-03-31"


def test_val_ev_ebitda_dep_gap_fallback(fl_repo, tmp_path):
    """Q1/Q3 折旧全缺（东财真实属性）→ Q1 窗口回退 FY 期 EBITDA（跨报告期回退证据）。

    只注入 2020FY 的 ebit/dep → ttm_ebit(2020FY)=4e8（12 月直接 YTD）、ttm_dep=2e8；
    2021Q1 期 ttm_ebit/ttm_dep = NULL → 2021-04-30 截面 tt 链回退 2020FY（announce 2021-03-31）。
    """
    _write_market_cap(fl_repo, {"600001.SH": [["2021-04-25", 60.0]]})

    def inject(fund):
        for r in fund["600001.SH"]["reports"]:
            r["short_borrow"], r["long_borrow"] = 5e8, 1e9
            r["bonds_payable"], r["current_mature_debt"], r["cash"] = 0.0, 0.0, 1e9
            if r["period"] == "2020-12-31":
                r["ebit"], r["dep_amort"] = 4e8, 2e8
    _edit_fund(fl_repo, inject)
    db = _prep(fl_repo, tmp_path)
    _mat(db, fl_repo, "val_ev_ebitda")
    hits = query(db, "val_ev_ebitda", dt="2021-04-30", secid="600001.SH")
    assert len(hits) == 1
    assert str(_pit(db, "val_ev_ebitda", "2021-04-30", "600001.SH")[0]) == "2020-12-31"
    # EV = 65e8（bs 链仍用最新 2021Q1 债务/现金），EBITDA = 6e8 → 65/6
    assert hits[0]["value"] == pytest.approx(65e8 / 6e8, rel=1e-9)


# ── 四、股息率 ────────────────────────────────────────────────────────
def test_val_div_yield_pit_and_window(fl_repo, tmp_path):
    """PIT + 12 月窗口：窗口内已公告计入；窗口外/未公告/缺公告日不计入。

    600001.SH close(2021-04-30)=12.0：ex 2021-03-01 announce 2021-02-20 dps 0.5
    → yield = 0.5/12 ≈ 0.0416667。
    """
    def inject(fund):
        fund["600001.SH"]["dividends"] = [
            {"ex_date": "2021-03-01", "announce": "2021-02-20", "dps": 0.5},   # 计入
            {"ex_date": "2020-04-10", "announce": "2020-04-01", "dps": 9.0},   # 窗口外
            {"ex_date": "2021-04-25", "announce": "2021-05-01", "dps": 3.0},   # 未公告
            {"ex_date": "2021-02-10", "announce": None, "dps": 3.0},           # 缺公告日
        ]
    _edit_fund(fl_repo, inject)
    db = _prep(fl_repo, tmp_path)
    _mat(db, fl_repo, "val_div_yield")
    hits = query(db, "val_div_yield", dt="2021-04-30", secid="600001.SH")
    assert len(hits) == 1
    assert hits[0]["value"] == pytest.approx(0.5 / 12.0, rel=1e-9)
    assert _pit(db, "val_div_yield", "2021-04-30", "600001.SH") is None  # 无报告期概念
    # 公告日红线已在 value 中隐含：窗口外 9.0 / 未公告 3.0 / 缺公告日 3.0 均未计入


def test_val_div_yield_no_dividend_absent(fl_repo, tmp_path):
    """无分红 → 无有效暴露（dps12=0 不落行）；ex_date 恰在 12 月边界外不计。"""
    db = _prep(fl_repo, tmp_path)                            # golden 无 dividends
    _mat(db, fl_repo, "val_div_yield")
    assert query(db, "val_div_yield", dt="2021-04-30", secid="600001.SH") == []
    # 边界：ex_date = dt − 12 MONTH 当日 → 窗口 (dt−12M, dt] 左开 → 不计
    def edge(fund):
        fund["600002.SH"]["dividends"] = [
            {"ex_date": "2020-04-30", "announce": "2020-04-20", "dps": 1.0}]
    _edit_fund(fl_repo, edge)
    db2 = _prep(fl_repo, tmp_path)
    _mat(db2, fl_repo, "val_div_yield")
    assert query(db2, "val_div_yield", dt="2021-04-30", secid="600002.SH") == []
    inside = query(db2, "val_div_yield", dt="2021-04-29", secid="600002.SH")
    assert inside and inside[0]["value"] == pytest.approx(1.0 / 12.5, rel=1e-9)


# ── 五、定义静态契约：假设绑定 ↔ registry ────────────────────────────
def test_val_defs_hypothesis_binding():
    """4 估值因子 yaml 绑定 H-0007..H-0010；registry 对应条目反向含因子 id 且方向一致。"""
    import yaml
    reg = json.loads(SB_REGISTRY.read_text(encoding="utf-8"))
    by_id = {h["id"]: h for h in reg.get("hypotheses", [])}
    for fid in VAL4:
        p = ROOT / "factorlib" / "defs" / "f2_fundamental" / f"{fid}.yaml"
        meta = yaml.safe_load(p.read_text(encoding="utf-8"))
        hid = meta.get("hypothesis_id")
        assert hid == _HYPS[fid], f"{fid} 假设绑定漂移：{hid}"
        h = by_id.get(hid)
        assert h is not None, f"{hid} 未在假设登记表"
        assert fid in h["linked_factors"]
        # 方向一致性：因子 low ↔ 假设 negative；因子 high ↔ 假设 positive
        want = "negative" if meta["direction"] == "low" else "positive"
        assert h["expected_direction"] == want
        assert h["statement"] and h["source"]["ref"]         # 可检验命题 + 来源可追溯


# ── 六、真实库留证（T-F2 物化产物；文件缺失 → skip）──────────────────
def _requires_real():
    return not (REAL_REPO / "market" / "daily_bars.json").exists() or not REAL_DB.exists()


@pytest.mark.skipif(_requires_real(), reason="真实库/数据面缺失（CI 只跑合成层）")
def test_real_val_factors_materialized():
    """真实库：4 估值因子物化量级 >0 且版本 1.0.0 在档。"""
    con = duckdb.connect(str(REAL_DB), read_only=True)
    try:
        rows = dict(con.execute(
            "select factor_id, count(*) from fl_factor_value_daily "
            "where factor_id in (?,?,?,?) group by 1", list(VAL4)).fetchall())
    finally:
        con.close()
    for fid in VAL4:
        assert rows.get(fid, 0) > 0, f"{fid} 未物化"


@pytest.mark.skipif(_requires_real(), reason="真实库/数据面缺失")
def test_real_val_p50_dimension_sanity():
    """量纲守卫（宽松带宽）：P/B~个位、P/S~个位/十位、EV/EBITDA~十位、股息率 <15%。"""
    con = duckdb.connect(str(REAL_DB), read_only=True)
    try:
        bands = {"val_pb_lf": (0.05, 25.0), "val_ps_ttm": (0.05, 60.0),
                 "val_ev_ebitda": (0.5, 120.0), "val_div_yield": (0.0, 0.15)}
        for fid, (lo, hi) in bands.items():
            med = con.execute(
                "select median(value) from fl_factor_value_daily "
                "where factor_id = ? and value is not null", [fid]).fetchone()[0]
            assert med is not None and lo < med < hi, f"{fid} p50={med} 超出量纲带宽"
    finally:
        con.close()


@pytest.mark.skipif(_requires_real(), reason="真实库/数据面缺失")
def test_real_ev_ebitda_coverage_disclosure():
    """披露性断言：EV/EBITDA 覆盖（dep Q1/Q3 缺 → 跨期回退）覆盖日期数 ≤ P/B。"""
    con = duckdb.connect(str(REAL_DB), read_only=True)
    try:
        n = dict(con.execute(
            "select factor_id, count(distinct dt) from fl_factor_value_daily "
            "where factor_id in ('val_pb_lf','val_ev_ebitda') group by 1").fetchall())
    finally:
        con.close()
    assert n.get("val_ev_ebitda", 0) > 0
    assert n.get("val_ev_ebitda", 0) <= n.get("val_pb_lf", 0)
