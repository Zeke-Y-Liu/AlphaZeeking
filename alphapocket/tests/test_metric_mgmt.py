"""指标全生命周期管理测试（T-R7：apply_metric_change + MCP 四工具 + CLI metric）

覆盖：add 成功/缺 hypothesis_id 拒绝/未知 formula 拒绝/重复 id 拒绝；update 点路径
与 version 自增/status 翻转需 hypothesis_id/非法字段拒绝；remove 软删（条目仍在，
status=inactive）；changelog before/after 与 FIFO 截断；list 过滤；MCP 四工具
round-trip（call_tool 走查 + 错误风格）；端到端行为生效（add → factor.macro 打分、
remove → missing=inactive）；CLI metric 冒烟。

运行：cd alphapocket && python -m pytest tests/test_metric_mgmt.py -q
"""
from __future__ import annotations

import json

import pytest

from applib import cli as CLI
from applib.ctx import Ctx
from applib.engines.base import (
    CHANGELOG_MAX,
    apply_metric_change,
    export_registry_v2,
    validate_registry,
)
from applib.tools import call_tool


# ── 变更应用器（纯函数）────────────────────────────────────────

def _add_m2(reg=None, **kw):
    return apply_metric_change(reg or export_registry_v2(), "macro", {
        "op": "add", "id": "test_liq_x",
        "patch": {"formula": "builtin:macro_series", "dim": "D3", "market": "A",
                  "dataset": "macro_liquidity", "scorer": {"type": "quant", "arg": None}},
        "hypothesis_id": "H-7001", "reason": "测试新增", **kw})


def test_add_ok_and_validate():
    """add 成功：条目入场、changelog 记录、结果必过 validate_registry（红线）。"""
    reg = export_registry_v2()
    new_reg, rec = _add_m2(reg)
    assert validate_registry(new_reg) == []
    got = next(m for m in new_reg["engines"]["macro"]["metrics"] if m["id"] == "test_liq_x")
    assert got["status"] == "active" and got["scorer"] == {"type": "quant", "arg": None}
    assert got["hypothesis_id"] == "H-7001" and got["version"] == 1
    assert rec["op"] == "add" and rec["before"] is None and rec["after"]["id"] == "test_liq_x"
    assert new_reg["changelog"][-1]["op"] == "add"           # 追加式记录
    assert reg["engines"]["macro"]["metrics"] == export_registry_v2()["engines"]["macro"]["metrics"]  # 入参不被改


def test_add_requires_hypothesis_id():
    """add 缺 hypothesis_id → ValueError（来源可溯红线）。"""
    with pytest.raises(ValueError, match="hypothesis_id"):
        apply_metric_change(export_registry_v2(), "macro", {
            "op": "add", "id": "test_liq_x", "patch": {"formula": "builtin:macro_series"}})


def test_add_unknown_formula_rejected():
    """add 未知 formula → ValueError 并提示先在 factors.py 实现。"""
    with pytest.raises(ValueError, match="factors.py"):
        apply_metric_change(export_registry_v2(), "macro", {
            "op": "add", "id": "test_liq_x", "patch": {"formula": "builtin:no_such_formula"},
            "hypothesis_id": "H-7001"})


def test_add_duplicate_id_rejected():
    """add 重复 id → ValueError（应改用 update）。"""
    reg, _ = _add_m2()
    with pytest.raises(ValueError, match="已存在"):
        _add_m2(reg)


def test_update_dotpath_merge_and_version_bump():
    """update：params 深合并（部分更新）、version 自增、其余字段整值替换。"""
    reg = export_registry_v2()
    new_reg, rec = apply_metric_change(reg, "alpha", {
        "op": "update", "id": "rev_20d", "patch": {"params": {"n": 25}, "note": "调参"},
        "reason": "OAT 扫描最优"})
    got = next(m for m in new_reg["engines"]["alpha"]["metrics"] if m["id"] == "rev_20d")
    assert got["params"] == {"n": 25}                        # 深合并
    assert got["version"] == 2 and got["note"] == "调参"
    assert got["status"] == "active" and got["dim"] == "动量"  # 未触碰字段保持
    assert rec["before"]["version"] == 1 and rec["after"]["version"] == 2


