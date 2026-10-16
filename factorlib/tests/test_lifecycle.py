"""M5 生命周期测试（实施计划 §8 DoD）：门禁全路径 + 巡检演练 + 版本冻结。

合成库 L（8 标的 B01..B08 × 33 个月末，同 M4 治理测试 G）：
- F_good：value = k → 对齐 IC 恒 +1.0（激活正例 / 巡检 healthy）；
- F_decay：前 18 月 value = k（IC +1），此后循环轮换（|IC| 小幅震荡）
  → recent 24 月均值 ≈ 0.46 / prior ≈ 1.0 → 滚动衰减 ≈ 54% ≥ 50% → weakening（DoD 注入）；
- F_bad：value = 13−k → IC 恒 −1 → recent ≤ 0 → invalid → 自动 deprecated（DoD 8.4）。

DoD 口径说明：「人为回填衰减 IC，30 天内触发 weakening」演练 = 注入衰减序列后
巡检 DAG 单次执行即裁决 weakening（演练时点即触发，无等待期）。
"""
import json

import duckdb
import pytest

from factorlib.evaluate import EVAL_TABLE, _EVAL_DDLS
from factorlib.governance import run_governance_on_con
from factorlib.lifecycle import (INSPECT_DECAY_MAX, LifecycleError, activate,
                                 deactivate, def_hash, factor_health,
                                 load_hypotheses, quarterly_inspection,
                                 register_ref, verify_frozen, who_references)
from factorlib.registry import (STATUS_ACTIVE, STATUS_DEPRECATED, STATUS_DRAFT,
                                RegistryError, set_status)

import calendar
from datetime import date

SEC = [f"B{k:02d}" for k in range(1, 9)]
R = {s: 0.01 * k for k, s in enumerate(SEC, start=1)}
MES = [f"{date(y, m, calendar.monthrange(y, m)[1]).isoformat()}"
       for y, m in [(2021, m) for m in range(1, 13)]
       + [(2022, m) for m in range(1, 13)]
       + [(2023, m) for m in range(1, 10)]]
BENCH = "510300.SH"
WF, LB = (6, 3), 3


def _fv(fid: str, i: int, k: int) -> float:
    if fid == "F_good":
        return float(k)
    if fid == "F_decay":
        return float(k) if i < 18 else float((k - 1 + i) % 8 + 1)
    if fid == "F_bad":
        return float(13 - k)
    raise KeyError(fid)


@pytest.fixture()
def lc_con():
    con = duckdb.connect(":memory:")
    for ddl in _EVAL_DDLS:
        con.execute(ddl)
    con.execute("""CREATE TABLE factor_value_daily
                   (dt DATE, secid TEXT, factor_id TEXT, value DOUBLE)""")
    con.execute("""CREATE TABLE factor_pit_meta
                   (dt DATE, secid TEXT, factor_id TEXT, used_period DATE,
                    used_announce DATE, data_staleness INTEGER)""")
    for s in SEC:
        c, closes = 100.0, []
        for _ in MES:
            c *= 1 + R[s]
            closes.append(c)
        con.executemany("INSERT INTO dwd_daily_bar VALUES (?,?,?,?,?,?,?,?,?)",
                        [(s, dt, c, c, c, c, 1000, 1000 * c, 0.0)
                         for dt, c in zip(MES, closes)])
    con.executemany("INSERT INTO dwd_daily_bar VALUES (?,?,?,?,?,?,?,?,?)",
                    [(BENCH, dt, 100.0, 100.0, 100.0, 100.0, 1000, 100000.0, 0.0)
                     for dt in MES])
    con.executemany("INSERT INTO dim_security VALUES (?,?,?,?,?)",
                    [(s, s, "XSHG", "STOCK", f"IND{(k - 1) % 4 + 1}")
                     for k, s in enumerate(SEC, start=1)])
    rows = [(dt, s, fid, _fv(fid, i, k))
            for fid in ("F_good", "F_decay", "F_bad")
            for i, dt in enumerate(MES) for k, s in enumerate(SEC, start=1)]
    con.executemany("INSERT INTO factor_value_daily (dt, secid, factor_id, value)"
                    " VALUES (?,?,?,?)", rows)
    con.execute(EVAL_TABLE)
    return con


@pytest.fixture()
def hyp_root(tmp_path):
    """假设注册表（含 refuted 反例）。"""
    (tmp_path / "hypotheses.yaml").write_text(
        "H-VAL-001:\n  statement: 价值溢价：低估值中长期正超额\n  status: open\n"
        "H-REFUTED:\n  statement: 已证伪假设（不应可激活）\n  status: refuted\n",
        encoding="utf-8")
    return tmp_path


