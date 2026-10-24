"""M5 T-L4 巡检编排层测试：golden fl 库 + 注入 eval 序列/数据断供，驱动 run_inspection。

手工对账（DoD）：构造已知 IC 序列（前 24 月 0.2 + 后 24 月 0.05）→ decay = 0.25 → weakening；
演练注入尾部 ×0.3 → weakening 但不落盘不写状态文件；单因子失败透明降级 unknown 不阻断。

运行：``cd alphapocket && python -m pytest tests/test_factor_inspect.py -q``
"""
from __future__ import annotations

import json
from pathlib import Path

import duckdb
import pytest

from applib.factor_eval_fl import save_eval_report
from applib.factor_inspect import (
    _metric_coverage,
    _metric_ic_decay,
    run_inspection,
)
from factorlib.registry import load_factor, set_status

_DAY = "2026-10-11"
_FID = "mom_20d_fl"          # direction=high → desc：directed = raw IC


class _FakeCtx:
    """最小 Ctx 替身：run_inspection 只消费 sandbox_dir/data/fl.duckdb、local_repo、artifacts_dir。"""

    def __init__(self, sandbox: Path, repo: Path, artifacts: Path):
        self.sandbox_dir = sandbox
        self.local_repo = repo
        self.artifacts_dir = artifacts


def _month_ends(n: int, start_year: int = 2021) -> list[str]:
    """n 个月末日期串（2021-01 起逐月，与月度截面协议同形态）。"""
    out, y, m = [], start_year, 1
    for _ in range(n):
        out.append(f"{y:04d}-{m:02d}-28")
        m += 1
        if m > 12:
            y, m = y + 1, 1
    return out


@pytest.fixture
def insp(fl_db, fl_repo, tmp_path):
    """sandbox 布局 + mom_20d_fl active 物化；返回 (ctx, db, repo)。"""
    db, _ = fl_db
    sandbox = tmp_path / "sandbox"
    (sandbox / "data").mkdir(parents=True)
    db2 = sandbox / "data" / "fl.duckdb"
    db.rename(db2)                       # 挪进 Ctx 目录约定（db.parent = sandbox/data）
    set_status(fl_repo, _FID, "active", who="t", why="巡检测试",
               gate={"ok": True, "reasons": []})
    from factorlib.materialize import materialize
    materialize(load_factor(fl_repo, _FID), db2, mode="full")
    return _FakeCtx(sandbox, fl_repo, tmp_path / "artifacts"), db2, fl_repo


def _craft_eval(db: Path, repo: Path, series: list[tuple[str, float]],
                fid: str = _FID) -> None:
    """向 fl_factor_eval 注入指定 h1 IC 序列的检验报告（report_json 优先来源）。"""
    from factorlib.materialize import latest_version
    con = duckdb.connect(str(db))
    try:
        ver = latest_version(con, fid)
        lid = con.execute("SELECT max(lineage_id) FROM fl_factor_value_daily "
                          "WHERE factor_id = ? AND factor_version = ?",
                          [fid, ver]).fetchone()[0]
    finally:
        con.close()
    direction = "asc" if load_factor(repo, fid).direction == "low" else "desc"
    rep = {"factor_id": fid, "factor_version": ver, "lineage_id": lid,
           "as_of": "2024-12-31", "code_version": "test", "direction": direction,
           "reports": {1: {"n_periods": len(series),
                           "ic": {"series": series, "mean": None, "ic_ir": None,
                                  "nw_t": None, "directed_mean": None},
                           "top_excess_per_period": None, "monotonicity": None,
                           "top_turnover": None}}}
    save_eval_report(rep, db)


def _state(repo: Path) -> dict:
    p = repo / "factors" / "factor_registry_state.json"
    return json.loads(p.read_text(encoding="utf-8"))


