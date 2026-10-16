"""组件化 T-R7：合并策略收敛（restore_guard spec 驱动 + 注册表单一事实源）。

锚点：T-R4 三语义行为逐字段等价（writeguard 既有 8 条测试保持全绿）、
T-R5/T-R6 spec 驱动链路不回退、落盘格式差异保持（registry/portfolio 缩进 2
且 `_merge` 键不落盘；bars 类紧凑 + dt_total）。
"""
from __future__ import annotations

import json
from pathlib import Path

from applib import datasets as DS
from applib import writeguard as WG


# ── 注册表单一事实源 ─────────────────────────────────────────────────────
def test_known_policies_derived_from_registry():
    assert DS.KNOWN_MERGE_POLICIES == tuple(DS.MERGE_POLICY_FN)
    assert set(DS.MERGE_POLICY_FN) == {"overwrite", "union_bars",
                                       "registry_merge", "portfolio_merge", "carry_old"}
    # 委托零第二实现：union_bars/carry_old 原生实现，registry/portfolio 委托 writeguard
    assert DS.MERGE_POLICY_FN["union_bars"] is WG.merge_bars
    assert DS.MERGE_POLICY_FN["carry_old"] is DS.carry_old_merge


def test_merge_by_policy_overwrite_passthrough():
    s = DS.DatasetSpec(dataset_id="x", path="m/x.json", merge_policy="overwrite",
                       sources=(DS.SourceSpec(interface="i", upstream="u"),))
    new = {"k": 1}
    merged, info = DS.merge_by_policy(s, new, {"old": True})
    assert merged == new and info == {}          # overwrite=新内容即最终态


def test_merge_by_policy_registry_adapter_pops_merge():
    s = DS.DatasetSpec(dataset_id="x", path="factors/registry.json",
                       merge_policy="registry_merge",
                       sources=(DS.SourceSpec(interface="i", upstream="u"),))
    old = {"factors": [{"id": "a", "status": "paused", "owner": "用户"}]}
    new = {"factors": [{"id": "a", "status": "active"}]}
    merged, info = DS.merge_by_policy(s, new, old)
    assert "_merge" not in merged                # 适配器摘出（restore 落盘前无需 pop）
    assert merged["factors"][0]["status"] == "paused" and merged["factors"][0]["owner"] == "用户"
    assert info["user_fields_kept"] == {"a": ["status", "owner"]}


# ── 守护面 ↔ 声明对齐 ────────────────────────────────────────────────────
def test_guarded_rels_all_specified():
    by_path = {s.path: s for s in DS.registry_specs().values()}
    for rel in WG.GUARDED_RELS:
        assert rel in by_path, f"守护面 {rel} 无 DatasetSpec 声明"
        assert by_path[rel].merge_policy != "overwrite", f"守护面 {rel} 不得为 overwrite"


# ── restore_guard spec 驱动（等价锚点）────────────────────────────────────
def _mk_repo(tmp: Path, rel: str, payload) -> None:
    p = tmp / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")


def test_restore_guard_registry_via_spec(tmp_path):
    snap = {"factors/registry.json":
            {"factors": [{"id": "a", "status": "paused", "owner": "用户"}]}}
    _mk_repo(tmp_path, "factors/registry.json",
             {"factors": [{"id": "a", "status": "active"}]})
    summary = WG.restore_guard(tmp_path, snap)
    assert summary["factors/registry.json"]["user_fields_kept"] == {"a": ["status", "owner"]}
    out = json.loads((tmp_path / "factors/registry.json").read_text())
    assert "_merge" not in out                                    # 不落盘
    assert out["factors"][0]["status"] == "paused"
    assert '\n  "factors"' in (tmp_path / "factors/registry.json").read_text()  # 缩进 2 保持


def test_restore_guard_bars_compact_and_dt_total(tmp_path):
    old_bars = {"s1": [{"dt": "2024-01-01", "close": 10.0, "pct": None},
                       {"dt": "2024-01-02", "close": 11.0, "pct": 0.1}]}
    snap = {"market/daily_bars.json": old_bars}
    _mk_repo(tmp_path, "market/daily_bars.json",
             {"s1": [{"dt": "2024-01-02", "close": 11.0, "pct": 0.1},
                     {"dt": "2024-01-03", "close": 12.0, "pct": None}]})
    summary = WG.restore_guard(tmp_path, snap)
    assert summary["market/daily_bars.json"]["dt_total"] == 3
    assert "resume" in summary["market/daily_bars.json"]
    out = json.loads((tmp_path / "market/daily_bars.json").read_text())
    assert [r["dt"] for r in out["s1"]] == ["2024-01-01", "2024-01-02", "2024-01-03"]
    # 紧凑格式保持（无 indent：整行单行 JSON）
    raw = (tmp_path / "market/daily_bars.json").read_text()
    assert raw.count("\n") <= 1


def test_restore_guard_unspecified_rel_logged(tmp_path):
    """守护面出现无声明文件 → 显式登记不静默（不抛异常、不动文件）。"""
    snap = {"market/unknown.json": {"k": 1}}
    _mk_repo(tmp_path, "market/unknown.json", {"k": 2})
    summary = WG.restore_guard(tmp_path, snap)
    assert summary["market/unknown.json"].get("unspecified") is True
    assert json.loads((tmp_path / "market/unknown.json").read_text()) == {"k": 2}


# ── 策略边界分支（DoD 2：每策略含边界单测）────────────────────────────────
def test_merge_bars_empty_window_keeps_all_old():
    """union_bars 边界：本次拉取空窗口（new={}）→ 全部旧数据整段保留。"""
    old = {"s1": [{"dt": "2024-01-01", "close": 10.0}], "s2": [{"dt": "2024-01-02", "close": 5.0}]}
    out, info = WG.merge_bars(old, {})
    assert out == old and info["kept_secids"] == ["s1", "s2"]


def test_merge_registry_machine_fields_new_wins():
    """registry_merge 边界：机器维护字段（version 等）新值胜出（用户改动优先仅限人工字段）。"""
    old = {"factors": [{"id": "a", "version": "v0.9", "status": "paused", "owner": "用户"}]}
    new = {"factors": [{"id": "a", "version": "v1.0", "status": "active"}]}
    merged = WG.merge_registry(old, new)
    info = merged.pop("_merge", {})
    f = merged["factors"][0]
    assert f["version"] == "v1.0"                       # 机器字段：新胜
    assert f["status"] == "paused" and f["owner"] == "用户"   # 人工字段：用户胜出
    assert info["user_fields_kept"] == {"a": ["status", "owner"]}