class _Meta:
    def __init__(self, fid, direction=1, version="1.0.0", hyp="H-VAL-001",
                 source="synthetic/test.yaml"):
        self.id, self.direction, self.type = fid, direction, "alpha"
        self.name, self.version, self.family, self.engine = fid, version, "test", "synthetic"
        self.hypothesis_id, self.source = hyp, source
        self.body, self.transform, self.params = "synthetic", [], {}
        self.lookback_days, self.depends, self.universe = 0, [], ["CN"]
        self.status = STATUS_DRAFT


@pytest.fixture()
def metas():
    return {f: _Meta(f) for f in ("F_good", "F_decay", "F_bad")}


@pytest.fixture()
def gov_report(lc_con, metas):
    """真链路治理报告（④⑤⑥ 凭据）：仅 F_good 入批（单因子批次 → ⑤ 自然通过）。

    全批跑会因 F_decay/F_bad 与 F_good 秩结构同族被 ⑤ 连通组拖成 alternative——
    那是 ⑤ 的正确行为，但门禁测试需要正例凭据。
    """
    return run_governance_on_con(lc_con, {"F_good": metas["F_good"]}, wf=WF,
                                 lockbox_months=LB, materialize_values=False)


def _seed_eval(con, fid, ic=1.0):
    """③ 凭据种子：meta_factor_eval 1M 行。"""
    con.execute("INSERT INTO meta_factor_eval VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                [fid, "1.0.0", MES[-1], "test", 1, ic, None, None, 32, 5,
                 None, None, None, None, "{}", "2026-10-12"])


# ── 状态机全路径（DoD 1 前半） ──
def test_state_machine_all_paths():
    m = _Meta("F_good")
    assert m.status == STATUS_DRAFT
    set_status(m, STATUS_ACTIVE)
    set_status(m, STATUS_DEPRECATED)
    assert m.status == STATUS_DEPRECATED
    with pytest.raises(RegistryError):        # 终态无出边
        set_status(m, STATUS_ACTIVE)
    m2 = _Meta("F_good")
    with pytest.raises(RegistryError):        # 禁止跳级 draft → deprecated
        set_status(m2, STATUS_DEPRECATED)


# ── 8.1 激活门禁：违例矩阵（DoD 1：未过门禁无法 active） ──
def test_activate_gate_blocks(lc_con, gov_report, hyp_root, metas):
    _seed_eval(lc_con, "F_good")
    # ① 内存手搓（source 空）
    ghost = _Meta("F_good", source="")
    with pytest.raises(LifecycleError, match="注册中心"):
        activate(lc_con, ghost, gov_report, root=hyp_root)
    # ② 非 draft
    m = metas["F_good"]
    set_status(m, STATUS_ACTIVE)
    with pytest.raises(LifecycleError, match="仅对 draft"):
        activate(lc_con, m, gov_report, root=hyp_root)
    m.status = STATUS_DRAFT                   # 白盒恢复（测试专用，绕过状态机）
    # ③ version 非语义化
    m.version = "v1"
    with pytest.raises(LifecycleError, match="语义化"):
        activate(lc_con, m, gov_report, root=hyp_root)
    m.version = "1.0.0"
    # ④ hypothesis_id 缺失 / 不在注册表 / refuted
    m.hypothesis_id = None
    with pytest.raises(LifecycleError, match="无来源"):
        activate(lc_con, m, gov_report, root=hyp_root)
    m.hypothesis_id = "H-NOPE"
    with pytest.raises(LifecycleError, match="不在假设注册表"):
        activate(lc_con, m, gov_report, root=hyp_root)
    m.hypothesis_id = "H-REFUTED"
    with pytest.raises(LifecycleError, match="证伪"):
        activate(lc_con, m, gov_report, root=hyp_root)
    m.hypothesis_id = "H-VAL-001"
    # ⑤ 治理凭据：缺失 / 不在报告 / 非 pass
    with pytest.raises(LifecycleError, match="M4 治理报告"):
        activate(lc_con, m, None, root=hyp_root)
    with pytest.raises(LifecycleError, match="不在治理报告"):
        activate(lc_con, m, {"verdicts": []}, root=hyp_root)
    v_bad = {**gov_report, "verdicts": [
        {**next(x for x in gov_report["verdicts"] if x["factor_id"] == "F_good"),
         "overall": "fail"}]}
    with pytest.raises(LifecycleError, match="≠ pass"):
        activate(lc_con, m, v_bad, root=hyp_root)
    # ⑥ 分关复核：overall pass 但单关不过 → 拒
    vv = next(x for x in gov_report["verdicts"] if x["factor_id"] == "F_good")
    v_sneak = {**gov_report, "verdicts": [{**vv, "overall": "pass",
                                           "time_gate": {**vv["time_gate"], "pass": False}}]}
    with pytest.raises(LifecycleError, match="time_gate"):
        activate(lc_con, m, v_sneak, root=hyp_root)
    # ⑦ PIT 红线
    v_pit = {**gov_report, "verdicts": [
        {**vv, "pit": {**vv["pit"], "violations": 3}}]}
    with pytest.raises(LifecycleError, match="PIT"):
        activate(lc_con, m, v_pit, root=hyp_root)
    # ⑧ ③ 凭据：无记录 / 对齐 IC 非正（原始 IC × direction 对齐判定）
    con2 = duckdb.connect(":memory:")
    con2.execute(EVAL_TABLE)
    with pytest.raises(LifecycleError, match="③ 检验记录缺失"):
        activate(con2, m, gov_report, root=hyp_root)
    con2.execute("DELETE FROM meta_factor_eval")
    _seed_eval(con2, "F_good", ic=-0.2)
    with pytest.raises(LifecycleError, match="③ 对齐 IC"):
        activate(con2, m, gov_report, root=hyp_root)   # +1 × −0.2 → 拒
    m_neg = _Meta("F_good", direction=-1)
    activate(con2, m_neg, gov_report, root=hyp_root)   # −1 × −0.2 = +0.2 → 过（对齐口径）
    assert m_neg.status == STATUS_ACTIVE
    assert m.status == STATUS_DRAFT           # 全部违例后仍未激活


