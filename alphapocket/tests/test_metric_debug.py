"""指标调试闭环测试（T-R8：engines/debug.py 编排层 + CLI test/diff）

覆盖：parse_grid 网格解析；verdict 阈值三分支；walkforward 模式 summary 四键
（beta/signal 与 alpha 两条分派）+ 注册表不被改写（人决策红线）；sensitivity 模式
beta 网格扫描与曲线截取 + 参数注入面守卫；macro 指标 walkforward → 明确 error；
diff 伪造 changelog（add + 两次语义 update）→ config_delta/output_delta 正确；
diff 快照缺失/越界/缺基线报错；产物文件落盘且可 json.load；CLI 冒烟。

运行：cd alphapocket && python -m pytest tests/test_metric_debug.py -q
"""
from __future__ import annotations

import datetime as dt
import json
import math
from pathlib import Path

import pytest

from applib import cli as CLI
from applib.ctx import Ctx
from applib.engines import debug as DBG
from applib.engines.base import apply_metric_change, export_registry_v2


# ── 夹具：合成行情（含 pct/amount，sensitivity 策略回测与 walkforward 共用）──
def _synth_bars(n_secids: int = 6, start: dt.date = dt.date(2021, 1, 4),
                end: dt.date = dt.date(2026, 9, 30)) -> dict:
    days, d = [], start
    while d <= end:
        if d.weekday() < 5:
            days.append(d.isoformat())
        d += dt.timedelta(days=1)
    bars = {}
    for k in range(n_secids):
        px, prev, arr = 100.0, None, []
        for i, day in enumerate(days):
            px *= 1 + 0.002 * math.sin((i + k) / 11.0) + 0.0004 * ((i + k) % 5 - 2)
            pct = 0.0 if prev is None else px / prev - 1
            arr.append({"dt": day, "open": round(px, 4), "high": round(px * 1.01, 4),
                        "low": round(px * 0.99, 4), "close": round(px, 4),
                        "volume": 1, "amount": 1.0, "pct": round(pct, 6)})
            prev = px
        bars[f"S{k}.XX"] = arr
    return bars


_MACRO_MIN = {k: [{"dt": "2026-09-01", "v": v} for v in vals] for k, vals in {
    "rrr_big": [9.5, 9.0], "m2_yoy": [6.0, 6.5], "tsf_new_12m": [200000.0, 210000.0],
    "shibor_1m": [1.8, 1.7], "lpr_1y": [3.5, 3.0], "cn_us_10y": [2.5],
    "us_10y_2y": [0.4], "fed_target": [4.5], "us_cpi_yoy": [3.4], "us_cpi_surprise": [-0.1],
}.items()}


def _setup_ws(tmp_path, reg=None, bars=None, macro=None, portfolio=False) -> Ctx:
    ctx = Ctx(root=str(tmp_path))
    ctx.ensure_sandbox()
    repo = ctx.local_repo
    (repo / "factors").mkdir(parents=True, exist_ok=True)
    (repo / "factors" / "registry.json").write_text(
        json.dumps(reg or export_registry_v2(), ensure_ascii=False), encoding="utf-8")
    if bars is not None or macro is not None:
        (repo / "market").mkdir(parents=True, exist_ok=True)
    if bars is not None:
        (repo / "market" / "daily_bars.json").write_text(
            json.dumps(bars, ensure_ascii=False), encoding="utf-8")
    if macro is not None:
        (repo / "market" / "macro_liquidity.json").write_text(
            json.dumps(macro, ensure_ascii=False), encoding="utf-8")
    if portfolio and bars is not None:
        (repo / "portfolio").mkdir(parents=True, exist_ok=True)
        (repo / "portfolio" / "cn4.json").write_text(json.dumps(
            {"id": "cn4", "name": "测试组合",
             "positions": [{"secid": s, "name": s} for s in bars]}, ensure_ascii=False),
            encoding="utf-8")
    return ctx


# ── 网格解析 ─────────────────────────────────────────────────