def _health_rows(db: Path) -> list[tuple]:
    con = duckdb.connect(str(db), read_only=True)
    try:
        has = con.execute("SELECT count(*) FROM information_schema.tables "
                          "WHERE table_name = 'fl_factor_health'").fetchone()[0]
        if not has:
            return []                                # drill 演练从不建表 → 视为无落库
        return con.execute("SELECT factor_id, as_of, health FROM fl_factor_health "
                           "ORDER BY factor_id").fetchall()
    finally:
        con.close()


# ── 手工对账（DoD 主战场）────────────────────────────────
def test_manual_reconciliation_decay_weakening(insp):
    """前 24 月 0.2 + 后 24 月 0.05 → decay=0.25 → weakening；落库 + 状态文件同步。"""
    ctx, db, repo = insp
    dts = _month_ends(48)
    _craft_eval(db, repo, [(d, 0.2) for d in dts[:24]] + [(d, 0.05) for d in dts[24:]])
    out = run_inspection(ctx, factors=[_FID], as_of=_DAY)
    f = out["factors"][0]
    assert f["health"] == "weakening"
    assert f["metrics"]["ic_decay"] == pytest.approx(0.25)
    assert f["metrics"]["ic_recent_mean"] == pytest.approx(0.05)
    assert f["metrics"]["ic_series_source"] == "fl_factor_eval.report_json"
    assert any("IC 衰减比" in r for r in f["reasons"])
    rows = _health_rows(db)
    assert len(rows) == 1 and rows[0][2] == "weakening" and rows[0][1] == _DAY
    assert _state(repo)[_FID]["health"] == "weakening"
    assert out["active_vs_cap"]["cap"] == 30
    assert Path(out["artifacts"]["md"]).exists() and "巡检" in Path(out["artifacts"]["md"]).read_text(encoding="utf-8")


def test_recent_mean_le_zero_invalid(insp):
    """近 24 月 directed IC 均值 ≤ 0 → invalid（衰减比同样触发，就低归并）。"""
    ctx, db, repo = insp
    dts = _month_ends(48)
    _craft_eval(db, repo, [(d, 0.2) for d in dts[:24]] + [(d, -0.05) for d in dts[24:]])
    out = run_inspection(ctx, factors=[_FID], as_of=_DAY)
    assert out["factors"][0]["health"] == "invalid"
    assert _state(repo)[_FID]["health"] == "invalid"


def test_coverage_drift_weakening(insp):
    """指标2：近 3 月覆盖度减半（模拟数据断供）→ 覆盖比 < 0.8 → weakening。"""
    ctx, db, repo = insp
    _craft_eval(db, repo, [(d, 0.2) for d in _month_ends(48)])   # IC 全史健康
    con = duckdb.connect(str(db))
    try:
        con.execute("DELETE FROM fl_factor_value_daily WHERE factor_id = ? "
                    "AND dt >= DATE '2021-04-01' AND secid IN "
                    "('600001.SH','600002.SH','600003.SH','600004.SH','600005.SH')",
                    [_FID])
    finally:
        con.close()
    out = run_inspection(ctx, factors=[_FID], as_of=_DAY)
    f = out["factors"][0]
    assert f["health"] == "weakening"
    assert f["metrics"]["coverage_ratio"] < 0.8
    assert any("覆盖度" in r for r in f["reasons"])


# ── 演练注入（不落盘不写状态）────────────────────────────
def test_negative_baseline_decay_is_unknown_not_weakening():
    """负基线守卫：此前全史均值 ≤ 0 时比值无定义 → None（不产出误导性负比值）。

    真实库案例（2026-10-11 巡检）：mom_20d_fl 全史均值 -0.0031、近窗 +0.012，
    负比值 -3.87 曾被误读为衰减——实际近期改善。
    """
    s = list(zip(_month_ends(36), [-0.01] * 24 + [0.02] * 12))
    decay, recent = _metric_ic_decay(s, window=24)
    assert decay is None                              # 基线 -0.01 ≤ 0 → 比值无定义
    assert recent == pytest.approx(0.005)             # 近窗 = 12×0.02 + 12×(-0.01)
    assert _metric_ic_decay([], 24) == (None, None)
    assert _metric_coverage([], 3) is None