def test_update_status_flip_requires_hypothesis():
    """update 启停：无 hypothesis_id 拒绝；带则成功并回写条目来源。"""
    reg = export_registry_v2()
    with pytest.raises(ValueError, match="hypothesis_id"):
        apply_metric_change(reg, "alpha", {"op": "update", "id": "mom_12_1",
                                           "patch": {"status": "inactive"}})
    new_reg, _ = apply_metric_change(reg, "alpha", {"op": "update", "id": "mom_12_1",
                                                    "patch": {"status": "inactive"},
                                                    "hypothesis_id": "H-7002"})
    got = next(m for m in new_reg["engines"]["alpha"]["metrics"] if m["id"] == "mom_12_1")
    assert got["status"] == "inactive" and got["hypothesis_id"] == "H-7002"


def test_update_rejects_code_owned_fields():
    """update 非法字段（formula/id/engine 属代码所有）→ ValueError。"""
    with pytest.raises(ValueError, match="非法字段"):
        apply_metric_change(export_registry_v2(), "alpha", {
            "op": "update", "id": "mom_12_1", "patch": {"formula": "builtin:hack"}})


def test_remove_soft_delete():
    """remove 软删红线：条目仍在 status=inactive；缺 hypothesis_id 拒绝；changelog 有 before。"""
    reg = export_registry_v2()
    with pytest.raises(ValueError, match="hypothesis_id"):
        apply_metric_change(reg, "macro", {"op": "remove", "id": "m2_yoy"})
    new_reg, rec = apply_metric_change(reg, "macro", {
        "op": "remove", "id": "m2_yoy", "hypothesis_id": "H-7003", "reason": "信度不足"})
    got = next(m for m in new_reg["engines"]["macro"]["metrics"] if m["id"] == "m2_yoy")
    assert got["status"] == "inactive"                       # 软删：条目与历史保留
    assert rec["before"]["status"] == "active" and rec["after"]["status"] == "inactive"
    assert validate_registry(new_reg) == []


def test_changelog_before_after_and_fifo():
    """changelog 记 before/after；超 CHANGELOG_MAX FIFO 截断；show 不落 changelog。"""
    reg = export_registry_v2()
    _, rec = apply_metric_change(reg, "alpha", {"op": "update", "id": "rev_20d",
                                                "patch": {"params": {"n": 30}}})
    assert rec["before"]["params"] == {"n": 20} and rec["after"]["params"] == {"n": 30}
    _, show = apply_metric_change(reg, "alpha", {"op": "show", "id": "rev_20d"})
    assert show["after"]["id"] == "rev_20d" and "ts" not in show   # show 只读

    reg2 = export_registry_v2()
    for i in range(CHANGELOG_MAX + 5):
        reg2, _ = apply_metric_change(reg2, "alpha", {"op": "update", "id": "rev_20d",
                                                      "patch": {"note": f"n{i}"}})
    assert len(reg2["changelog"]) == CHANGELOG_MAX           # FIFO 截断
    assert reg2["changelog"][-1]["after"]["note"] == f"n{CHANGELOG_MAX + 4}"
    assert reg2["changelog"][0]["after"]["note"] == f"n5"    # 最早的 5 条被挤出


def test_list_filter(tmp_path):
    """engine.metrics.list：按引擎/状态过滤 + changelog 计数。"""
    reg = export_registry_v2()
    reg, _ = _add_m2(reg)
    reg, _ = apply_metric_change(reg, "macro", {"op": "remove", "id": "m2_yoy",
                                                "hypothesis_id": "H-7003"})
    ctx = Ctx(root=str(tmp_path))
    ctx.ensure_sandbox()
    (ctx.local_repo / "factors").mkdir(parents=True, exist_ok=True)
    (ctx.local_repo / "factors" / "registry.json").write_text(
        json.dumps(reg, ensure_ascii=False), encoding="utf-8")

    res = call_tool("engine.metrics.list", {"engine": "macro", "status": "inactive"}, ctx)
    ids = {m["id"] for m in res["metrics"]}
    assert ids == {"m2_yoy"} and res["changelog_n"] == 2
    res_all = call_tool("engine.metrics.list", {"engine": "alpha"}, ctx)
    assert {m["id"] for m in res_all["metrics"]} >= {"val_pe_ttm", "mom_12_1"}