def test_parse_grid():
    assert DBG.parse_grid("win=20,25,30") == {"win": [20, 25, 30]}
    assert DBG.parse_grid("band=0.02,0.03") == {"band": [0.02, 0.03]}
    assert DBG.parse_grid("rebalance=monthly,weekly") == {"rebalance": ["monthly", "weekly"]}
    for bad in (None, "", "win", "=1,2"):
        with pytest.raises(ValueError):
            DBG.parse_grid(bad)


# ── verdict 阈值三分支（DoD①）────────────────────────────────

def test_verdict_threshold_branches():
    assert DBG._verdict(0.55) == "keep"
    assert DBG._verdict(0.9) == "keep"
    assert DBG._verdict(0.5) == "watch"
    assert DBG._verdict(0.45) == "watch"
    assert DBG._verdict(0.449) == "drop"
    assert DBG._verdict(-0.2) == "drop"
    assert DBG._verdict(None).startswith("watch")     # 无有效 IC 折 → 样本不足


# ── walkforward 模式（DoD①：summary 三键 + 两条分派；红线：注册表不被改写）──

def test_walkforward_summary_and_dispatch(tmp_path):
    ctx = _setup_ws(tmp_path, bars=_synth_bars())
    reg_before = (ctx.local_repo / "factors" / "registry.json").read_text(encoding="utf-8")

    out = DBG.metric_test(ctx, "mom_20d", tool="walkforward")       # beta → type=signal 分派
    assert "error" not in out, out
    assert out["engine"] == "beta" and out["tool"] == "walkforward"
    assert {"n_folds", "mean_ic", "hit_rate", "verdict"} <= set(out["summary"])
    assert out["summary"]["n_folds"] == out["result"]["n_folds"] == 6   # 24/6/6 → 6 折
    assert len(out["result"]["fold_ic"]) == 6
    hr = out["summary"]["hit_rate"]
    if hr is None:
        assert out["summary"]["verdict"].startswith("watch")
    else:
        assert out["summary"]["verdict"] == DBG._verdict(hr)
    assert out["result"]["lockbox"] and out["result"]["lockbox"][0] == "2026-01-01"

    out2 = DBG.metric_test(ctx, "mom_12_1", tool="walkforward")     # alpha → compute_alpha 分派
    assert "error" not in out2, out2
    assert out2["engine"] == "alpha" and out2["summary"]["n_folds"] == 6

    # 人决策红线：test 只读引擎，注册表逐字节未动；产物落盘且可 json.load
    assert (ctx.local_repo / "factors" / "registry.json").read_text(encoding="utf-8") == reg_before
    p = Path(out["file"])
    assert p.exists() and "debug_test_mom_20d_" in p.name
    loaded = json.loads(p.read_text(encoding="utf-8"))
    assert loaded["metric_id"] == "mom_20d" and loaded["summary"] == out["summary"]


def test_walkforward_horizon_override_via_cfg(tmp_path):
    """--overrides-json 命中滚动协议键（horizon_days）→ 生效进 cfg；其余键诚实忽略。"""
    ctx = _setup_ws(tmp_path, bars=_synth_bars())
    out = DBG.metric_test(ctx, "mom_20d", tool="walkforward", overrides={"horizon_days": 10})
    assert "error" not in out, out
    assert out["result"]["horizon_days"] == 10
    out2 = DBG.metric_test(ctx, "mom_20d", tool="walkforward")
    assert out2["result"]["horizon_days"] == 20                     # 默认前瞻窗口


# ── sensitivity 模式（DoD②：网格解析 + 曲线截取 + 注入面守卫）────────

