"""M7 个人化因子通道测试（实施计划 §10 任务 10.4 DoD）：归纳锚 + 同一流水线不豁免。

合成库（4 标的 B01..B04 × 33 个月末，月收益 r_k = 0.01·k，基准恒 100）：
- F_good：value = k（B04 恒第一）→ 满意交易选 B04 → 分位 1.0 → 入选共性特征；
- F_bad：value = 5−k（B04 恒最后）→ 分位 0.25 → 低于阈值 → 不入选；
- 候选 per_e3_clone = F_good 截面 zscore → 与 F_good ρ̄≈1 → ⑤ 判替代通道
  → 治理裁决 alternative ≠ pass → 激活门禁拒绝（不豁免任何关卡）；
- E1 降级：满意交易 < 2 笔 → 拒绝归纳，不生成候选。
"""
import calendar
from datetime import date

import duckdb
import pytest

from factorlib.evaluate import _EVAL_DDLS
from factorlib.lifecycle import LifecycleError
from factorlib.personal import (CANDIDATE_ID, E3_MIN_TRADES, E3_PCT_THRESHOLD,
                                build_candidate, extract_features, induce,
                                load_trades, register_candidate, run_channel_on_con)
from factorlib.registry import load_definitions

SEC = [f"B{k:02d}" for k in range(1, 5)]
R = {s: 0.01 * k for k, s in enumerate(SEC, start=1)}
MES = [f"{date(y, m, calendar.monthrange(y, m)[1]).isoformat()}"
       for y, m in [(2021, m) for m in range(1, 13)]
       + [(2022, m) for m in range(1, 13)]
       + [(2023, m) for m in range(1, 10)]]                 # 33 个月末
BENCH = "510300.SH"
ENTRY = [MES[5], MES[10], MES[15]]                          # 满意交易入场日
WF, LB = (6, 3), 3


def closes_series(s: str) -> list[float]:
    out, c = [], 100.0
    for _ in MES:
        c *= 1 + R[s]
        out.append(c)
    return out


class _Meta:
    def __init__(self, fid, direction=1):
        self.id, self.direction, self.type = fid, direction, "alpha"
        self.name, self.version, self.family, self.engine = fid, "1.0.0", "test", "synthetic"


METAS = {"F_good": _Meta("F_good", 1), "F_bad": _Meta("F_bad", 1)}


@pytest.fixture()
def per_con():
    con = duckdb.connect(":memory:")
    for ddl in _EVAL_DDLS:
        con.execute(ddl)
    con.execute("""CREATE TABLE factor_value_daily
                   (dt DATE, secid TEXT, factor_id TEXT, value DOUBLE)""")
    con.execute("""CREATE TABLE factor_pit_meta
                   (dt DATE, secid TEXT, factor_id TEXT, used_period DATE,
                    used_announce DATE, data_staleness INTEGER)""")
    for s in SEC:
        con.executemany("INSERT INTO dwd_daily_bar VALUES (?,?,?,?,?,?,?,?,?)",
                        [(s, dt, c, c, c, c, 1000, 1000 * c, 0.0)
                         for dt, c in zip(MES, closes_series(s))])
    con.executemany("INSERT INTO dwd_daily_bar VALUES (?,?,?,?,?,?,?,?,?)",
                    [(BENCH, dt, 100.0, 100.0, 100.0, 100.0, 1000, 100000.0, 0.0)
                     for dt in MES])
    con.executemany("INSERT INTO dim_security VALUES (?,?,?,?,?)",
                    [(s, s, "XSHG", "STOCK", "IND1") for s in SEC])
    rows = [(dt, s, fid, float(k) if fid == "F_good" else float(5 - k))
            for fid in ("F_good", "F_bad")
            for dt in MES for k, s in enumerate(SEC, start=1)]
    con.executemany("INSERT INTO factor_value_daily (dt, secid, factor_id, value)"
                    " VALUES (?,?,?,?)", rows)
    return con


@pytest.fixture()
def per_root(tmp_path):
    (tmp_path / "hypotheses.yaml").write_text(
        "H-E3-001:\n  statement: E3 克隆测试假设\n  status: open\n", encoding="utf-8")
    return tmp_path


def _trades(satisfied=True):
    rows = [{"secid": "B04", "entry_date": d, "satisfied": satisfied} for d in ENTRY]
    rows.append({"secid": "B01", "entry_date": MES[8], "satisfied": False})
    return rows


# ── 账本 ──
def test_load_trades_validation(tmp_path):
    p = tmp_path / "t.yaml"
    p.write_text("trades:\n  - secid: B01\n    entry_date: 2021-06-30\n"
                 "    satisfied: true\n", encoding="utf-8")
    ts = load_trades(p)
    assert ts == [{"secid": "B01", "entry_date": "2021-06-30",
                   "satisfied": True, "note": ""}]
    p.write_text("trades:\n  - secid: B01\n", encoding="utf-8")
    with pytest.raises(ValueError, match="entry_date"):
        load_trades(p)
    p.write_text("trades: []\n", encoding="utf-8")
    with pytest.raises(ValueError, match="非空"):
        load_trades(p)


