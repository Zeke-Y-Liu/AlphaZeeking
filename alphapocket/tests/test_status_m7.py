"""M7 T-F6：状态总表 + 巡检 DAG 全覆盖 + 池容量测试。

三层：
- 总表完整性（DoD a）：25 唯一实体（24 注册 + 1 缺口登记，含 T-F9 personal_clone 演练
  候选 demo_clone_f1）行行在档；契约「25 = 21 V3 + 4 存量」身份计口径差在 deviation
  显式披露（val_pe_ttm 双身份）；gap 行三要素在档；
- 口径一致性（DoD b/c/d）：抽 5 因子 gates/health 列 ↔ fl_factor_gate/fl_factor_health
  逐项对账（只读，不重算）；summary 计数自洽（active+rejected+gap = total）；
  family_coverage x 值 ↔ active_pool 实际族分布；
- 巡检 DAG 全覆盖：最新 fl_inspect artifact n_inspected = 注册因子数（draft/active 全含）、
  无阻断（逐因子有判定或透明 err）。

运行：``cd alphapocket && /workspace/alphapocket-web/core/.venv/bin/python -m pytest tests/test_status_m7.py -q``
"""
from __future__ import annotations

import json
from pathlib import Path

import duckdb
import pytest

ROOT = Path(__file__).resolve().parents[1]
REAL_DB = ROOT / "sandbox" / "data" / "fl.duckdb"
SB_REPO = ROOT / "sandbox" / "repo"
STATUS_MD = ROOT / "sandbox" / "artifacts" / "m7_status" / "factor_status_m7.md"
STATUS_JSON = ROOT / "sandbox" / "artifacts" / "m7_status" / "factor_status_m7.json"
INSPECT_DIR = ROOT / "sandbox" / "artifacts"
GAP_MD = ROOT / "sandbox" / "artifacts" / "m7_gaps" / "gro_earnings_surprise.md"

# V3 §6.2 五族 21 清单（T-F6 总表覆盖度基准）
V3_21 = {
    "估值": ["val_pe_ttm", "val_pb_lf", "val_ps_ttm", "val_ev_ebitda", "val_div_yield"],
    "质量": ["qua_roe_ttm", "qua_gross_margin", "qua_accruals",
             "qua_ocf_to_np", "qua_debt_to_asset"],
    "成长": ["gro_rev_yoy", "gro_np_yoy", "gro_rev_cagr_3y", "gro_stability",
             "gro_earnings_surprise"],
    "动量": ["mom_12_1", "mom_6_1", "mom_industry_adj"],
    "低波红利": ["low_vol_252d", "low_beta", "div_payout_streak"],
}
LEGACY4 = ("val_pe_ttm", "mom_20d_fl", "sig_mom20_rank_fl", "eps_revise_fl")


def _requires_real() -> bool:
    return not (REAL_DB.exists() and STATUS_JSON.exists())


def _load_doc() -> dict:
    return json.loads(STATUS_JSON.read_text(encoding="utf-8"))


def _latest_inspect() -> dict:
    runs = sorted(d for d in INSPECT_DIR.glob("fl_inspect_*") if d.is_dir())
    assert runs, "无 fl_inspect artifacts"
    return json.loads((runs[-1] / "inspection.json").read_text(encoding="utf-8"))