# ── MCP 四工具 round-trip（call_tool 走查）───────────────────────

def test_mcp_roundtrip(tmp_path):
    """list → add → show → update → remove 全链路；校验失败返回 {'error': 中文} 不抛异常。"""
    ctx = Ctx(root=str(tmp_path))
    ctx.ensure_sandbox()
    spec = {"id": "test_liq_x", "formula": "builtin:macro_series", "dim": "D3",
            "market": "A", "dataset": "macro_liquidity",
            "scorer": {"type": "quant", "arg": None}}
    r1 = call_tool("engine.metrics.add", {"engine": "macro", "spec": spec,
                                          "hypothesis_id": "H-7001", "reason": "新增"}, ctx)
    assert r1["ok"] and r1["metric"]["status"] == "active"

    r2 = call_tool("engine.metrics.update", {"id": "test_liq_x",
                                             "patch": {"note": "调参", "status": "experimental"},
                                             "hypothesis_id": "H-7002"}, ctx)
    assert r2["ok"] and r2["before"]["status"] == "active" and r2["after"]["status"] == "experimental"

    r3 = call_tool("engine.metrics.remove", {"id": "test_liq_x",
                                             "hypothesis_id": "H-7003", "reason": "退役"}, ctx)
    assert r3["ok"] and r3["metric"]["status"] == "inactive"

    r4 = call_tool("engine.metrics.list", {"engine": "macro", "status": "inactive"}, ctx)
    assert "test_liq_x" in {m["id"] for m in r4["metrics"]} and r4["changelog_n"] == 3

    # 盘上文件已写入且合法（load_registry 可加载）
    from applib.engines.base import load_registry
    loaded = load_registry(ctx.local_repo)
    assert validate_registry(loaded) == []

    # 错误风格：{"error": 中文}，不抛异常
    e1 = call_tool("engine.metrics.add", {"engine": "macro", "spec": spec,
                                          "hypothesis_id": "H-7001"}, ctx)   # 重复 id
    assert "error" in e1 and "已存在" in e1["error"]
    e2 = call_tool("engine.metrics.update", {"id": "ghost_x", "patch": {"note": "x"}}, ctx)
    assert "error" in e2 and "不存在" in e2["error"]
    e3 = call_tool("engine.metrics.remove", {"id": "m2_yoy", "engine": "macro"}, ctx)
    assert "error" in e3 and "hypothesis_id" in e3["error"]


# ── 端到端行为生效：改注册表即改引擎行为（经 T-R3 配置链路）────────

_MACRO_MIN = {k: [{"dt": "2026-09-01", "v": v} for v in vals] for k, vals in {
    "rrr_big": [9.5, 9.0], "m2_yoy": [6.0, 6.5], "tsf_new_12m": [200000.0, 210000.0],
    "shibor_1m": [1.8, 1.7], "lpr_1y": [3.5, 3.0], "cn_us_10y": [2.5],
    "us_10y_2y": [0.4], "fed_target": [4.5], "us_cpi_yoy": [3.4], "us_cpi_surprise": [-0.1],
}.items()}
_MACRO_META_MIN = {k: {"upstream": "测试机构", "freq": "月"} for k in _MACRO_MIN}


