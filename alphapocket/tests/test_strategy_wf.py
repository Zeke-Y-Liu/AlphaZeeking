"""M6 T-C4 策略层 walk-forward 测试：扩展窗口滚动 + lockbox 一次性 + 敏感性网格。

折叠手工对账（golden）：30 月样本、train_min=24/valid=6/step=6 → t=24 时 24+6=30 ≤ 30
恰 1 折（train=[0,24) 扩展窗口、valid=[24,30)）；t=30 时 30+6>30 停 → 对齐 M4 CP-0024 口径。
集成用 golden repo（6 个月 2021-01..06）+ 小 cfg：core=01..04 → 2 折、lockbox 封存 05/06。
运行：``cd alphapocket && python -m pytest tests/test_strategy_wf.py -q``
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from applib.strategy_wf import (StrategyWfError, audit_folds, lockbox_verify,
                                plan_folds, rank_stable_from, select_method,
                                sensitivity_grid, strategy_walk_forward,
                                walk_forward_folds, _split_lockbox)
from factorlib import load_factor, set_status
from factorlib.materialize import materialize


def _months(n: int, y0: int = 2021, m0: int = 1) -> list[str]:
    """自 y0-m0 起连续 n 个月的月键（YYYY-MM）。"""
    out, y, m = [], y0, m0
    while len(out) < n:
        out.append(f"{y:04d}-{m:02d}")
        m += 1
        if m > 12:
            y, m = y + 1, 1
    return out


# ── 纯函数：折叠规划（DoD a）─────────────────────────────
def test_plan_folds_30m_single_fold_manual():
    """30 月 × train24/valid6/step6 → 恰 1 折（手工对账 CP-0024 口径）。"""
    ms = _months(30)
    folds = plan_folds(ms, {"train_min_months": 24, "valid_months": 6, "step_months": 6})
    assert len(folds) == 1
    f = folds[0]
    assert f["t_index"] == 24
    assert f["train_months"] == ms[:24] and f["train_months"][-1] == "2022-12"
    assert f["valid_months"] == ms[24:30] and f["valid_months"][0] == "2023-01"


def test_plan_folds_expanding_and_disjoint():
    """42 月 → 3 折：train 单向扩展（24/30/36）、valid 互不重叠且单向往前。"""
    ms = _months(42)
    folds = plan_folds(ms, {"train_min_months": 24, "valid_months": 6, "step_months": 6})
    assert [f["t_index"] for f in folds] == [24, 30, 36]
    assert all(f["train_months"] == ms[:f["t_index"]] for f in folds)      # 扩展窗口，禁止滑动
    starts = [f["valid_months"][0] for f in folds]
    ends = [f["valid_months"][-1] for f in folds]
    assert starts == ["2023-01", "2023-07", "2024-01"]
    assert all(starts[i] > ends[i - 1] for i in range(1, len(folds)))      # 无重叠/倒退


def test_plan_folds_invalid_params():
    """train_min/valid/step 任一 <1 → StrategyWfError（配置违规不得静默）。"""
    ms = _months(30)
    for bad in ({"train_min_months": 0}, {"valid_months": 0}, {"step_months": -1}):
        with pytest.raises(StrategyWfError, match="≥1"):
            plan_folds(ms, bad)


# ── 纯函数：折叠自审（DoD c/f）───────────────────────────
def _hand_folds(ms: list[str], spans: list[tuple[int, int]]) -> list[dict]:
    """手工构造折：spans = [(t_index, valid_len), ...]。"""
    return [{"t_index": t, "train_months": ms[:t], "valid_months": ms[t:t + v]}
            for t, v in spans]


def test_audit_folds_pass():
    ms = _months(36)
    res = audit_folds(_hand_folds(ms, [(24, 6), (30, 6)]), "2025-01")
    assert res["check"] == "pass" and res["n_folds"] == 2
    assert res["note"] and "lockbox_start" in res


def test_audit_folds_overlap_fail():
    """step < valid → valid 窗口重叠 = 协议违规（DoD f）。"""
    ms = _months(30)
    bad = _hand_folds(ms, [(12, 6), (14, 6)])          # 第二折 valid 起 ms[14] ≤ 前折末 ms[17]
    with pytest.raises(StrategyWfError, match="重叠"):
        audit_folds(bad, None)


def test_audit_folds_valid_regression_fail():
    """valid 窗口倒退（起 < 前折末）同罪。"""
    ms = _months(30)
    with pytest.raises(StrategyWfError, match="重叠"):
        audit_folds(_hand_folds(ms, [(12, 6), (10, 4)]), None)


def test_audit_folds_train_not_expanding_fail():
    """train 窗口未单向扩展（滑动重置历史）= 协议违规（valid 无重叠时独立触发 train 检查）。"""
    ms = _months(42)
    folds = [{"t_index": 30, "train_months": ms[6:30], "valid_months": ms[30:36]},
             {"t_index": 24, "train_months": ms[:24], "valid_months": ms[36:42]}]
    with pytest.raises(StrategyWfError, match="扩展"):
        audit_folds(folds, None)


def test_audit_folds_lockbox_touch_fail():
    """train/valid 任一段触及 lockbox 月 → 报错（越界不得静默，DoD c 语义）。"""
    ms = _months(36)                                     # 2021-01..2023-12
    ok = _hand_folds(ms, [(24, 6)])                      # train ..2022-12 / valid 2023-01..06
    assert audit_folds(ok, "2023-07")["check"] == "pass"
    bad_valid = _hand_folds(ms, [(30, 6)])               # valid 2023-07..12 全 ≥ lockbox
    with pytest.raises(StrategyWfError, match="lockbox"):
        audit_folds(bad_valid, "2023-07")
    bad_train = [{"t_index": 36, "train_months": ms[:36], "valid_months": ["2023-01"]}]
    with pytest.raises(StrategyWfError, match="lockbox"):
        audit_folds(bad_train, "2023-06")                # train 末段含 2023-06 ≥ lockbox


# ── 纯函数：选法 / 封存切分 / 排序稳定 ───────────────────
def test_select_method_max_tie_and_none():
    """train Sharpe 最大当选；并列按枚举序取先；None（无波动）视为最差。"""
    assert select_method({"equal": 0.5, "ic_12m": 1.2, "ic_ir": 0.9}) == "ic_12m"
    assert select_method({"equal": 1.0, "ic_12m": 1.0}) == "equal"          # 并列 → 枚举先
    assert select_method({"ic_ir": 1.0, "ic_12m": 1.0}) == "ic_12m"        # order 优先级
    assert select_method({"equal": None, "ic_12m": -0.5}) == "ic_12m"
    assert select_method({"equal": None, "ic_12m": None}) == "equal"
    assert select_method({"zeta": 0.1, "equal": 0.2}) == "equal"           # 未知法排枚举后
    with pytest.raises(StrategyWfError, match="候选"):
        select_method({})


def test_split_lockbox_guards():
    """封存不足 min_seal / 晚于末端 / 全封存 → 报错；None → 全 core 零封存。"""
    ms = ["2021-01", "2021-02", "2021-03"]
    assert _split_lockbox(ms, None, 12) == (ms, [])
    core, sealed = _split_lockbox(ms, "2021-03", 1)
    assert (core, sealed) == (["2021-01", "2021-02"], ["2021-03"])
    with pytest.raises(StrategyWfError, match="协议最少"):
        _split_lockbox(ms, "2021-03", 2)                 # 封存 1 月 < 2
    with pytest.raises(StrategyWfError, match="封存段为空"):
        _split_lockbox(ms, "2021-06", 1)                 # 晚于样本末端
    with pytest.raises(StrategyWfError, match="无可用区间"):
        _split_lockbox(["2021-01"], "2021-01", 1)        # 全封存


def test_rank_stable_monotone_judge():
    """rank_stable 判据：扰动方向与绩效单调（slippage↑ 终值不增）→ True，回升 → False。"""
    assert rank_stable_from([0.30, 0.29, 0.28]) is True
    assert rank_stable_from([0.30, 0.30, 0.30]) is True  # 平坦（并列）也算稳定
    assert rank_stable_from([0.28, 0.30, 0.29]) is False
    assert rank_stable_from([0.3]) is True


# ── 集成：golden repo 滚动 / lockbox / 敏感性 ────────────
FIDS = ("val_pe_ttm", "mom_20d_fl")

SPEC = {"id": "wf-strategy", "name": "WF 策略",
        "factors": [{"id": "val_pe_ttm", "direction": "asc", "weight": 1.0},
                    {"id": "mom_20d_fl", "direction": "desc", "weight": 1.0}],
        "top_n": 3, "methods": ["equal", "ic_12m"], "rebalance": "monthly",
        "benchmark": "510300.SH", "exec": "close"}

CONS_NV = {"t_plus_1": False, "limit_move": "defer", "suspension": "defer", "lot_size": 100}
SPEC_NV = {**SPEC, "exec": "next_day_vwap", "constraints": CONS_NV, "capital": 1_000_000}

# golden 样本 2021-01..06；mom_20d_fl 需历史预热（01 月无截面）→ sample_start=2021-02：
# core=02..04（3 月）→ 2 折（t=1: train[02] valid[03]；t=2: train[02,03] valid[04]）；
# lockbox 封存 05/06（2 月）
CFG = {"train_min_months": 1, "valid_months": 1, "step_months": 1,
       "lockbox_start": "2021-05", "min_lockbox_months": 1, "sample_start": "2021-02"}


@pytest.fixture
def m4wf(fl_db, fl_repo):
    """物化两因子（active）→ (db_path, repo)。"""
    db, _ = fl_db
    for fid in FIDS:
        set_status(fl_repo, fid, "active", who="wf-bridge", why="T-C4 验收",
                   gate={"ok": True, "reasons": []})
        materialize(load_factor(fl_repo, fid), db, mode="full")
    return db, fl_repo


def _add_intraday_range(repo: Path) -> None:
    """golden 行情为平 OHLC → tmp 副本加日内振幅与量额（close/pct 不动），次日 VWAP 可成交。"""
    p = repo / "market" / "daily_bars.json"
    bars = json.loads(p.read_text(encoding="utf-8"))
    for rows in bars.values():
        for r in rows:
            c = r["close"]
            r["open"] = round(c * 0.99, 4)
            r["high"] = round(c * 1.01, 4)
            r["low"] = round(c * 0.98, 4)
            r["volume"], r["amount"] = 10000, c * 10000
    p.write_text(json.dumps(bars, ensure_ascii=False), encoding="utf-8")


def test_walk_forward_folds_integration(m4wf):
    """滚动集成：2 折、train 单向扩展、valid 不重叠不触 lockbox、aggregate 字段齐。"""
    db, repo = m4wf
    res = walk_forward_folds(SPEC, db, repo, CFG)
    assert res["n_folds"] == 2 and res["months_n"] == 5 and res["core_months_n"] == 3
    assert res["sealed_months"] == ["2021-05", "2021-06"]
    assert res["sealed_share"] == pytest.approx(2 / 5, abs=1e-6)
    f1, f2 = res["folds"]
    assert f1["train_range"] == ["2021-02", "2021-02"] and f1["valid_range"] == ["2021-03", "2021-03"]
    assert f2["train_range"] == ["2021-02", "2021-03"] and f2["valid_range"] == ["2021-04", "2021-04"]
    assert f2["train_months_n"] > f1["train_months_n"]                     # 扩展窗口
    assert f1["valid_months_n"] == f2["valid_months_n"] == 1
    for f in res["folds"]:
        assert f["method_chosen"] in ("equal", "ic_12m")
        assert set(f["method_sharpes_is"]) == {"equal", "ic_12m"}
    assert res["audit"]["check"] == "pass"
    agg = res["aggregate"]
    assert set(agg) == {"is_mean_sharpe", "oos_mean_sharpe", "oos_ir", "decay", "n_folds"}
    assert agg["n_folds"] == 2
    assert res["cfg"]["lockbox_start"] == "2021-05" and res["cfg"]["sample_start"] == "2021-02"
    assert res["cfg"]["sensitivity"] is False


def test_walk_forward_deterministic_bitwise(m4wf):
    """同输入重跑折序列 bit 级一致（确定性红线；runid/归档均为内容哈希口径）。"""
    db, repo = m4wf
    r1 = walk_forward_folds(SPEC, db, repo, CFG)
    r2 = walk_forward_folds(SPEC, db, repo, CFG)
    assert json.dumps(r1["folds"], sort_keys=True) == json.dumps(r2["folds"], sort_keys=True)
    assert json.dumps(r1["aggregate"], sort_keys=True) == json.dumps(r2["aggregate"], sort_keys=True)


def test_walk_forward_no_fold_space(m4wf):
    """lockbox 封存过多 / train_min 过大 → 滚动无折叠空间 → 报错并给出 core/train/valid 数目。"""
    db, repo = m4wf
    with pytest.raises(StrategyWfError, match="无折叠空间"):
        walk_forward_folds(SPEC, db, repo, {**CFG, "train_min_months": 3})
    with pytest.raises(StrategyWfError, match="无折叠空间"):
        walk_forward_folds(SPEC, db, repo, {**CFG, "lockbox_start": "2021-04",
                                            "train_min_months": 2})


def test_walk_forward_invalid_month_cfg(m4wf):
    """train_min_months=0 → StrategyWfError（norm 层拦截）。"""
    db, repo = m4wf
    with pytest.raises(StrategyWfError, match="≥1"):
        walk_forward_folds(SPEC, db, repo, {**CFG, "train_min_months": 0})


def test_lockbox_verify_once_and_archive(m4wf, tmp_path):
    """lockbox 一次性（DoD d）：首验 verify_count=1 + report.json 归档；二次调用报 _v2 协议。"""
    db, repo = m4wf
    rep = lockbox_verify(SPEC, "equal", db, repo, CFG, artifact_dir=tmp_path)
    assert rep["strategy_id"] == "wf-strategy" and rep["verify_count"] == 1
    assert rep["method"] == "equal" and rep["lockbox_start"] == "2021-05"
    assert rep["sealed_months"] == ["2021-05", "2021-06"] and rep["n_sealed"] == 2
    assert len(rep["runid"]) == 12
    assert rep["window"][0].startswith("2021-05") and rep["window"][1].startswith("2021-06")
    assert rep["cost"] is None                        # close 模式无撮合成本轨 → 如实披露
    assert rep["note"] and "只验证一次" in rep["note"]
    out = tmp_path / f"lockbox_wf-strategy_{rep['runid']}" / "report.json"
    assert out.exists() and json.loads(out.read_text(encoding="utf-8"))["runid"] == rep["runid"]
    with pytest.raises(StrategyWfError, match="_v2"):
        lockbox_verify(SPEC, "equal", db, repo, CFG, artifact_dir=tmp_path)


def test_lockbox_verify_v2_bypass_and_guards(m4wf, tmp_path):
    """升 version（id 后缀 _v2）可重录；lockbox_start 为 None / 晚于末端 → 报错。"""
    db, repo = m4wf
    spec2 = {**SPEC, "id": "wf-strategy-v2"}
    rep2 = lockbox_verify(spec2, "equal", db, repo, CFG, artifact_dir=tmp_path)
    assert rep2["strategy_id"] == "wf-strategy-v2" and rep2["verify_count"] == 1
    with pytest.raises(StrategyWfError, match="未配置"):
        lockbox_verify(SPEC, "equal", db, repo,
                       {**CFG, "lockbox_start": None}, artifact_dir=tmp_path)
    with pytest.raises(StrategyWfError, match="封存段为空"):
        lockbox_verify(SPEC, "equal", db, repo,
                       {**CFG, "lockbox_start": "2021-12"}, artifact_dir=tmp_path)


def test_sensitivity_grid_close_mode(m4wf):
    """close 模式：slippage 轴显式 skip（须 T-C3 撮合口径）；top_n ±30% 取整；method 全枚举。"""
    db, repo = m4wf
    out = sensitivity_grid(SPEC, db, repo, CFG)
    assert out["window"][0].startswith("2021-02") and out["window"][1].startswith("2021-04")
    assert "skipped" in out["axes"]["slippage_bps"]
    assert "close" in out["axes"]["slippage_bps"]["skipped"]
    tn = out["axes"]["top_n"]
    assert tn["base"] == 3 and set(tn["values"]) == {"2", "3", "4"}
    assert set(out["axes"]["method"]["values"]) == {"equal", "ic_12m"}
    for axis in (tn["values"], out["axes"]["method"]["values"]):
        for v in axis.values():
            assert set(v) == {"total_return", "sharpe"}
    assert out["rank_stable"] is None and out["band"] is None            # 无滑点轴无单调判定


def test_sensitivity_grid_next_vwap_monotone(m4wf):
    """next_day_vwap（DoD e）：滑点 3.5→6.5bps 终值单调不增 → rank_stable=True、band≥0。"""
    db, repo = m4wf
    _add_intraday_range(repo)
    out = sensitivity_grid(SPEC_NV, db, repo, CFG)
    sl = out["axes"]["slippage_bps"]
    assert sl["base"] == 5.0 and set(sl["values"]) == {"3.5", "5.0", "6.5"}
    finals = [sl["values"][k]["total_return"] for k in ("3.5", "5.0", "6.5")]
    assert all(finals[i] >= finals[i + 1] - 1e-12 for i in range(len(finals) - 1))
    assert out["rank_stable"] is True and out["band"] >= 0
    assert out["band"] == pytest.approx(max(finals) - min(finals), abs=5e-7)   # band 为 6 位舍入
    assert set(out["axes"]["top_n"]["values"]) == {"2", "3", "4"}


def test_strategy_walk_forward_e2e(m4wf, tmp_path):
    """主流程：滚动 + 最终选法 + lockbox 一次性（归档）+ 缺省不跑敏感性。"""
    db, repo = m4wf
    res = strategy_walk_forward(SPEC, db, repo, CFG, artifact_dir=tmp_path)
    assert res["n_folds"] == 2 and res["final_method"] in ("equal", "ic_12m")
    assert set(res["final_method_sharpes"]) == {"equal", "ic_12m"}
    lb = res["lockbox"]
    assert lb["verify_count"] == 1 and lb["strategy_id"] == "wf-strategy"
    assert res["sensitivity"] is None                                    # 缺省关
    assert res["audit"]["check"] == "pass"
    archived = list(tmp_path.glob("lockbox_wf-strategy_*/report.json"))
    assert len(archived) == 1


def test_strategy_walk_forward_e2e_sensitivity(m4wf, tmp_path):
    """sensitivity=True → 敏感性与 lockbox 同链产出（lockbox 段仍不参与扰动评估）。"""
    db, repo = m4wf
    res = strategy_walk_forward(SPEC, db, repo, {**CFG, "sensitivity": True},
                                artifact_dir=tmp_path)
    assert res["sensitivity"] is not None
    assert res["sensitivity"]["window"][0].startswith("2021-02")         # = core（02..04）
    assert res["sensitivity"]["window"][1].startswith("2021-04")         # 封存段未触碰
    assert res["lockbox"]["verify_count"] == 1