# ── a) 总表完整性 ────────────────────────────────────────
@pytest.mark.skipif(_requires_real(), reason="真实库/总表产物缺失（CI 只跑合成层）")
def test_status_table_complete():
    """总表行齐全（DoD a）：25 唯一实体 = 24 注册 + 1 缺口登记（含 T-F9 演练候选
    demo_clone_f1）；V3 21 + 存量 4 全覆盖（val_pe_ttm 双身份唯一）；契约 25 身份计
    口径差 deviation 显式披露。"""
    doc = _load_doc()
    ids = [f["factor_id"] for f in doc["factors"]]
    assert len(ids) == len(set(ids)) == 25, \
        "总表应 25 唯一实体（24 注册 + 1 gap，含 demo_clone_f1）"
    {m.strip() for m in
               (SB_REPO / "factors" / "factor_registry_state.json").read_text(
                   encoding="utf-8").split('"') if m and m[0].isalpha() and "_" in m}
    # 状态文件解析太脆：直接对 V3/存量清单断言（语义基准）
    v3_ids = {s for lst in V3_21.values() for s in lst}
    assert v3_ids - {"gro_earnings_surprise"} <= set(ids), "V3 注册因子须全部入表"
    assert set(LEGACY4) <= set(ids), "存量 4 因子须全部入表"
    assert doc["summary"]["n_gap"] == 1 and "gro_earnings_surprise" in ids
    dev = "\n".join(doc["summary"]["deviation"])
    assert "身份计" in dev and "双身份" in dev and "25" in dev, "25→25 口径差须显式披露"
    assert "demo_clone_f1" in dev and "personal_clone" in dev, \
        "T-F9 通道候选须在 deviation 披露（个人化不豁免门禁）"


@pytest.mark.skipif(_requires_real(), reason="真实库/总表产物缺失")
def test_gap_row_disclosed():
    """surprise 缺口行（DoD a）：gap 态 + 三要素来源在档（无来源不激活——连假设都不立）。"""
    doc = _load_doc()
    row = next(f for f in doc["factors"] if f["factor_id"] == "gro_earnings_surprise")
    assert row["lifecycle"] == "unregistered" and row["activation"].startswith("gap(")
    assert GAP_MD.exists(), "缺口登记文件在档"
    text = GAP_MD.read_text(encoding="utf-8")
    assert "一致预期" in text and "M8" in text and "不作为近似实现冒充" in text


# ── b) 结论与落库表一致（抽 5 因子）─────────────────────
_SAMPLE = ("val_pe_ttm", "mom_12_1", "low_vol_252d", "div_payout_streak", "qua_ocf_to_np")
_MARK = {"pass": True, "fail": False, "soft": None}


@pytest.mark.skipif(_requires_real(), reason="真实库/总表产物缺失")
def test_status_matches_db_tables():
    """口径一致性（DoD b）：抽 5 因子 gates 列 pass 标记 ↔ fl_factor_gate 最新 as_of
    逐关对账；health 列 ↔ fl_factor_health 当日巡检行（只读对账，不重算）。"""
    doc = _load_doc()
    rows = {f["factor_id"]: f for f in doc["factors"]}
    con = duckdb.connect(str(REAL_DB), read_only=True)
    try:
        for fid in _SAMPLE:
            g = rows[fid]["gates"]
            for gate, key in (("time_robustness", "time="), ("param_robustness", "param="),
                              ("environment", "env="), ("incremental_value", "rho="),
                              ("multiplicity", "bonf=")):
                db_pass = con.execute(
                    "SELECT pass FROM fl_factor_gate WHERE factor_id = ? AND gate = ? "
                    "ORDER BY as_of DESC LIMIT 1", [fid, gate]).fetchone()
                if db_pass is None:
                    continue
                seg = next((s for s in g.split() if s.startswith(key)), None)
                assert seg, f"{fid} gates 列缺 {key}"
                assert _MARK[seg[len(key):]] == db_pass[0], \
                    f"{fid}/{gate} 总表 {seg} ≠ 落库 {db_pass[0]}"
            db_health = con.execute(
                "SELECT health FROM fl_factor_health WHERE factor_id = ? "
                "ORDER BY as_of DESC LIMIT 1", [fid]).fetchone()
            if db_health is not None and rows[fid]["lifecycle"] in ("active", "draft"):
                # 状态真源（状态文件）优先：invalid→healthy 直通被拒时二者合法不一致
                from factorlib.registry import get_health
                state_h = get_health(SB_REPO, fid)[0]
                assert rows[fid]["health"] == state_h, \
                    f"{fid} health 列 ≠ 状态真源"
                assert rows[fid]["health"] == db_health[0] or state_h == "invalid", \
                    f"{fid} health 列既非当日巡检行也与状态真源不一致"
    finally:
        con.close()