def test_drill_inject_decay_no_persist(insp):
    """尾部 24 月 ×0.3 → weakening；drill 不落 fl_factor_health、不写状态文件。"""
    ctx, db, repo = insp
    _craft_eval(db, repo, [(d, 0.2) for d in _month_ends(48)])   # 全史健康 → 基线 healthy
    out = run_inspection(ctx, factors=[_FID], as_of=_DAY, inject_decay=[_FID])
    assert out["drill"] is True
    f = out["factors"][0]
    assert f["health"] == "weakening"                            # 注入后 0.06/0.2=0.3 < 0.5
    assert f["metrics"]["ic_recent_mean"] == pytest.approx(0.06)
    assert "drill 注入" in f["metrics"]["ic_series_source"]
    assert _health_rows(db) == []                                # 不落库
    assert "health" not in _state(repo)[_FID]                    # 不写状态文件
    assert "演练态" in Path(out["artifacts"]["md"]).read_text(encoding="utf-8")


def test_non_drill_baseline_healthy(insp):
    """未注入基线：同序列非演练 → healthy 且正常落库（与 drill 结果对照）。"""
    ctx, db, repo = insp
    _craft_eval(db, repo, [(d, 0.2) for d in _month_ends(48)])
    out = run_inspection(ctx, factors=[_FID], as_of=_DAY)
    assert out["drill"] is False and out["factors"][0]["health"] == "healthy"
    assert len(_health_rows(db)) == 1


# ── 幂等与透明降级 ───────────────────────────────────────
def test_rerun_same_as_of_idempotent(insp):
    """同 as_of 重跑：fl_factor_health 行数不变；health_history 去重不重复追加。"""
    ctx, db, repo = insp
    _craft_eval(db, repo, [(d, 0.2) for d in _month_ends(24)] + [(d, 0.05)
                                                                 for d in _month_ends(48)[24:]])
    run_inspection(ctx, factors=[_FID], as_of=_DAY)
    n_rows_1 = len(_health_rows(db))
    hist_1 = _state(repo)[_FID].get("health_history", [])
    run_inspection(ctx, factors=[_FID], as_of=_DAY)
    assert len(_health_rows(db)) == n_rows_1 == 1
    assert _state(repo)[_FID].get("health_history", []) == hist_1   # 同 (to, as_of) 不重复


def test_batch_failure_transparent_degradation(insp):
    """单因子失败（未物化/不存在）→ unknown + 异常摘要，不阻断批量其余因子判定。"""
    ctx, db, repo = insp
    _craft_eval(db, repo, [(d, 0.2) for d in _month_ends(48)])
    out = run_inspection(ctx, factors=[_FID, "no_such_factor"], as_of=_DAY)
    assert out["n_inspected"] == 2
    by_id = {f["factor_id"]: f for f in out["factors"]}
    assert by_id[_FID]["health"] == "healthy"                    # 好因子照常判定
    bad = by_id["no_such_factor"]
    assert bad["health"] == "unknown"
    assert any("巡检异常" in r for r in bad["reasons"])
    assert len(_health_rows(db)) == 2                            # unknown 也落库（透明）


def test_default_active_universe_and_recompute_fallback(insp):
    """factors=None 取全部 active（未物化的透明 unknown）；无 eval 记录走现场重算。"""
    ctx, _db, _repo = insp
    out = run_inspection(ctx, as_of=_DAY)
    assert out["n_inspected"] == out["active_vs_cap"]["active"]
    me = next(f for f in out["factors"] if f["factor_id"] == _FID)
    assert me["metrics"]["ic_series_source"].startswith("recompute(factor_eval_fl)")
    assert me["health"] in ("healthy", "weakening", "invalid", "unknown")