# ── ① 特征提取：方向秩分位锚 ──
def test_extract_features_percentile(per_con):
    pcts, notes = extract_features(per_con, METAS, _trades()[:3])
    assert pcts["F_good"] == [1.0, 1.0, 1.0]        # B04 恒第一 → 分位 1.0
    assert pcts["F_bad"] == [0.25, 0.25, 0.25]      # B04 恒最后
    assert notes == []
    # 方向翻转：F_good direction=-1 → B04 分位变 0.25
    m = _Meta("F_good", -1)
    pcts2, _ = extract_features(per_con, {"F_good": m}, _trades()[:1])
    assert pcts2["F_good"] == [0.25]


# ── ② 归纳：阈值裁决 ──
def test_induce_threshold(per_con):
    pcts, _ = extract_features(per_con, METAS, _trades()[:3])
    feats = induce(pcts, threshold=E3_PCT_THRESHOLD)
    assert feats == {"F_good": 1.0}
    assert induce({"F_x": [0.5, 0.5]}, threshold=0.7) == {}


# ── ③ 候选 YAML：可注册加载（id 前缀 + draft 初始态） ──
def test_candidate_yaml_loadable(per_root, per_con):
    feats = {"F_good": 1.0}
    yaml_text, sql = build_candidate(feats, {"F_good": 1}, 3, ENTRY[:1] + ENTRY[-1:])
    assert "factor_id IN ('F_good')" in sql
    p = register_candidate(per_root, yaml_text, per_con)
    metas = {m.id: m for m in load_definitions(per_root)}
    assert CANDIDATE_ID in metas
    m = metas[CANDIDATE_ID]
    assert m.family == "personal" and m.status == "draft"
    assert m.engine == "sql" and m.direction == 1
    assert m.hypothesis_id == "H-E3-001" and "factor_value_daily" in m.depends
    assert p.exists() and "E3 策略克隆候选" in p.read_text(encoding="utf-8")


# ── ④ 同一流水线：治理裁决 alternative → 激活被拒（不豁免） ──
def test_pipeline_no_exemption(per_con, per_root):
    rep = run_channel_on_con(per_con, per_root, _trades(), metas=dict(METAS),
                             wf=WF, lockbox_months=LB)
    assert rep["evidence"] == "E3"
    assert rep["components"] == {"F_good": 1.0}
    # ⑤ 增量关：克隆与成分 ρ̄≈1 → 替代通道（门禁如实体现共线事实）
    assert rep["verdict"]["overall"] == "alternative"
    assert rep["verdict"]["incremental"]["pass"] is False
    assert "替代" in rep["verdict"]["incremental"]["reason"]
    # 激活门禁拒绝（verdict ≠ pass，无旁路）
    assert rep["activation"]["attempted"] is True
    assert rep["activation"]["activated"] is False
    assert "≠ pass" in rep["activation"]["blocked_by"]
    # E3 置信度：样本量与区间强制标注
    assert rep["reproduction"]["sample_size"] == 3
    assert rep["reproduction"]["mean_pct"] == 1.0
    # 候选保持 draft（注册纪律：激活失败不升状态）
    m = {x.id: x for x in load_definitions(per_root)}[CANDIDATE_ID]
    assert m.status == "draft"


def test_activate_blocked_directly(per_con, per_root):
    """门禁直测：即使手搓 gov 报告也无法绕过裁决校验（无旁路）。"""
    from factorlib.personal import build_candidate as bc
    yaml_text, _ = bc({"F_good": 1.0}, {"F_good": 1}, 3, ENTRY[:2])
    register_candidate(per_root, yaml_text, per_con)
    meta = {x.id: x for x in load_definitions(per_root)}[CANDIDATE_ID]
    fake_gov = {"verdicts": [{"factor_id": CANDIDATE_ID, "overall": "alternative"}]}
    with pytest.raises(LifecycleError, match="≠ pass"):
        from factorlib.lifecycle import activate
        activate(per_con, meta, fake_gov, root=per_root)


# ── E1 降级：样本不足拒绝归纳 ──
def test_e1_insufficient_sample(per_con, per_root):
    trades = [{"secid": "B04", "entry_date": ENTRY[0], "satisfied": True}]
    rep = run_channel_on_con(per_con, per_root, trades, metas=dict(METAS),
                             wf=WF, lockbox_months=LB)
    assert rep["evidence"] == "E1"
    assert rep["candidate"] is None
    assert str(E3_MIN_TRADES) in rep["conclusion"]
    assert not (per_root / "definitions" / "personal" / f"{CANDIDATE_ID}.yaml").exists()


# ── 诚实拒绝：无共性特征不克隆 ──
def test_no_common_features(per_con, per_root):
    # 仅 F_good 入册时 B02 分位 0.5 < 0.70 → 无共性特征 → 诚实不克隆
    rep = run_channel_on_con(per_con, per_root,
                             [{"secid": "B02", "entry_date": d, "satisfied": True}
                              for d in ENTRY],
                             metas={"F_good": METAS["F_good"]},
                             wf=WF, lockbox_months=LB)
    assert rep["evidence"] == "E3"
    assert rep["candidate"] is None
    assert "无共性特征" in rep["conclusion"]