def test_activate_gate_pass_and_archive(lc_con, gov_report, hyp_root, metas):
    _seed_eval(lc_con, "F_good")
    m = metas["F_good"]
    out = activate(lc_con, m, gov_report, root=hyp_root, actor="tester")
    assert m.status == STATUS_ACTIVE
    assert out["def_hash"] and out["hypothesis_id"] == "H-VAL-001"
    row = lc_con.execute("SELECT factor_id, version, def_hash, status"
                         " FROM meta_factor_archive WHERE factor_id='F_good'"
                         " ORDER BY activated_at DESC LIMIT 1").fetchone()
    assert row == ("F_good", "1.0.0", out["def_hash"], STATUS_ACTIVE)
    h1, _ = def_hash(m)
    h2, _ = def_hash(m)
    assert h1 == h2 == out["def_hash"]        # hash 确定性


# ── 8.3 版本冻结（DoD 3：重跑 bit 级一致凭据） ──
def test_version_freeze(lc_con, gov_report, hyp_root, metas):
    _seed_eval(lc_con, "F_good")
    m = metas["F_good"]
    activate(lc_con, m, gov_report, root=hyp_root)
    assert verify_frozen(lc_con, m)["frozen"] is True
    # 口径漂移未 bump version → 拒绝（保证旧报告重跑 bit 级一致的守门语义）
    m.body = "synthetic -- CHANGED"
    with pytest.raises(LifecycleError, match="定义漂移"):
        verify_frozen(lc_con, m)
    # 正确流程：bump 新 version → 无快照 → 须重新走门禁
    m.body = "synthetic"
    m.version = "2.0.0"
    with pytest.raises(LifecycleError, match="无激活快照"):
        verify_frozen(lc_con, m)


def test_freeze_bitwise_rerun(lc_con, gov_report, hyp_root, metas):
    """DoD 3：引用 v1.0.0 的报告重跑 bit 级一致——重算行集 digest 逐位一致。"""
    _seed_eval(lc_con, "F_good")
    m = metas["F_good"]
    activate(lc_con, m, gov_report, root=hyp_root)
    verify_frozen(lc_con, m)                  # 重跑前置校验（未漂移才放行）

    def digest():
        rows = lc_con.execute("SELECT dt, secid, value FROM factor_value_daily"
                              " WHERE factor_id='F_good' ORDER BY dt, secid").fetchall()
        return json.dumps(rows, ensure_ascii=False, default=str)
    d1 = digest()
    lc_con.execute("DELETE FROM factor_value_daily WHERE factor_id='F_good'")
    con2_rows = [(dt, s, "F_good", _fv("F_good", i, k))
                 for i, dt in enumerate(MES) for k, s in enumerate(SEC, start=1)]
    lc_con.executemany("INSERT INTO factor_value_daily (dt, secid, factor_id, value)"
                       " VALUES (?,?,?,?)", con2_rows)
    assert digest() == d1                     # bit 级一致
    # 漂移定义被冻结拦截 → 不可能产出不一致的「v1.0.0 重跑」
    m.body = "synthetic -- CHANGED"
    with pytest.raises(LifecycleError):
        verify_frozen(lc_con, m)


# ── 8.2 季度巡检（DoD 2：注入衰减 → weakening） ──
@pytest.fixture()
def active_metas(metas):
    """巡检输入：三因子全部白盒置 active（巡检职责与门禁解耦的测试口径）。"""
    for m in metas.values():
        m.status = STATUS_ACTIVE
    return metas


