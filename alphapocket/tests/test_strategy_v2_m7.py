"""M7 T-F7：三策略 v2 + lockbox 2022-01 + v1/v2 对照测试。

要点（契约红线）：
- v2 因子只准来自 T-F6 后实际 active 池（§5.3 #3）：禁止 draft/拒绝因子凑五族；
- lockbox 2022-01 恢复计划口径：core ≥44 月（T-F1 设计目标锚）、折数 ≥3、封存 ≥12 月；
- ledger 一次性协议：v2 新 id 独立条目，v1/M6 冒烟记录不覆盖；
- 对照报告 comparability：universe 不同 → 绝对收益不可比（诚实口径，禁止跨 universe 排名）。

运行：``cd alphapocket && /workspace/alphapocket-web/core/.venv/bin/python -m pytest tests/test_strategy_v2_m7.py -q``
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from applib.strategy import StrategyError, load_strategy

ROOT = Path(__file__).resolve().parents[1]
SB_REPO = ROOT / "sandbox" / "repo"
DB = ROOT / "sandbox" / "data" / "fl.duckdb"
STRAT = SB_REPO / "strategies"
ART = ROOT / "sandbox" / "artifacts"
LEDGER = ART / "strategy_wf" / "lockbox_ledger.json"
COMPARE_JSON = ART / "m7_v2_compare" / "compare_v1_v2.json"

V2 = ("lowvol-dividend-v2", "pe-roe-v2", "multi-resonance-v2")
V1 = ("lowvol-dividend-approx-v1", "pe-roe-approx-v1", "multi-resonance-v1")
# T-F6 时点 active 池快照（红线基准）：active ∧ health≠invalid
_POOL3 = {"val_pe_ttm", "mom_20d_fl", "sig_mom20_rank_fl"}
# 契约目标腿（T-F2~T-F5 门禁未过 → 全部 draft，禁止入 v2）
_DRAFT_TARGETS = {"low_vol_252d", "val_div_yield", "div_payout_streak", "qua_roe_ttm"}


def _sig_health_now() -> str | None:
    """sig_mom20_rank_fl 当下巡检健康（fl_factor_health 最新行；无行=None）。

    T-F7 数据面同步（2026-10-14 16:33 clone bars 修正）之后，2026-10-15 巡检如实重算：
    近 24 月 directed IC 均值 -0.046 ≤ 0 且全段失效 → invalid（healthy→invalid 合法迁移，
    M5 巡检纪律），合成池摘除——v2 引用它被池准入拒绝 = 门禁正确行为，不是回归。"""
    import duckdb
    if not DB.exists():
        return None
    con = duckdb.connect(str(DB), read_only=True)
    try:
        has = con.execute("SELECT count(*) FROM information_schema.tables "
                          "WHERE table_name = 'fl_factor_health'").fetchone()[0]
        if not has:
            return None
        row = con.execute("SELECT health FROM fl_factor_health "
                          "WHERE factor_id = 'sig_mom20_rank_fl' "
                          "ORDER BY as_of DESC LIMIT 1").fetchone()
        return row[0] if row else None
    finally:
        con.close()


def _wf_reports() -> list[dict]:
    """本轮 T-F7 wf 归档（2026-10-14 起，lockbox 2022-01 口径；report 无 spec.id，
    策略归属用 ledger/lockbox 报告路径，此处按 cfg 识别本轮口径归档）。"""
    out = []
    for d in sorted(ART.glob("strategy_wf_2*/report.json")):
        rep = json.loads(d.read_text(encoding="utf-8"))
        if (rep.get("cfg") or {}).get("lockbox_start") == "2022-01":
            out.append(rep)
    return out


# ── a) 三份 v2 YAML schema + 池准入 + 缺腿/缺族披露 ─────
def test_v2_yaml_schema_and_pool_gate():
    """T-F6 时点：三份 v2 全过池准入（sig 在池快照内）。
    2026-10-15 巡检演进（T-F7 数据面修正后如实重算）：sig_mom20_rank_fl invalid →
    合成池摘除 → v2 池准入拒绝。两分支都按纪律断言（不豁免不造假）：
    - sig 可入池 → v2 全过准入 + 因子越池红线；
    - sig 被摘除 → v2 load 必须被拒（门禁无旁路）+ 摘除登记在档
      （fl_factor_health invalid 行）+ T-F7 时点 artifact 冻结在档（ledger 一次性）。
      策略恢复路径 = M6 升版纪律（摘除 sig 升 v3 重走 lockbox），M8 待办，不在本测放宽。"""
    import duckdb

    sig_h = _sig_health_now()
    for sid in V2:
        p = STRAT / f"{sid}.yaml"
        assert p.exists(), f"{sid}.yaml 缺失"
        text = p.read_text(encoding="utf-8")
        assert ("门禁未过" in text or "缺族" in text) and "披露" in text, \
            f"{sid} 缺腿/缺族披露缺失"

    if sig_h != "invalid":
        for sid in V2:
            spec = load_strategy(STRAT / f"{sid}.yaml", SB_REPO, DB)
            fids = {f["id"] for f in spec["factors"]}
            assert fids <= _POOL3, f"{sid} 因子越池（红线 §5.3 #3）：{fids - _POOL3}"
            assert not (fids & _DRAFT_TARGETS), f"{sid} 拿 draft 因子凑腿"
        return

    # 巡检演进分支：池准入如实拒绝（含 sig 的 v2 必须 raise，非静默降级）
    con = duckdb.connect(str(DB), read_only=True)
    try:
        row = con.execute("SELECT reasons_json FROM fl_factor_health "
                          "WHERE factor_id = 'sig_mom20_rank_fl' "
                          "ORDER BY as_of DESC LIMIT 1").fetchone()
    finally:
        con.close()
    assert row and row[0] and "invalid" in row[0], "sig invalid 判据应在巡检 reasons 在档"
    import yaml as _yaml
    for sid in V2:
        raw = _yaml.safe_load((STRAT / f"{sid}.yaml").read_text(encoding="utf-8"))
        refs_sig = any(f.get("id") == "sig_mom20_rank_fl"
                       for f in raw.get("factors") or [])
        if refs_sig:
            with pytest.raises(StrategyError, match="sig_mom20_rank_fl"):
                load_strategy(STRAT / f"{sid}.yaml", SB_REPO, DB)
        else:
            spec = load_strategy(STRAT / f"{sid}.yaml", SB_REPO, DB)  # 不引用被摘除因子 → 正常准入
            assert {f["id"] for f in spec["factors"]} <= _POOL3


# ── b) lockbox 2022-01 核算：core ≥44 月（T-F1 设计锚）──
@pytest.mark.skipif(not (DB.exists() and LEDGER.exists()), reason="wf 归档/ledger 缺失")
def test_lockbox_2022_01_core_anchor():
    """契约核算锚：sample_start=2018-05（active 池公共有值月）→ core 44 月 ≥44、
    折数 ≥3（t=24/30/36）、封存 ≥12 月；v1 归档口径（2025-10 封存）不复现；
    策略 id 归属由 lockbox 报告路径承载（lockbox_<sid>_<runid>）。"""
    reports = _wf_reports()
    assert len(reports) >= 3, f"T-F7 wf 归档（lockbox 2022-01 口径）不足 3 份：{len(reports)}"
    for rep in reports:
        assert rep["cfg"]["sample_start"] == "2018-05"
        assert rep["core_months_n"] == 44, f"core={rep['core_months_n']} ≠ 44（T-F1 锚失守）"
        assert rep["n_folds"] >= 3
        assert rep["n_sealed"] >= 12
        assert rep["audit"]["check"] == "pass"
        assert (rep.get("sensitivity") or {}).get("rank_stable") is True, "敏感性未跑/不稳"
    for sid in V2:                              # 每策略 lockbox 一次性报告在档
        lbs = sorted((ART / "strategy_wf").glob(f"lockbox_{sid}_*/report.json"))
        assert lbs, f"{sid} lockbox 报告缺失"
        rep = json.loads(lbs[-1].read_text(encoding="utf-8"))
        assert rep["verify_count"] == 1 and rep["lockbox_start"] == "2022-01"
        assert {"total_return", "sharpe", "max_dd"} <= set(rep["oos"] or {})


# ── c) ledger 一次性：v2 条目独立，v1/M6 记录不覆盖 ─────
@pytest.mark.skipif(not LEDGER.exists(), reason="lockbox ledger 缺失")
def test_lockbox_ledger_one_shot():
    led = json.loads(LEDGER.read_text(encoding="utf-8"))
    # v1/M6 冒烟条目原样保留（一次性协议：历史记录不覆盖）
    assert "pe-mom-wf-smoke" in led, "M6 冒烟 ledger 记录丢失"
    m6_runs = led["pe-mom-wf-smoke"]["runs"]
    assert any(r["lockbox_start"] == "2025-10" for r in m6_runs), "M6 lockbox 2025-10 记录被改动"
    # v2 三条新 id 独立条目，verify_count = 1（一次性验证）
    for sid in V2:
        assert sid in led, f"{sid} 未入 ledger"
        runs = led[sid]["runs"]
        assert len(runs) == 1 and runs[0]["lockbox_start"] == "2022-01", \
            f"{sid} ledger 违反一次性：{runs}"


# ── d) v1 归档冻结 + 对照报告产物 ───────────────────────
@pytest.mark.skipif(not COMPARE_JSON.exists(), reason="对照报告未生成（T-F7 后段产物）")
def test_v1_frozen_and_compare_report():
    """v1 归档冻结（三份 v1 YAML 与 M6 backtest 归档原样）+ 对照报告 comparability 披露
    （universe 不同 → 绝对收益不可比；禁止跨 universe 排名结论）。"""
    for sid in V1:
        p = STRAT / f"{sid}.yaml"
        assert p.exists(), f"v1 策略 {sid} 被删除（归档冻结红线）"
        text = p.read_text(encoding="utf-8")
        assert "近似" in text or "池内" in text, f"{sid} 近似标注丢失"
    doc = json.loads(COMPARE_JSON.read_text(encoding="utf-8"))
    assert set(doc["strategies"]) == {"lowvol-dividend", "pe-roe", "multi-resonance"}
    for name, row in doc["strategies"].items():
        comp = row["comparability"]
        assert "不可直接比较" in comp and "universe" in comp, f"{name} comparability 披露缺失"
        for k in ("v1", "v2"):
            assert {"factors", "lockbox", "ret", "sharpe", "dd"} <= set(row[k]), \
                f"{name}/{k} 字段残缺"
    # lockbox 口径差异如实呈现：v1=2025-10（M6 被迫）→ v2=2022-01（计划口径恢复）
    sample = next(iter(doc["strategies"].values()))
    assert sample["v1"]["lockbox"] == "2025-10" and sample["v2"]["lockbox"] == "2022-01"
    assert "v1 历史归档不重跑" in doc["notes"]


# ── e') lockbox 段零接触（红线：滚动阶段 2022-01 起零接触）──
@pytest.mark.skipif(not (DB.exists() and LEDGER.exists()), reason="wf 归档/ledger 缺失")
def test_lockbox_zero_touch_in_folds():
    """红线落测：本轮全部 wf 折的 train/valid 段最后月 < lockbox 2022-01（封存段在
    滚动阶段零接触，与 audit_folds 双保险）；lockbox 绩效只出现在一次性验证报告。"""
    for rep in _wf_reports():
        for f in rep["folds"]:
            assert f["train_range"][1] < "2022-01", f"train 触 lockbox：{f['train_range']}"
            assert f["valid_range"][1] < "2022-01", f"valid 触 lockbox：{f['valid_range']}"
        assert rep["n_sealed"] == 58 and rep["sealed_months"][0] == "2022-01"


# ── f) v2 回测归档在档（DoD 3）─────────────────────────
@pytest.mark.skipif(not DB.exists(), reason="fl 库缺失")
def test_v2_backtest_artifacts():
    """三策略 v2 全量回测归档（双列/暴露/分布三节口径不变）：m6_bt_<id>_bt_* 目录
    （run_id 前缀沿用）以目录名解析策略 id（report.strategy 是成交模式描述非 id）。"""
    found = set()
    for d in ART.glob("m6_bt_*-v2_bt_*/report.json"):
        sid = d.parent.name[len("m6_bt_"):].split("_bt_")[0]
        if sid in V2:
            rep = json.loads(d.read_text(encoding="utf-8"))
            sm = rep.get("strategy_metrics") or {}
            assert {"total_return", "sharpe", "max_dd"} <= set(sm), f"{sid} 回测指标残缺"
            found.add(sid)
    assert found == set(V2), f"v2 回测归档不齐：{set(V2) - found}"
