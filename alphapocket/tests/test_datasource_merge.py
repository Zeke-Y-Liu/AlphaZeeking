"""增量合并与日历尾部豁免测试（T-fix：数据获取两问题的修复回归）

问题背景（2026-10-09 实测）：
1. 新浪港股个别标的收盘后发布滞后数小时（09988.HK 的当日 K 线在收盘 ~6h 后才出现），
   而 calendar_check 末根校验全市场连坐 → 单标的滞后 → 整市场 FAIL，反复同步仍红；
2. fetch_all 增量路径 rmtree 后仅写增量窗口（不合并旧数据）→ 一次 incremental 即历史
   全丢/空快照 → 六项验证全红，把上次已绿状态也拉红。

覆盖：
- ``datasource._merge_bars``：并集合并 / 重叠新胜 / 空窗口保旧 / 边界 pct 修复 /
  公司行动阈值守卫；
- ``datasource._snapshot_mergeable``：快照与缺文件降级；
- ``runner._calendar_check`` 尾部豁免三分支：发布滞后豁免（1 日内 + 腾讯有更新）/
  尾部无行情豁免（腾讯亦无）/ 真缺失不豁免（≥2 日 + 腾讯有更新 → FAIL）。

运行：``cd alphapocket && python -m pytest tests/test_datasource_merge.py -q``
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from applib import datasource as DS
from applib import runner as R
from applib.ctx import Ctx


# ── _merge_bars：纯函数 ──────────────────────────────────────────

def test_merge_bars_append_and_order():
    old = [{"dt": "2026-10-08", "close": 10.0, "pct": 0.01}]
    new = [{"dt": "2026-10-09", "close": 10.2, "pct": None}]
    merged = DS._merge_bars(old, new)
    assert [r["dt"] for r in merged] == ["2026-10-08", "2026-10-09"]
    # 窗口首根 pct=None 且旧尾存在 → 补全边界收益
    assert merged[1]["pct"] == pytest.approx(10.2 / 10.0 - 1)


def test_merge_bars_overlap_new_wins():
    old = [{"dt": "2026-10-08", "close": 10.0, "pct": 0.01},
           {"dt": "2026-10-09", "close": 99.0, "pct": 8.9}]     # 旧值错误
    new = [{"dt": "2026-10-09", "close": 10.2, "pct": 0.02}]    # 重拉修正
    merged = DS._merge_bars(old, new)
    assert len(merged) == 2
    assert merged[1]["close"] == 10.2 and merged[1]["pct"] == 0.02


def test_merge_bars_empty_window_keeps_old():
    old = [{"dt": "2026-10-08", "close": 10.0, "pct": 0.01}]
    assert DS._merge_bars(old, []) == old
    assert DS._merge_bars(old, []) is not old        # 返回拷贝，防外部篡改
    assert DS._merge_bars([], [{"dt": "2026-10-09", "close": 1.0}]) == [
        {"dt": "2026-10-09", "close": 1.0}]


def test_merge_bars_corp_action_guard_keeps_none():
    # 窗口首根跳变 >33% → 疑似份额折算/拆分，pct 保持 None（与 fund 主链阈值一致）
    old = [{"dt": "2026-10-08", "close": 1.0, "pct": 0.0}]
    new = [{"dt": "2026-10-09", "close": 2.0, "pct": None}]
    merged = DS._merge_bars(old, new)
    assert merged[1]["pct"] is None


def test_merge_bars_first_new_pct_present_untouched():
    # 港股/基金路径窗口首根本就带 pct（全量链切片）→ 不改写
    old = [{"dt": "2026-10-08", "close": 10.0, "pct": 0.01}]
    new = [{"dt": "2026-10-09", "close": 10.2, "pct": 0.0199}]
    merged = DS._merge_bars(old, new)
    assert merged[1]["pct"] == 0.0199


# ── _snapshot_mergeable：快照 IO ─────────────────────────────────

def test_snapshot_mergeable_reads_and_degrades(tmp_path: Path):
    repo = tmp_path / "repo"
    (repo / "market").mkdir(parents=True)
    (repo / "market" / "daily_bars.json").write_text(
        json.dumps({"00700.HK": [{"dt": "2026-10-09", "close": 1.0}]}), encoding="utf-8")
    (repo / "market" / "daily_bars_raw.json").write_text("{not json", encoding="utf-8")  # 损坏
    snap = DS._snapshot_mergeable(repo)
    assert snap["daily_bars"] == {"00700.HK": [{"dt": "2026-10-09", "close": 1.0}]}
    assert snap["daily_bars_raw"] == {}             # 损坏 → 空，不抛
    assert snap["cross_check_tx"] == {}              # 缺文件 → 空


# ── _calendar_check：尾部豁免三分支 ──────────────────────────────

def _mk_ws(tmp_path: Path, *, bars: dict, tx: dict, hk_cal: list[str] | None = None,
           a_cal: list[str] | None = None) -> Ctx:
    """构造最小 workspace（sandbox/repo/market/*），供 _calendar_check 直读。"""
    ctx = Ctx(root=tmp_path)
    mkt = ctx.local_repo / "market"
    mkt.mkdir(parents=True, exist_ok=True)
    (mkt / "daily_bars.json").write_text(json.dumps(bars), encoding="utf-8")
    (mkt / "cross_check_tx.json").write_text(json.dumps(tx), encoding="utf-8")
    cal = a_cal or ["2026-10-07", "2026-10-08", "2026-10-09"]
    (mkt / "trade_calendar.json").write_text(json.dumps(cal), encoding="utf-8")
    if hk_cal is not None:
        (mkt / "trade_calendar_hk.json").write_text(json.dumps(hk_cal), encoding="utf-8")
    return ctx


def _bars(*dts: str) -> list[dict]:
    return [{"dt": d, "close": 1.0} for d in dts]


def test_calendar_tail_lag_exempt(tmp_path: Path, monkeypatch):
    """发布滞后豁免：09988 落后 1 交易日、腾讯有 10-09 → pass + 透明登记，不再连坐。"""
    monkeypatch.setattr(R.time, "strftime", lambda *_a, **_k: "2026-10-09")
    ctx = _mk_ws(
        tmp_path,
        bars={"00700.HK": _bars("2026-10-07", "2026-10-08", "2026-10-09"),
              "09988.HK": _bars("2026-10-07", "2026-10-08")},
        tx={"00700.HK": _bars("2026-10-09"), "09988.HK": _bars("2026-10-09")},
        hk_cal=["2026-10-07", "2026-10-08", "2026-10-09"],
    )
    msg = R._calendar_check(ctx)["calendar_check"]
    assert msg.startswith("pass(")
    assert "发布滞后豁免1只(09988.HK@2026-10-08)" in msg


def test_calendar_tail_suspension_exempt(tmp_path: Path, monkeypatch):
    """尾部无行情豁免：腾讯亦无更新（停牌）→ pass + 计数。"""
    monkeypatch.setattr(R.time, "strftime", lambda *_a, **_k: "2026-10-09")
    ctx = _mk_ws(
        tmp_path,
        bars={"00700.HK": _bars("2026-10-07", "2026-10-08", "2026-10-09"),
              "09988.HK": _bars("2026-10-07", "2026-10-08")},
        tx={"00700.HK": _bars("2026-10-09"), "09988.HK": _bars("2026-10-08")},
        hk_cal=["2026-10-07", "2026-10-08", "2026-10-09"],
    )
    msg = R._calendar_check(ctx)["calendar_check"]
    assert msg.startswith("pass(")
    assert "尾部无行情豁免1只" in msg


def test_calendar_tail_real_gap_still_fails(tmp_path: Path, monkeypatch):
    """真缺失不豁免：落后 ≥2 交易日且腾讯有更新 → FAIL（持续上游故障须暴露）。"""
    monkeypatch.setattr(R.time, "strftime", lambda *_a, **_k: "2026-10-09")
    ctx = _mk_ws(
        tmp_path,
        bars={"00700.HK": _bars("2026-10-07", "2026-10-08", "2026-10-09"),
              "09988.HK": _bars("2026-10-06")},
        tx={"00700.HK": _bars("2026-10-09"), "09988.HK": _bars("2026-10-09")},
        hk_cal=["2026-10-06", "2026-10-07", "2026-10-08", "2026-10-09"],
    )
    msg = R._calendar_check(ctx)["calendar_check"]
    assert msg.startswith("FAIL(")
    assert "末根K线" in msg


def test_calendar_all_current_still_strict(tmp_path: Path, monkeypatch):
    """无豁免场景不放松：全部末根=最近交易日 → 普通 pass，无豁免注记。"""
    monkeypatch.setattr(R.time, "strftime", lambda *_a, **_k: "2026-10-09")
    ctx = _mk_ws(
        tmp_path,
        bars={"00700.HK": _bars("2026-10-08", "2026-10-09"),
              "09988.HK": _bars("2026-10-08", "2026-10-09")},
        tx={"00700.HK": _bars("2026-10-09"), "09988.HK": _bars("2026-10-09")},
        hk_cal=["2026-10-07", "2026-10-08", "2026-10-09"],
    )
    msg = R._calendar_check(ctx)["calendar_check"]
    assert msg.startswith("pass(")
    assert "豁免" not in msg
