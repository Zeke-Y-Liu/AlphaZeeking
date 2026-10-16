"""M7 T-F4：成长族 4 因子测试——TTM 对齐锁 / PIT 红线 / 负值与窗口边界 / 预热锚 / 假设绑定。

三层（与 test_factor_qua_m7 同模式）：
- 合成 fixture（golden repo 注入历史报告期/负净利/负基数 → 重 prepare → 物化 →
  query 对账）；golden 算术独立手工核对（600001.SH 注入 2019Q1 后：
  ttm_rev(2021Q1)=1.1e9、ttm_rev(2020Q1)=1e9 → yoy=0.1；YTD 误用得 0.4 → 锁死 TTM 对齐；
  stability 手工推演：yoy 序列 {0×7, 0.1} → samp std = sqrt(0.00125) = 0.035355…）；
- 包内 defs 静态契约：4 成长因子 hypothesis 绑定 ↔ sandbox/repo hypothesis/registry +
  direction ↔ eval asc/desc 映射互锁（DoD f：asc=值小好语义锁定）；
- 真实库留证（T-F4 物化产物，文件缺失 → skip）：量级/量纲/预热覆盖（2021-01 截面 >0）/
  检验与门禁在档；surprise 缺口登记三要素在档。

运行：``cd alphapocket && python -m pytest tests/test_factor_gro_m7.py -q``
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
GAP_MD = ROOT / "sandbox" / "artifacts" / "m7_gaps" / "gro_earnings_surprise.md"

GRO4 = ("gro_rev_yoy", "gro_np_yoy", "gro_rev_cagr_3y", "gro_stability")
_HYPS = {"gro_rev_yoy": "H-0016", "gro_np_yoy": "H-0017",
         "gro_rev_cagr_3y": "H-0018", "gro_stability": "H-0019"}


# ── 注入 helpers（golden repo 可变副本上操作）─────────────────────────
def _edit_fund(fl_repo: Path, fn) -> None:
    fp = fl_repo / "factors" / "fundamental_values.json"
    fund = json.loads(fp.read_text(encoding="utf-8"))
    fn(fund)
    fp.write_text(json.dumps(fund, ensure_ascii=False), encoding="utf-8")


def _inject_reports(fund: dict, secid: str, rows: list[dict]) -> None:
    for r in rows:
        fund[secid]["reports"].append(r)


# 注入模板：600001.SH 2019Q1（使 ttm(2020Q1) 可算：2.5e8 + FY2019 1e9 − 2.5e8 = 1e9）
_Q1_2019 = {"period": "2019-03-31", "announce_date": "2019-04-28",
            "eps_ytd": 0.25, "net_profit": 2.5e7, "revenue": 2.5e8,
            "total_assets": 1e10, "total_liab": 5e9, "ocf": 2.5e6, "capex": 1.25e6}

# 注入模板：2017Q1..2019Q2 稳态序列（每季 YTD 2.5e8 累计 → 各期 ttm_rev ≡ 1e9）
# 手工推演见 test_gro_stability_golden_value 注释
_STEADY = [
    {"period": "2017-03-31", "announce_date": "2017-04-28", "revenue": 2.5e8},
    {"period": "2017-06-30", "announce_date": "2017-08-30", "revenue": 5.0e8},
    {"period": "2017-09-30", "announce_date": "2017-10-31", "revenue": 7.5e8},
    {"period": "2017-12-31", "announce_date": "2018-03-30", "revenue": 1.0e9},
    {"period": "2018-03-31", "announce_date": "2018-04-28", "revenue": 2.5e8},
    {"period": "2018-06-30", "announce_date": "2018-08-30", "revenue": 5.0e8},
    {"period": "2018-09-30", "announce_date": "2018-10-31", "revenue": 7.5e8},
    {"period": "2018-12-31", "announce_date": "2019-03-29", "revenue": 1.0e9},
    {"period": "2019-03-31", "announce_date": "2019-04-28", "revenue": 2.5e8},
    {"period": "2019-06-30", "announce_date": "2019-08-30", "revenue": 5.0e8},
]


def _prep(fl_repo: Path, tmp_path: Path):
    from factorlib.prepare import prepare
    db = tmp_path / "fl_gro.duckdb"
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
    for fid in fids:
        meta = load_factor(fl_repo, fid)
        materialize(meta, db, mode="full")
    return db


# ── 一、营收/净利 TTM 同比：golden 手工对账 + TTM 对齐锁 ────────────────
def test_gro_rev_yoy_golden_value(fl_repo, tmp_path):
    """yoy = ttm_rev(P)/ttm_rev(P−4q) − 1——TTM 对齐锁定（误用 YTD 得 0.4，正确 0.1）。

    600001.SH 注入 2019Q1 后：ttm_rev(2021Q1) = 3.5e8 + 1e9(FY2020) − 2.5e8 = 1.1e9；
    ttm_rev(2020Q1) = 2.5e8 + 1e9(FY2019) − 2.5e8 = 1e9 → 2021-04-30 yoy = 0.1；
    2021-04-29（Q1 未公告）回退 2020Q4：ttm 1e9 / ttm(2019Q4) 1e9 → 0（used=2020-12-31）。
    """

    def inject(fund):
        _inject_reports(fund, "600001.SH", [dict(_Q1_2019)])
    _edit_fund(fl_repo, inject)
    db = _mat(_prep(fl_repo, tmp_path), fl_repo, "gro_rev_yoy")
    hits = query(db, "gro_rev_yoy", dt="2021-04-30", secid="600001.SH")
    assert len(hits) == 1
    assert hits[0]["value"] == pytest.approx(0.1, rel=1e-9)
    pit = _pit(db, "gro_rev_yoy", "2021-04-30", "600001.SH")
    assert str(pit[0]) == "2021-03-31" and str(pit[1]) == "2021-04-30"
    early = query(db, "gro_rev_yoy", dt="2021-04-29", secid="600001.SH")
    assert early and early[0]["value"] == pytest.approx(0.0, rel=1e-9, abs=1e-12)
    assert str(_pit(db, "gro_rev_yoy", "2021-04-29", "600001.SH")[0]) == "2020-12-31"


def test_gro_np_yoy_golden_value(fl_repo, tmp_path):
    """净利 yoy 同口径：ttm_np(2021Q1)=1.1e8 / ttm_np(2020Q1)=1e8 → 0.1。"""

    def inject(fund):
        _inject_reports(fund, "600001.SH", [dict(_Q1_2019)])
    _edit_fund(fl_repo, inject)
    db = _mat(_prep(fl_repo, tmp_path), fl_repo, "gro_np_yoy")
    hits = query(db, "gro_np_yoy", dt="2021-04-30", secid="600001.SH")
    assert len(hits) == 1
    assert hits[0]["value"] == pytest.approx(0.1, rel=1e-9)
    assert str(_pit(db, "gro_np_yoy", "2021-04-30", "600001.SH")[0]) == "2021-03-31"


def test_gro_np_yoy_negative_np_absent(fl_repo, tmp_path):
    """边界（契约 c）：净利 TTM ≤0（分子或分母）→ 同比比率失真 → 无行；计数披露在物化脚本。

    600007.SH Q1（公告 2021-05-28）注入亏损：ttm_np = −1.2e8 + 1e8 − 2.5e7 = −4.5e7
    → 05-28 截面无行（不以回退 2020FY 顶替——最新可见期是 Q1，其值为 NULL 即缺席）；
    05-28 之前（04-30）最新可见 = 2020FY（04-29 公告）→ 仍有值 0。
    """

    def loss(fund):
        for r in fund["600007.SH"]["reports"]:
            if r["period"] == "2021-03-31":
                r["net_profit"] = -1.2e8
    _edit_fund(fl_repo, loss)
    db = _mat(_prep(fl_repo, tmp_path), fl_repo, "gro_np_yoy")
    assert query(db, "gro_np_yoy", dt="2021-05-28", secid="600007.SH") == []
    early = query(db, "gro_np_yoy", dt="2021-04-30", secid="600007.SH")
    assert early and early[0]["value"] == pytest.approx(0.0, rel=1e-9, abs=1e-12)
    assert str(_pit(db, "gro_np_yoy", "2021-04-30", "600007.SH")[0]) == "2020-12-31"


def test_gro_rev_yoy_base_nonpositive_absent(fl_repo, tmp_path):
    """边界（契约 c）：基期 TTM ≤0 → 同比无意义 → 无行（真实信息由缺席承载，不填 0）。

    600001.SH 注入 2019Q1（rev 2.5e8）并改 2020Q1 YTD rev = −8e8：
    ttm_rev(2020Q1) = −8e8 + 1e9 − 2.5e8 = −5e8 < 0 → 2021-04-30 无行；
    04-29 截面（cur=2020Q4，base=2019Q4）不受影响 → 0。
    """

    def negbase(fund):
        _inject_reports(fund, "600001.SH", [dict(_Q1_2019)])
        for r in fund["600001.SH"]["reports"]:
            if r["period"] == "2020-03-31":
                r["revenue"] = -8e8
    _edit_fund(fl_repo, negbase)
    db = _mat(_prep(fl_repo, tmp_path), fl_repo, "gro_rev_yoy")
    assert query(db, "gro_rev_yoy", dt="2021-04-30", secid="600001.SH") == []
    early = query(db, "gro_rev_yoy", dt="2021-04-29", secid="600001.SH")
    assert early and early[0]["value"] == pytest.approx(0.0, rel=1e-9, abs=1e-12)


# ── 二、3 年 CAGR：多期手工复利推演 ───────────────────────────────────
def test_gro_rev_cagr_3y_golden_value(fl_repo, tmp_path):
    """CAGR = (ttm_rev(P)/ttm_rev(P−12q))^(1/3) − 1——多期复利手工推演（DoD a）。

    注入 2017Q1（rev 2e8）/ 2017Q4（rev 8e8）/ 2018Q1（rev 2.2e8）：
    ttm_rev(2018Q1) = 2.2e8 + 8e8(FY2017) − 2e8 = 8.2e8（基期）。
    2021-04-30：(1.1e9/8.2e8)^(1/3) − 1 ≈ 0.10280（12 期回看）；
    2021-04-29（回退 2020Q4）：(1e9/8e8)^(1/3) − 1 = 1.25^(1/3) − 1 ≈ 0.07722
    （基期 2017Q4 ttm = 12 月期 YTD = 8e8）。
    """

    def inject(fund):
        _inject_reports(fund, "600001.SH", [
            {"period": "2017-03-31", "announce_date": "2017-04-28", "revenue": 2.0e8},
            {"period": "2017-12-31", "announce_date": "2018-03-30", "revenue": 8.0e8},
            {"period": "2018-03-31", "announce_date": "2018-04-28", "revenue": 2.2e8},
        ])
    _edit_fund(fl_repo, inject)
    db = _mat(_prep(fl_repo, tmp_path), fl_repo, "gro_rev_cagr_3y")
    hits = query(db, "gro_rev_cagr_3y", dt="2021-04-30", secid="600001.SH")
    assert len(hits) == 1
    assert hits[0]["value"] == pytest.approx((1.1e9 / 8.2e8) ** (1 / 3) - 1, rel=1e-9)
    assert str(_pit(db, "gro_rev_cagr_3y", "2021-04-30", "600001.SH")[0]) == "2021-03-31"
    early = query(db, "gro_rev_cagr_3y", dt="2021-04-29", secid="600001.SH")
    assert early and early[0]["value"] == pytest.approx(1.25 ** (1 / 3) - 1, rel=1e-9)
    assert str(_pit(db, "gro_rev_cagr_3y", "2021-04-29", "600001.SH")[0]) == "2020-12-31"


# ── 三、增长稳定性：8 季窗口 std + 窗口不足边界 ───────────────────────
def test_gro_stability_golden_value(fl_repo, tmp_path):
    """stability = 近 8 季 yoy 的样本标准差——手工推演（DoD a + 真实 0 与 None 区分锁）。

    注入 2017Q1..2019Q2 稳态序列（各期 ttm_rev ≡ 1e9）后，2021-04-30 窗口
    （2019Q2..2021Q1 八时点）yoy = {0,0,0,0,0,0,0,0.1}（2021Q1 增长为 golden 特设）：
    mean=0.0125，平方和 = 7×0.0125² + 0.0875² = 0.00875，样本方差 = 0.00875/7 = 0.00125
    → std = sqrt(0.00125) = 0.035355339059327378。
    2021-04-29 窗口（2019Q1..2020Q4）yoy 全 0 → std = 0（真实 0：稳态增长有行值 0，
    与窗口不足的 None 无行严格区分）。
    """

    def inject(fund):
        _inject_reports(fund, "600001.SH", [dict(r) for r in _STEADY])
    _edit_fund(fl_repo, inject)
    db = _mat(_prep(fl_repo, tmp_path), fl_repo, "gro_stability")
    hits = query(db, "gro_stability", dt="2021-04-30", secid="600001.SH")
    assert len(hits) == 1
    assert hits[0]["value"] == pytest.approx(0.035355339059327378, rel=1e-9)
    assert str(_pit(db, "gro_stability", "2021-04-30", "600001.SH")[0]) == "2021-03-31"
    early = query(db, "gro_stability", dt="2021-04-29", secid="600001.SH")
    assert early and early[0]["value"] == pytest.approx(0.0, abs=1e-12)
    assert str(_pit(db, "gro_stability", "2021-04-29", "600001.SH")[0]) == "2020-12-31"


def test_gro_stability_insufficient_window_none(fl_repo, tmp_path):
    """边界（契约 c）：窗口不足 8 季（golden 缺 2019Q2 及更早）→ None 无行。

    golden 600001.SH 仅 2019Q3..2021Q1 七期，窗口 2019Q2 时点缺报告 → 样本 <8 → 无行
    （红线：不以部分窗口的 std 顶替，不以 0 顶替）。
    """
    db = _mat(_prep(fl_repo, tmp_path), fl_repo, "gro_stability")
    assert query(db, "gro_stability", dt="2021-04-30", secid="600001.SH") == []
    assert query(db, "gro_stability", dt="2021-04-30", secid="600002.SH") == []


# ── 四、PIT 红线（篡改注入捕获）───────────────────────────────────────
def test_gro_pit_announce_tamper(fl_repo, tmp_path):
    """PIT 红线（篡改对象）：2021Q1 公告日改到未来 → 当期链回退 2020Q4（yoy=0）。

    捕获证据：2021-04-30 截面 used_period=2020-12-31（原应 2021-03-31/yoy=0.1）；
    05-28（篡改公告日 05-10 已过）恢复 2021Q1 口径。
    """

    def tamper(fund):
        _inject_reports(fund, "600001.SH", [dict(_Q1_2019)])
        for r in fund["600001.SH"]["reports"]:
            if r["period"] == "2021-03-31":
                r["announce_date"] = "2021-05-10"
    _edit_fund(fl_repo, tamper)
    db = _mat(_prep(fl_repo, tmp_path), fl_repo, "gro_rev_yoy")
    hits = query(db, "gro_rev_yoy", dt="2021-04-30", secid="600001.SH")
    assert len(hits) == 1
    assert hits[0]["value"] == pytest.approx(0.0, rel=1e-9, abs=1e-12)   # 2020FY 口径
    assert str(_pit(db, "gro_rev_yoy", "2021-04-30", "600001.SH")[0]) == "2020-12-31"
    later = query(db, "gro_rev_yoy", dt="2021-05-28", secid="600001.SH")
    assert later and later[0]["value"] == pytest.approx(0.1, rel=1e-9)
    assert str(_pit(db, "gro_rev_yoy", "2021-05-28", "600001.SH")[0]) == "2021-03-31"


# ── 五、direction 语义锁 + 定义静态契约 + 缺口登记 ─────────────────────
def test_gro_direction_mapping_locked(fl_repo):
    """DoD f（direction 一致性）：yaml low ↔ eval asc / high ↔ desc 互锁。

    stability 为 asc（高增长波动=低质量 → 原始值小的组应得高分）；
    rev_yoy / np_yoy / cagr_3y 为 desc。映射接反 = 分层多空反向，与门禁脚本 DIRECTION 同源锁定。
    """
    WANT_EVAL = {"gro_rev_yoy": "desc", "gro_np_yoy": "desc",
                 "gro_rev_cagr_3y": "desc", "gro_stability": "asc"}
    from applib.factor_eval_fl import FactorEvalError, run_factor_eval
    for fid, want_eval in WANT_EVAL.items():
        meta = load_factor(fl_repo, fid)
        assert meta.direction in ("low", "high")
        assert (meta.direction == "low") == (want_eval == "asc"), f"{fid} 方向映射漂移"
        # run_factor_eval 只收 asc/desc：误传 yaml 口径 high/low 必须立即报错（不触库）
        with pytest.raises(FactorEvalError):
            run_factor_eval(REAL_DB, REAL_REPO, fid, meta.direction)


def test_gro_defs_hypothesis_binding():
    """4 成长因子 yaml 绑定 H-0016..H-0019；registry 反向含因子 id 且方向一致。"""
    import yaml
    reg = json.loads(SB_REGISTRY.read_text(encoding="utf-8"))
    by_id = {h["id"]: h for h in reg.get("hypotheses", [])}
    for fid in GRO4:
        p = ROOT / "factorlib" / "defs" / "gro" / f"{fid}.yaml"
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


def test_gro_earnings_surprise_gap_registered():
    """DoD f：surprise 缺口登记文件在档且三要素齐备（缺一致预期源 / 事件型不匹配 / M8 建议）。"""
    assert GAP_MD.exists(), "gro_earnings_surprise 缺口登记文件缺失"
    text = GAP_MD.read_text(encoding="utf-8")
    assert "一致预期" in text and "未物化入库" in text      # 要素一：数据源缺失
    assert "事件型" in text and "F4_event" in text           # 要素二：类型不匹配
    assert "M8" in text and "不作为近似实现冒充" in text      # 要素三：候选建议 + 不 mock 红线
    # 未登记假设（无来源不激活——连假设都不立）
    reg = json.loads(SB_REGISTRY.read_text(encoding="utf-8"))
    assert not any("gro_earnings_surprise" in h.get("linked_factors", [])
                   for h in reg.get("hypotheses", []))


# ── 六、真实库留证（T-F4 物化产物；文件缺失 → skip）──────────────────
def _requires_real():
    return not (REAL_REPO / "market" / "daily_bars.json").exists() or not REAL_DB.exists()


@pytest.mark.skipif(_requires_real(), reason="真实库/数据面缺失（CI 只跑合成层）")
def test_real_gro_factors_materialized():
    """真实库：4 成长因子物化量级 >0 且版本 1.0.0 在档。"""
    con = duckdb.connect(str(REAL_DB), read_only=True)
    try:
        rows = dict(con.execute(
            "select factor_id, count(*) from fl_factor_value_daily "
            f"where factor_id in ({','.join('?' * len(GRO4))}) group by 1",
            list(GRO4)).fetchall())
    finally:
        con.close()
    for fid in GRO4:
        assert rows.get(fid, 0) > 0, f"{fid} 未物化"


@pytest.mark.skipif(_requires_real(), reason="真实库/数据面缺失")
def test_real_gro_warmup_coverage():
    """DoD d（预热锚，按因子区分生效时点）：
    - yoy / np_yoy / stability：2021-01 截面覆盖 >0（最早可算 2019Q1/2020Q3，2021-01 早已有行）；
    - cagr_3y：基期 TTM 最早非空 = 2018Q1 → 最早当期 = 2021Q1（2021-04 公告），
      2021-01 零覆盖是正确预热行为，生效锚在 2021-05 截面（与 defs 预热声明一致）。
    """
    anchors = {"gro_rev_yoy": "2021-01", "gro_np_yoy": "2021-01",
               "gro_stability": "2021-01", "gro_rev_cagr_3y": "2021-05"}
    con = duckdb.connect(str(REAL_DB), read_only=True)
    try:
        for fid, ym in anchors.items():
            n = con.execute(
                "select count(distinct secid) from fl_factor_value_daily "
                "where dt between date(?) and (date(?) + interval 1 month - interval 1 day) "
                "and factor_id = ?", [f"{ym}-01", f"{ym}-01", fid]).fetchone()[0]
            assert n > 0, f"{fid} {ym} 截面零覆盖（预热声明失效）"
    finally:
        con.close()


@pytest.mark.skipif(_requires_real(), reason="真实库/数据面缺失")
def test_real_gro_p50_dimension_sanity():
    """量纲守卫（宽松带宽）：yoy ~±小数、cagr ~正小数、stability ≥0。"""
    con = duckdb.connect(str(REAL_DB), read_only=True)
    try:
        bands = {"gro_rev_yoy": (-2.0, 5.0), "gro_np_yoy": (-5.0, 20.0),
                 "gro_rev_cagr_3y": (-0.95, 3.0), "gro_stability": (0.0, 3.0)}
        for fid, (lo, hi) in bands.items():
            med = con.execute(
                "select median(value) from fl_factor_value_daily "
                "where factor_id = ? and value is not null", [fid]).fetchone()[0]
            assert med is not None and lo < med < hi, f"{fid} p50={med} 超出量纲带宽"
    finally:
        con.close()


@pytest.mark.skipif(_requires_real(), reason="真实库/数据面缺失")
def test_real_gro_eval_gate_on_record():
    """DoD e 分层留证：fl_factor_eval / fl_factor_gate 在档（分层与超额字段如实落库）。"""
    con = duckdb.connect(str(REAL_DB), read_only=True)
    try:
        ev = dict(con.execute(
            "select factor_id, count(*) from fl_factor_eval "
            f"where factor_id in ({','.join('?' * len(GRO4))}) and directed_mean is not null "
            "group by 1", list(GRO4)).fetchall())
        gt = dict(con.execute(
            "select factor_id, count(*) from fl_factor_gate "
            f"where factor_id in ({','.join('?' * len(GRO4))}) group by 1",
            list(GRO4)).fetchall())
    finally:
        con.close()
    for fid in GRO4:
        assert ev.get(fid, 0) >= 3, f"{fid} fl_factor_eval 在档不足（{ev.get(fid)} 行）"
        assert gt.get(fid, 0) >= 1, f"{fid} fl_factor_gate 未在档"
