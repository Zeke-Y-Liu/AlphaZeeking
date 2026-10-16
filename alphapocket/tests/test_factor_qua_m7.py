"""M7 T-F3：质量族 5 因子测试——TTM/LF 双口径锁 / PIT 红线 / 缺口语义 / 负值边界 / 假设绑定。

三层（与 test_factor_val_m7 同模式）：
- 合成 fixture（golden repo 注入负净利/资不抵债/营业成本/新 BS 期 → 重 prepare → 物化 →
  query 对账）；golden 算术独立手工核对（600001.SH ttm_np(2021Q1)=110e6、ttm_ocf=11e6、
  equity=5e9 → roe=0.022 等）；
- 包内 defs 静态契约：5 质量因子 hypothesis 绑定 ↔ sandbox/repo hypothesis/registry +
  direction ↔ eval asc/desc 映射互锁（DoD f：asc=值小好语义锁定）；
- 真实库留证（T-F3 物化产物，文件缺失 → skip）：量级/量纲/覆盖披露/检验与门禁在档。

运行：``cd alphapocket && python -m pytest tests/test_factor_qua_m7.py -q``
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

QUA5 = ("qua_roe_ttm", "qua_gross_margin", "qua_accruals",
        "qua_ocf_to_np", "qua_debt_to_asset")
_HYPS = {"qua_roe_ttm": "H-0011", "qua_gross_margin": "H-0012",
         "qua_accruals": "H-0013", "qua_ocf_to_np": "H-0014",
         "qua_debt_to_asset": "H-0015"}


# ── 注入 helpers（golden repo 可变副本上操作）─────────────────────────
def _edit_fund(fl_repo: Path, fn) -> None:
    fp = fl_repo / "factors" / "fundamental_values.json"
    fund = json.loads(fp.read_text(encoding="utf-8"))
    fn(fund)
    fp.write_text(json.dumps(fund, ensure_ascii=False), encoding="utf-8")


def _prep(fl_repo: Path, tmp_path: Path):
    from factorlib.prepare import prepare
    db = tmp_path / "fl_qua.duckdb"
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
    out = {}
    for fid in fids:
        meta = load_factor(fl_repo, fid)
        out[fid] = materialize(meta, db, mode="full")
    return out


# ── 一、TTM 阶梯扩列：ttm_ocf / ttm_cost ─────────────────────────────
def test_ttm_ocf_cost_cumulative_math(fl_db):
    """ttm_ocf/ttm_cost 沿累计口径：YTD + FY(Y−1) − YTD(同期上年)，12 月直接 YTD。

    600001.SH：ttm_ocf(2021Q1) = 3.5e6 + 1e7(2020FY) − 2.5e6(2020Q1) = 1.1e7；
    golden 无 cost_of_sales → ttm_cost 全 NULL（依赖缺失 → 该列 NULL，不 mock）。
    """
    db, _ = fl_db
    con = duckdb.connect(str(db), read_only=True)
    try:
        rows = {str(p): (ocf, cost) for p, ocf, cost in con.execute(
            "select period, ttm_ocf, ttm_cost from fl_dws_financial_ttm "
            "where secid = '600001.SH'").fetchall()}
    finally:
        con.close()
    assert rows["2020-12-31"][0] == pytest.approx(1e7)       # 12 月期直接 YTD
    assert rows["2021-03-31"][0] == pytest.approx(1.1e7)
    assert rows["2021-03-31"][1] is None                     # cost 依赖缺失 → NULL
    assert rows["2020-12-31"][1] is None


# ── 二、ROE TTM（TTM/LF 双口径锁 + 负净资产 + PIT）────────────────────
def test_qua_roe_ttm_golden_value(fl_repo, tmp_path):
    """ROE = 归母净利 TTM / 净资产 LF——TTM/LF 错配锁定（单测红线）。

    600001.SH 2021-04-30：ttm_np(2021Q1) = 35e6 + 1e8(2020FY) − 2.5e7(2020Q1) = 1.1e8
    （≠ YTD 35e6，分子若误用 YTD 得 0.007 —— 错）；equity = 1e10 − 5e9 = 5e9（LF）
    → ROE = 1.1e8/5e9 = 0.022，used_period=2021-03-31。
    """
    db = _prep(fl_repo, tmp_path)
    _mat(db, fl_repo, "qua_roe_ttm")
    hits = query(db, "qua_roe_ttm", dt="2021-04-30", secid="600001.SH")
    assert len(hits) == 1
    assert hits[0]["value"] == pytest.approx(0.022, rel=1e-9)
    pit = _pit(db, "qua_roe_ttm", "2021-04-30", "600001.SH")
    assert str(pit[0]) == "2021-03-31" and str(pit[1]) == "2021-04-30"
    # 2021-04-29（Q1 未公告）→ 两链回退 2020FY：ttm_np=1e8 → ROE = 0.02
    early = query(db, "qua_roe_ttm", dt="2021-04-29", secid="600001.SH")
    assert early and early[0]["value"] == pytest.approx(0.02, rel=1e-9)


def test_qua_roe_ttm_negative_equity_absent(fl_repo, tmp_path):
    """资不抵债（净资产 ≤0）→ ROE 无意义，NULL 不落行；此前期间不受影响。"""

    def insolvent(fund):
        fund["600002.SH"]["reports"].append(
            {"period": "2021-03-31", "announce_date": "2021-04-30",
             "eps_ytd": 0.2, "net_profit": 2e6, "revenue": 2e8,
             "total_assets": 1e9, "total_liab": 2e9,        # equity = −1e9
             "ocf": 1e5, "capex": 5e4})
    _edit_fund(fl_repo, insolvent)
    db = _prep(fl_repo, tmp_path)
    _mat(db, fl_repo, "qua_roe_ttm")
    assert query(db, "qua_roe_ttm", dt="2021-04-30", secid="600002.SH") == []
    early = query(db, "qua_roe_ttm", dt="2021-04-29", secid="600002.SH")
    assert early and early[0]["value"] == pytest.approx(0.02, rel=1e-9)


def test_qua_roe_ttm_pit_announce_tamper(fl_repo, tmp_path):
    """PIT 红线（篡改对象）：Q1 公告日改到未来 → TTM 链与 BS 链同步回退 2020FY。"""

    def tamper(fund):
        for r in fund["600001.SH"]["reports"]:
            if r["period"] == "2021-03-31":
                r["announce_date"] = "2021-05-10"
    _edit_fund(fl_repo, tamper)
    db = _prep(fl_repo, tmp_path)
    _mat(db, fl_repo, "qua_roe_ttm")
    hits = query(db, "qua_roe_ttm", dt="2021-04-30", secid="600001.SH")
    assert len(hits) == 1
    assert hits[0]["value"] == pytest.approx(0.02, rel=1e-9)      # 2020FY 口径
    pit = _pit(db, "qua_roe_ttm", "2021-04-30", "600001.SH")
    assert str(pit[0]) == "2020-12-31"                             # 未见 Q1（公告在 05-10）
    assert str(pit[1]) == "2021-03-31"
    later = query(db, "qua_roe_ttm", dt="2021-05-28", secid="600001.SH")
    assert later and str(_pit(db, "qua_roe_ttm", "2021-05-28", "600001.SH")[0]) == "2021-03-31"


# ── 三、毛利率（缺口语义）─────────────────────────────────────────────
def test_qua_gross_margin_gap_no_row(fl_repo, tmp_path):
    """缺口路径（契约 d）：cost_of_sales 全缺（金融行业属性）→ 因子无行，不 mock 不填 0。"""
    db = _prep(fl_repo, tmp_path)                            # golden 无 cost_of_sales
    _mat(db, fl_repo, "qua_gross_margin")
    assert query(db, "qua_gross_margin", dt="2021-04-30", secid="600001.SH") == []
    assert query(db, "qua_gross_margin", dt="2021-04-30", secid="600002.SH") == []


def test_qua_gross_margin_golden_value(fl_repo, tmp_path):
    """注入营业成本（=营收×0.7）→ 毛利率恒 0.3；PIT 切期证据在 used_period。

    2021-04-30：ttm_rev=1.1e9、ttm_cost = 2.45e8 + 7e8(2020FY) − 1.75e8(2020Q1) = 7.7e8
    → (1.1e9 − 7.7e8)/1.1e9 = 0.3，used_period=2021-03-31；
    2021-04-29 回退 2020FY：ttm_cost=7e8 → 0.3，used_period=2020-12-31（切期）。
    """

    def inject(fund):
        for s in ("600001.SH", "600002.SH"):
            for r in fund[s]["reports"]:
                if r["period"] in ("2020-03-31", "2020-12-31", "2021-03-31"):
                    r["cost_of_sales"] = r["revenue"] * 0.7
    _edit_fund(fl_repo, inject)
    db = _prep(fl_repo, tmp_path)
    _mat(db, fl_repo, "qua_gross_margin")
    hits = query(db, "qua_gross_margin", dt="2021-04-30", secid="600001.SH")
    assert len(hits) == 1
    assert hits[0]["value"] == pytest.approx(0.3, rel=1e-9)
    assert str(_pit(db, "qua_gross_margin", "2021-04-30", "600001.SH")[0]) == "2021-03-31"
    early = query(db, "qua_gross_margin", dt="2021-04-29", secid="600001.SH")
    assert early and early[0]["value"] == pytest.approx(0.3, rel=1e-9)
    assert str(_pit(db, "qua_gross_margin", "2021-04-29", "600001.SH")[0]) == "2020-12-31"


# ── 四、应计 / 含金量 / 杠杆 ──────────────────────────────────────────
def test_qua_accruals_golden_value(fl_repo, tmp_path):
    """应计 = (ttm_np − ttm_ocf)/total_assets（LF 期末分母，Sloan 口径简化如实标注）。

    600001.SH 2021-04-30：(1.1e8 − 1.1e7)/1e10 = 0.0099；
    2021-04-29（回退 2020FY）：(1e8 − 1e7)/1e10 = 0.009。
    """
    db = _prep(fl_repo, tmp_path)
    _mat(db, fl_repo, "qua_accruals")
    hits = query(db, "qua_accruals", dt="2021-04-30", secid="600001.SH")
    assert len(hits) == 1
    assert hits[0]["value"] == pytest.approx(0.0099, rel=1e-9)
    assert str(_pit(db, "qua_accruals", "2021-04-30", "600001.SH")[0]) == "2021-03-31"
    early = query(db, "qua_accruals", dt="2021-04-29", secid="600001.SH")
    assert early and early[0]["value"] == pytest.approx(0.009, rel=1e-9)


def test_qua_ocf_to_np_golden_value(fl_repo, tmp_path):
    """含金量 = ttm_ocf / ttm_np：600001.SH 2021-04-30 = 1.1e7/1.1e8 = 0.1。"""
    db = _prep(fl_repo, tmp_path)
    _mat(db, fl_repo, "qua_ocf_to_np")
    hits = query(db, "qua_ocf_to_np", dt="2021-04-30", secid="600001.SH")
    assert len(hits) == 1
    assert hits[0]["value"] == pytest.approx(0.1, rel=1e-9)
    assert str(_pit(db, "qua_ocf_to_np", "2021-04-30", "600001.SH")[0]) == "2021-03-31"


def test_qua_ocf_to_np_negative_np_absent(fl_repo, tmp_path):
    """边界（契约 c）：净利 TTM ≤0 → 含金量无经济意义 → 无行；负 ROE 同点如实保留。

    600007.SH Q1 公告日为 2021-05-28（golden 特例）：亏损 Q1 仅在 05-28 起可见。
    """

    def loss(fund):
        for r in fund["600007.SH"]["reports"]:
            if r["period"] == "2021-03-31":
                r["net_profit"] = -1.2e8      # ttm_np = −1.2e8 + 1e8 − 2.5e7 = −4.5e7
    _edit_fund(fl_repo, loss)
    db = _prep(fl_repo, tmp_path)
    _mat(db, fl_repo, "qua_ocf_to_np", "qua_roe_ttm")
    # 05-28 起最新可见报告 = 亏损 Q1 → 含金量 NULL 不落行（且不回退 2020FY）
    assert query(db, "qua_ocf_to_np", dt="2021-05-28", secid="600007.SH") == []
    # 05-28 前一日（Q1 未公告）：最新可见 = 2020FY（04-29 公告）→ 仍有值 0.1
    early = query(db, "qua_ocf_to_np", dt="2021-04-30", secid="600007.SH")
    assert early and early[0]["value"] == pytest.approx(0.1, rel=1e-9)
    assert str(_pit(db, "qua_ocf_to_np", "2021-04-30", "600007.SH")[0]) == "2020-12-31"
    # 同点（05-28）ROE = −4.5e7/5e9 = −0.009（负净利 ROE 是真实信息，保留）
    roe = query(db, "qua_roe_ttm", dt="2021-05-28", secid="600007.SH")
    assert roe and roe[0]["value"] == pytest.approx(-0.009, rel=1e-9)


def test_qua_debt_to_asset_lf_switch(fl_repo, tmp_path):
    """杠杆 = 总负债 LF/总资产 LF；LF 期切换（QUALIFY）：新 BS 期公告当日生效。

    600005.SH 注入 2021-06-30 期（当日公告）：2021-06-30 截面 6e9/1e10=0.6
    （used_period=2021-06-30）；2021-04-30 截面 0.5（Q1 当日公告生效
    used_period=2021-03-31）；2021-04-29（公告前一日）0.5（used_period=2020-12-31）。
    """

    def inject(fund):
        fund["600005.SH"]["reports"].append(
            {"period": "2021-06-30", "announce_date": "2021-06-30",
             "eps_ytd": 0.6, "net_profit": 6e7, "revenue": 6e8,
             "total_assets": 1e10, "total_liab": 6e9,
             "ocf": 6e6, "capex": 3e6})
    _edit_fund(fl_repo, inject)
    db = _prep(fl_repo, tmp_path)
    _mat(db, fl_repo, "qua_debt_to_asset")
    hits = query(db, "qua_debt_to_asset", dt="2021-06-30", secid="600005.SH")
    assert len(hits) == 1
    assert hits[0]["value"] == pytest.approx(0.6, rel=1e-9)
    assert str(_pit(db, "qua_debt_to_asset", "2021-06-30", "600005.SH")[0]) == "2021-06-30"
    early = query(db, "qua_debt_to_asset", dt="2021-04-30", secid="600005.SH")
    assert early and early[0]["value"] == pytest.approx(0.5, rel=1e-9)
    # Q1 于 04-30 当日公告 → 当日生效（QUALIFY 同日含）
    assert str(_pit(db, "qua_debt_to_asset", "2021-04-30", "600005.SH")[0]) == "2021-03-31"
    # 公告前一日仍是 2020FY
    prev = query(db, "qua_debt_to_asset", dt="2021-04-29", secid="600005.SH")
    assert prev and prev[0]["value"] == pytest.approx(0.5, rel=1e-9)
    assert str(_pit(db, "qua_debt_to_asset", "2021-04-29", "600005.SH")[0]) == "2020-12-31"


# ── 五、direction 语义锁 + 定义静态契约 ───────────────────────────────
def test_qua_direction_mapping_locked(fl_repo):
    """DoD f（direction 一致性）：yaml low ↔ eval asc（值小好）/ high ↔ desc（值大好）互锁。

    accruals/debt 为 asc（高应计=低质量、高杠杆=高风险 → 原始值小的组应得高分）；
    roe/gm/ocf_to_np 为 desc。映射接反 = 分层多空反向，此处与门禁脚本 DIRECTION 同源锁定。
    """
    WANT_EVAL = {"qua_roe_ttm": "desc", "qua_gross_margin": "desc",
                 "qua_accruals": "asc", "qua_ocf_to_np": "desc",
                 "qua_debt_to_asset": "asc"}
    from applib.factor_eval_fl import FactorEvalError, run_factor_eval
    for fid, want_eval in WANT_EVAL.items():
        meta = load_factor(fl_repo, fid)
        assert meta.direction in ("low", "high")
        assert (meta.direction == "low") == (want_eval == "asc"), f"{fid} 方向映射漂移"
        # run_factor_eval 只收 asc/desc：误传 yaml 口径 high/low 必须立即报错（不触库）
        with pytest.raises(FactorEvalError):
            run_factor_eval(REAL_DB, REAL_REPO, fid, meta.direction)


def test_qua_defs_hypothesis_binding():
    """5 质量因子 yaml 绑定 H-0011..H-0015；registry 反向含因子 id 且方向一致。"""
    import yaml
    reg = json.loads(SB_REGISTRY.read_text(encoding="utf-8"))
    by_id = {h["id"]: h for h in reg.get("hypotheses", [])}
    for fid in QUA5:
        p = ROOT / "factorlib" / "defs" / "qua" / f"{fid}.yaml"
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


# ── 六、真实库留证（T-F3 物化产物；文件缺失 → skip）──────────────────
def _requires_real():
    return not (REAL_REPO / "market" / "daily_bars.json").exists() or not REAL_DB.exists()


@pytest.mark.skipif(_requires_real(), reason="真实库/数据面缺失（CI 只跑合成层）")
def test_real_qua_factors_materialized():
    """真实库：5 质量因子物化量级 >0 且版本 1.0.0 在档。"""
    con = duckdb.connect(str(REAL_DB), read_only=True)
    try:
        rows = dict(con.execute(
            "select factor_id, count(*) from fl_factor_value_daily "
            f"where factor_id in ({','.join('?' * len(QUA5))}) group by 1",
            list(QUA5)).fetchall())
    finally:
        con.close()
    for fid in QUA5:
        assert rows.get(fid, 0) > 0, f"{fid} 未物化"


@pytest.mark.skipif(_requires_real(), reason="真实库/数据面缺失")
def test_real_qua_p50_dimension_sanity():
    """量纲守卫（宽松带宽）：ROE ~几%~几十%、毛利率 0~1、应计 ±小数、含金量正数、杠杆 0~1。"""
    con = duckdb.connect(str(REAL_DB), read_only=True)
    try:
        bands = {"qua_roe_ttm": (0.0, 0.60), "qua_gross_margin": (0.02, 0.98),
                 "qua_accruals": (-0.50, 0.50), "qua_ocf_to_np": (-20.0, 40.0),
                 "qua_debt_to_asset": (0.02, 0.98)}
        for fid, (lo, hi) in bands.items():
            med = con.execute(
                "select median(value) from fl_factor_value_daily "
                "where factor_id = ? and value is not null", [fid]).fetchone()[0]
            assert med is not None and lo < med < hi, f"{fid} p50={med} 超出量纲带宽"
    finally:
        con.close()


@pytest.mark.skipif(_requires_real(), reason="真实库/数据面缺失")
def test_real_qua_eval_gate_on_record():
    """契约 e + DoD f 分层留证：fl_factor_eval / fl_factor_gate 在档（分层与超额字段如实落库）；
    gross_margin 覆盖 ≤ roe 覆盖（无营业成本科目标的如实缺数据，非 mock）。"""
    con = duckdb.connect(str(REAL_DB), read_only=True)
    try:
        ev = dict(con.execute(
            "select factor_id, count(*) from fl_factor_eval "
            f"where factor_id in ({','.join('?' * len(QUA5))}) and directed_mean is not null "
            "group by 1", list(QUA5)).fetchall())
        gt = dict(con.execute(
            "select factor_id, count(*) from fl_factor_gate "
            f"where factor_id in ({','.join('?' * len(QUA5))}) group by 1",
            list(QUA5)).fetchall())
        cov = dict(con.execute(
            "select factor_id, count(distinct secid) from fl_factor_value_daily "
            "where factor_id in ('qua_roe_ttm','qua_gross_margin') group by 1").fetchall())
    finally:
        con.close()
    for fid in QUA5:
        assert ev.get(fid, 0) >= 3, f"{fid} fl_factor_eval 在档不足（{ev.get(fid)} 行）"
        assert gt.get(fid, 0) >= 1, f"{fid} fl_factor_gate 未在档"
    assert cov.get("qua_gross_margin", 0) <= cov.get("qua_roe_ttm", 0)