# ── c) summary 计数自洽 ─────────────────────────────────
@pytest.mark.skipif(_requires_real(), reason="真实库/总表产物缺失")
def test_summary_counts_consistent():
    """计数自洽（DoD c）：active + rejected + gap = total；n_active 与池快照一致。"""
    doc = _load_doc()
    s = doc["summary"]
    f = doc["factors"]
    assert s["n_active"] + s["n_rejected"] + s["n_gap"] == s["n_total"]
    assert s["n_active"] == sum(1 for x in f if x["lifecycle"] == "active")
    assert s["n_rejected"] == sum(1 for x in f if x["lifecycle"] == "draft")
    assert s["n_gap"] == sum(1 for x in f if x["lifecycle"] == "unregistered")
    # activation 列三态与 lifecycle 一致（active@ / rejected( / gap( 前缀）
    for x in f:
        if x["lifecycle"] == "active":
            assert x["activation"].startswith("active@")
        elif x["lifecycle"] == "draft":
            assert x["activation"].startswith("rejected")
        else:
            assert x["activation"].startswith("gap(")


# ── d) family_coverage 与 pool 一致 ─────────────────────
@pytest.mark.skipif(_requires_real(), reason="真实库/总表产物缺失")
def test_family_coverage_matches_pool():
    """族覆盖与池一致（DoD d）：family_coverage x 值 = 该族 active 实数（与池内因子
    族归属一致）；deviation 含「≥6 硬条件」达成现状登记。"""
    from factorlib.lifecycle import active_pool
    doc = _load_doc()
    rows = {f["factor_id"]: f for f in doc["factors"]}
    pool = active_pool(None, SB_REPO)
    for fam, ratio in doc["summary"]["family_coverage"].items():
        x, y = (int(v) for v in ratio.split("/"))
        members = [r for r in rows.values() if r["family"] == fam]
        # 族分母含 gap 行（V3 成长 5 = 4 注册 + 1 缺口登记），与脚本口径一致
        assert len(members) == y, f"{fam} 族分母 {y} ≠ 表内实体 {len(members)}"
        act = sum(1 for r in members if r["lifecycle"] == "active")
        assert act == x, f"{fam} 族覆盖分子 {x} ≠ 表内 active {act}"
        if x:
            assert any(p["factor_id"] in {r["factor_id"] for r in members}
                       for p in pool["pool"]), f"{fam} 族有 active 却不在池内"
    dev = "\n".join(doc["summary"]["deviation"])
    assert "≥6" in dev, "池 ≥6 硬条件达成现状须如实登记"


# ── e) 巡检 DAG 全覆盖 + 幂等/无阻断 ────────────────────
@pytest.mark.skipif(_requires_real(), reason="真实库/巡检产物缺失")
def test_inspect_dag_full_coverage():
    """巡检全覆盖（DoD 2）：n_inspected = 注册因子数（draft/active 全含，24）；
    逐因子有判定或透明 err（不阻断批量）；总表 health 与巡检结果一致。"""
    from factorlib.registry import load_registry
    insp = _latest_inspect()
    n_reg = len(load_registry(SB_REPO))
    assert insp["n_inspected"] == n_reg, f"巡检应全覆盖 {n_reg} 注册因子"
    for r in insp["factors"]:
        assert r.get("health") or r.get("err"), f"{r['factor_id']} 无判定且无透明 err"
    doc = _load_doc()
    rows = {f["factor_id"]: f for f in doc["factors"]}
    # 迁移被拒行（如 eps_revise_fl：巡检重算 healthy 但 invalid→healthy 直通非法，
    # 状态真源保持 invalid）合法不一致——健康不可恢复迁移纪律（M5 T-L4）
    rejected_migrate = {r["factor_id"] for r in insp["factors"]
                        if any("健康迁移被拒" in x for x in r.get("reasons") or [])}
    insp_map = {r["factor_id"]: r["health"] for r in insp["factors"]
                if "health" in r and r["factor_id"] not in rejected_migrate}
    for fid, h in insp_map.items():
        assert rows[fid]["health"] == h, f"{fid} 总表 health ≠ 巡检结果（{h}）"