def test_e2e_add_then_factor_macro_scores(tmp_path):
    """add 宏观新指标（quant/D3/A）→ factor.macro 打分入场；remove → missing=inactive。"""
    ctx = Ctx(root=str(tmp_path))
    ctx.ensure_sandbox()
    md = ctx.local_repo / "market"
    md.mkdir(parents=True, exist_ok=True)
    series = {**_MACRO_MIN, "test_liq_x": [{"dt": "2026-09-01", "v": 1.0},
                                           {"dt": "2026-09-02", "v": 1.2}]}
    (md / "macro_liquidity.json").write_text(
        json.dumps({"series": series, "meta": _MACRO_META_MIN}, ensure_ascii=False), encoding="utf-8")

    spec = {"id": "test_liq_x", "formula": "builtin:macro_series", "dim": "D3",
            "market": "A", "dataset": "macro_liquidity",
            "scorer": {"type": "quant", "arg": None}}
    r = call_tool("engine.metrics.add", {"engine": "macro", "spec": spec,
                                         "hypothesis_id": "H-7001"}, ctx)
    assert r["ok"]

    res = call_tool("factor.macro", {}, ctx)                 # registry 驱动（T-R3 链路）
    assert "error" not in res
    ind = res["indicators"]["A"]["test_liq_x"]
    assert ind["score"] is not None and ind["missing"] is False   # 参与打分
    d3 = res["markets"]["A"]["dims"]["D3"]
    assert d3["n_total"] == 3                               # D3 分母：默认 shibor/lpr + 新指标

    call_tool("engine.metrics.remove", {"id": "test_liq_x",
                                        "hypothesis_id": "H-7003"}, ctx)
    res2 = call_tool("factor.macro", {}, ctx)
    ind2 = res2["indicators"]["A"]["test_liq_x"]
    assert ind2["missing"] == "inactive" and ind2["score"] is None   # 软删 → 不参与打分
    assert res2["markets"]["A"]["dims"]["D3"]["n_total"] == 2        # 分母剔除（inactive 不进分母）


# ── CLI metric 冒烟 ─────────────────────────────────────────────

def _ns(**kw):
    base = dict(action="list", id=None, engine=None, status=None, formula=None, dim=None,
                market=None, scorer_type=None, scorer_arg=None, params_json=None,
                set_kv=[], weights_json=None, hypothesis_id=None, reason=None, as_json=False)
    base.update(kw)
    return type("A", (), base)()


def test_cli_metric_smoke(tmp_path, monkeypatch, capsys):
    """CLI：list → add（写盘+提示）→ show → update 点路径 → remove；缺参 SystemExit。"""
    ctx = Ctx(root=str(tmp_path))
    ctx.ensure_sandbox()
    monkeypatch.setattr(CLI, "Ctx", lambda: ctx)

    CLI.cmd_metric(_ns(action="list", engine="macro"))
    out = capsys.readouterr().out
    assert "m2_yoy" in out and "共" in out

    CLI.cmd_metric(_ns(action="add", engine="macro", id="test_liq_x",
                       formula="builtin:macro_series", dim="D3", market="A",
                       scorer_type="quant", hypothesis_id="H-7001", reason="测试"))
    out = capsys.readouterr().out
    assert "已新增 macro.test_liq_x" in out and "ap push" in out
    assert (ctx.local_repo / "factors" / "registry.json").exists()   # 已写 sandbox

    CLI.cmd_metric(_ns(action="update", id="test_liq_x", set_kv=["params.win=25", "note=调参"]))
    capsys.readouterr()
    reg = json.loads((ctx.local_repo / "factors" / "registry.json").read_text(encoding="utf-8"))
    got = next(m for m in reg["engines"]["macro"]["metrics"] if m["id"] == "test_liq_x")
    assert got["params"] == {"win": 25} and got["note"] == "调参" and got["version"] == 2

    with pytest.raises(SystemExit, match="hypothesis_id"):
        CLI.cmd_metric(_ns(action="update", id="test_liq_x", set_kv=["status=inactive"]))
    capsys.readouterr()

    CLI.cmd_metric(_ns(action="remove", id="test_liq_x", hypothesis_id="H-7003"))
    out = capsys.readouterr().out
    assert "软删" in out
    reg = json.loads((ctx.local_repo / "factors" / "registry.json").read_text(encoding="utf-8"))
    got = next(m for m in reg["engines"]["macro"]["metrics"] if m["id"] == "test_liq_x")
    assert got["status"] == "inactive" and len(reg["changelog"]) == 3

    with pytest.raises(SystemExit):
        CLI.cmd_metric(_ns(action="show", id="ghost_x"))