def test_sensitivity_beta_grid_and_artifact(tmp_path):
    bars = _synth_bars(n_secids=3, start=dt.date(2021, 1, 4), end=dt.date(2021, 11, 1))
    ctx = _setup_ws(tmp_path, bars=bars, portfolio=True)

    out = DBG.metric_test(ctx, "mom_20d", tool="sensitivity", grid="win=12,20,30")
    assert "error" not in out, out
    assert out["engine"] == "beta" and out["result"]["portfolio"] == "cn4"
    assert out["summary"]["param"] == "win" and out["summary"]["baseline"] == 20
    assert out["summary"]["n_values"] == 3
    rows = out["result"]["rows"]
    assert [r["value"] for r in rows] == [12, 20, 30]               # 曲线截取：逐档一格
    assert all({"cagr", "sharpe", "max_dd", "rel_change", "flagged"} <= set(r) for r in rows)
    p = Path(out["file"])
    assert p.exists() and json.loads(p.read_text(encoding="utf-8"))["tool"] == "sensitivity"


def test_sensitivity_guards(tmp_path):
    """alpha 无数值参数注入面 / beta 信号级参数不进策略层 / 缺网格 → 诚实报错不硬凑。"""
    ctx = _setup_ws(tmp_path, bars=_synth_bars(n_secids=3, start=dt.date(2021, 1, 4),
                                               end=dt.date(2021, 11, 1)))
    e = DBG.metric_test(ctx, "val_pe_ttm", tool="sensitivity", grid="win=20,30")
    assert "error" in e and "alpha" in e["error"]
    e2 = DBG.metric_test(ctx, "mom_20d", tool="sensitivity", grid="bias_thresh=0.05,0.10")
    assert "error" in e2 and "win" in e2["error"]
    e3 = DBG.metric_test(ctx, "mom_20d", tool="sensitivity", grid=None)
    assert "error" in e3 and "--grid" in e3["error"]


# ── macro 指标 walkforward → 明确 error（DoD③）────────────────

def test_walkforward_macro_metric_error(tmp_path):
    ctx = _setup_ws(tmp_path)
    e = DBG.metric_test(ctx, "m2_yoy", tool="walkforward")
    assert "宏观指标无横截面回放" in e.get("error", "")


# ── diff：changelog 快照 → config_delta/output_delta（DoD④）────────

def test_diff_changelog_config_and_output_delta(tmp_path):
    reg = export_registry_v2()
    reg, _ = apply_metric_change(reg, "macro", {
        "op": "add", "id": "test_liq_x",
        "patch": {"formula": "builtin:macro_series", "dim": "D3", "market": "A",
                  "dataset": "macro_liquidity", "scorer": {"type": "clip", "arg": 1.0}},
        "hypothesis_id": "H-7001", "reason": "测试新增"})
    reg, _ = apply_metric_change(reg, "macro", {
        "op": "update", "id": "test_liq_x", "patch": {"scorer": {"arg": 3.0}},
        "hypothesis_id": "H-7002", "reason": "尺度评审"})            # v1 → v2（clip 尺度减半）
    series = {**_MACRO_MIN, "test_liq_x": [{"dt": "2026-09-01", "v": 1.0},
                                           {"dt": "2026-09-02", "v": 1.2}]}
    ctx = _setup_ws(tmp_path, reg=reg, macro={"series": series})

    out = DBG.metric_diff(ctx, "test_liq_x", baseline=1)
    assert "error" not in out, out
    assert out["engine"] == "macro"
    assert out["baseline_version"] == 1 and out["current_version"] == 2
    assert out["config_delta"] == {"scorer": {"arg": {"before": 1.0, "after": 3.0}}}
    od = out["output_delta"]
    assert od["market_score"]["before"] != od["market_score"]["after"]   # clip 1.0→3.0 改变打分
    assert od["dim_score"]["before"] != od["dim_score"]["after"]
    assert len(out["files"]) == 3                                        # 主 + before + after
    for f in out["files"]:
        json.loads(Path(f).read_text(encoding="utf-8"))                  # 可 json.load