def test_inspection_weakening_injection(lc_con, active_metas, hyp_root):
    """DoD 2：人为回填衰减 IC（F_decay 后 15 月退化）→ 单次巡检即触发 weakening。"""
    rep = quarterly_inspection(lc_con, active_metas, root=hyp_root)
    v = {r["factor_id"]: r for r in rep["verdicts"]}
    rd = v["F_decay"]["metrics"]["rolling"]
    assert rd["prior_mean"] > rd["recent_mean"] > 0
    assert rd["decay"] >= INSPECT_DECAY_MAX
    assert v["F_decay"]["verdict"] == "weakening"
    acts = v["F_decay"]["actions"]
    assert any(a["type"] == "hypothesis_review" and a["priority"] == "normal"
               and a["statement"] for a in acts)          # 假设复查步自动带出
    assert any(a["type"] == "warning" for a in acts)      # 8.4 标警


def test_inspection_invalid_auto_deprecate(lc_con, active_metas, hyp_root):
    rep = quarterly_inspection(lc_con, active_metas, root=hyp_root)
    v = {r["factor_id"]: r for r in rep["verdicts"]}
    assert v["F_bad"]["verdict"] == "invalid"
    assert v["F_bad"]["status_after"] == STATUS_DEPRECATED   # 8.4 自动摘除
    assert any(a["type"] == "removed_from_pool"
               and a["impacted_strategies"] == [] for a in v["F_bad"]["actions"])
    assert any(a["type"] == "hypothesis_review" and a["priority"] == "high"
               for a in v["F_bad"]["actions"])
    assert active_metas["F_bad"].status == STATUS_DEPRECATED


def test_inspection_healthy_and_spot_check(lc_con, active_metas, hyp_root):
    rep = quarterly_inspection(lc_con, active_metas, root=hyp_root)
    v = {r["factor_id"]: r for r in rep["verdicts"]}
    assert v["F_good"]["verdict"] == "healthy"
    assert rep["summary"] == {"healthy": 1, "weakening": 1, "invalid": 1}
    # healthy 20% 确定性抽查：healthy_ids=[F_good] → max(1, round(0.2)) = 1 → 抽中
    assert rep["spot_check"] == ["F_good"]
    assert any(a["type"] == "spot_check" for a in v["F_good"]["actions"])
    assert factor_health(lc_con) == {"F_bad": "invalid", "F_decay": "weakening",
                                     "F_good": "healthy"}


def test_inspection_skips_non_active_and_md(lc_con, metas, hyp_root, tmp_path):
    rep = quarterly_inspection(lc_con, metas, root=hyp_root, out_dir=tmp_path)
    assert rep["verdicts"] == [] and len(rep["skipped"]) == 3   # draft 显式跳过
    md = (tmp_path / f"m5_inspection_{rep['as_of']}.md").read_text(encoding="utf-8")
    assert "## 巡检看板" in md and "## 待答项与动作" in md
    assert (tmp_path / f"m5_inspection_{rep['as_of']}.json").exists()


# ── 8.3/8.4 引用影响面与降级 ──
def test_refs_and_deactivate_impact(lc_con, gov_report, hyp_root, metas):
    _seed_eval(lc_con, "F_good")
    m = metas["F_good"]
    activate(lc_con, m, gov_report, root=hyp_root)
    register_ref(lc_con, "strat_lowvol", "F_good", "1.0.0")
    register_ref(lc_con, "strat_per", "F_good", "1.0.0")
    assert who_references(lc_con, "F_good") == [
        {"strategy_id": "strat_lowvol", "version": "1.0.0"},
        {"strategy_id": "strat_per", "version": "1.0.0"}]
    with pytest.raises(LifecycleError, match="影响面评估"):
        deactivate(lc_con, m)                 # 有引用未确认 → 拒
    out = deactivate(lc_con, m, confirm_impact=True)
    assert m.status == STATUS_DEPRECATED
    assert out["impacted_strategies"] and out["removed_from_pool"] is True
    # 降级后再 deactivate → 拒（终态）
    with pytest.raises(LifecycleError, match="仅对 active"):
        deactivate(lc_con, m)


# ── 假设注册表加载 ──
def test_load_hypotheses(hyp_root):
    hyp = load_hypotheses(hyp_root)
    assert hyp["H-VAL-001"]["status"] == "open"
    assert hyp["H-REFUTED"]["status"] == "refuted"
    (hyp_root / "hypotheses.yaml").write_text("H-BAD:\n  status: open\n", encoding="utf-8")
    with pytest.raises(LifecycleError, match="statement"):
        load_hypotheses(hyp_root)