def test_diff_beta_params_registration_note(tmp_path):
    """beta params 为登记面（runtime 走引擎 defaults）→ 输出无差异属预期，note 明示。"""
    reg = export_registry_v2()
    reg, _ = apply_metric_change(reg, "beta", {"op": "update", "id": "mom_20d",
                                               "patch": {"params": {"n": 25}}, "reason": "调参"})
    ctx = _setup_ws(tmp_path, reg=reg, bars=_synth_bars(n_secids=3,
                                                        start=dt.date(2021, 1, 4),
                                                        end=dt.date(2021, 11, 1)))
    out = DBG.metric_diff(ctx, "mom_20d", baseline=1)
    assert "error" not in out, out
    assert out["config_delta"] == {"params": {"n": {"before": 20, "after": 25}}}
    od = out["output_delta"]
    assert od["signal_count"]["before"] == od["signal_count"]["after"]
    assert "无差异" in out["note"]


# ── diff 快照缺失 / 越界 / 缺基线（DoD⑤）──────────────────────

def test_diff_snapshot_missing_and_guards(tmp_path):
    ctx = _setup_ws(tmp_path)                                        # 默认注册表：changelog 空
    e = DBG.metric_diff(ctx, "mom_12_1", baseline=1)
    assert "快照不存在" in e.get("error", "")
    e2 = DBG.metric_diff(ctx, "mom_12_1")                            # 未指定基线
    assert "--baseline" in e2.get("error", "")
    e3 = DBG.metric_diff(ctx, "mom_12_1", changelog_idx=0)           # 下标越界
    assert "下标越界" in e3.get("error", "")

    reg = export_registry_v2()
    reg["changelog"] = [{"ts": "t", "op": "update", "engine": "alpha", "id": "rev_20d",
                         "before": {"id": "rev_20d", "version": 1},
                         "after": {"id": "rev_20d", "version": 2}}]
    ctx2 = Ctx(root=str(tmp_path / "ws2"))
    ctx2.ensure_sandbox()
    (ctx2.local_repo / "factors").mkdir(parents=True, exist_ok=True)
    (ctx2.local_repo / "factors" / "registry.json").write_text(
        json.dumps(reg, ensure_ascii=False), encoding="utf-8")
    e4 = DBG.metric_diff(ctx2, "mom_12_1", changelog_idx=0)          # idx 属其他指标
    assert "rev_20d" in e4.get("error", "")
    e5 = DBG.metric_diff(ctx2, "ghost_x", baseline=1)                # 指标不存在
    assert "不存在" in e5.get("error", "")


# ── CLI 冒烟（test/diff）──────────────────────────────────────

def _ns(**kw):
    base = dict(action="list", id=None, engine=None, status=None, formula=None, dim=None,
                market=None, scorer_type=None, scorer_arg=None, params_json=None,
                set_kv=[], weights_json=None, hypothesis_id=None, reason=None, as_json=False,
                tool="walkforward", grid=None, overrides_json=None, portfolio="cn4",
                baseline=None, changelog_idx=None, ctx=None)
    base.update(kw)
    return type("A", (), base)()


def test_cli_test_and_diff_smoke(tmp_path, monkeypatch, capsys):
    bars = _synth_bars(n_secids=3, start=dt.date(2021, 1, 4), end=dt.date(2021, 11, 1))
    _setup_ws(tmp_path, bars=bars, portfolio=True)
    monkeypatch.setattr(CLI, "Ctx", lambda: Ctx(root=str(tmp_path)))

    CLI.cmd_metric(_ns(action="test", id="mom_20d", tool="walkforward"))
    out = capsys.readouterr().out
    assert "mom_20d" in out and "artifacts" in out and "verdict" in out

    CLI.cmd_metric(_ns(action="test", id="mom_20d", tool="sensitivity",
                       grid="win=12,20,30", ctx=str(tmp_path / "sandbox")))
    out = capsys.readouterr().out
    assert "win" in out and "artifacts" in out                       # --ctx 显式指向 sandbox

    with pytest.raises(SystemExit, match="--baseline"):
        CLI.cmd_metric(_ns(action="diff", id="mom_12_1"))            # 缺基线 → 中文报错退出
    capsys.readouterr()

    with pytest.raises(SystemExit, match="不存在"):
        CLI.cmd_metric(_ns(action="diff", id="ghost_x", baseline=1))
    capsys.readouterr()
