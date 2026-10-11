"""组件化 T-R9：engine.metrics 四工具（list/add/update/remove）。

锚点：list 空态 / add 幂等不覆盖用户改动 / update 字段级留痕 / remove orphan 不物理删；
操作留痕不可关闭（operations 链逐次登记）。
"""
from __future__ import annotations

import json

import pytest

from applib import tools as T
from applib.ctx import Ctx


def _mk_ctx(tmp_path, factors=None):
    sb = tmp_path / "sandbox" / "repo" / "factors"   # 工具面读写 local_repo（sandbox 副本）
    sb.mkdir(parents=True, exist_ok=True)
    (sb / "registry.json").write_text(json.dumps(
        {"factors": factors or []}, ensure_ascii=False, indent=2))
    return Ctx(remote_dir=tmp_path, sandbox_dir=tmp_path / "sandbox")


def test_list_empty_and_full(tmp_path):
    ctx = _mk_ctx(tmp_path)                        # 空态：无 factors 键不炸
    out = T.engine_metrics_list({}, ctx)
    assert out["factors"] == [] and out["n_factors"] == 0
    ctx2 = _mk_ctx(tmp_path / "b", factors=[{"id": "val_pe_ttm", "status": "active"}])
    out2 = T.engine_metrics_list({}, ctx2)
    assert out2["n_factors"] == 1 and out2["n_datasets"] >= 15   # DatasetSpec 声明派生（T-R5）


def test_add_idempotent_keeps_user_changes(tmp_path):
    ctx = _mk_ctx(tmp_path, factors=[{"id": "mom_12_1", "status": "paused", "owner": "用户R9"}])
    r1 = T.engine_metrics_add({"id": "mom_12_1", "type": "alpha", "name": "重名登记"}, ctx)
    assert r1["added"] is False and "幂等" in r1["reason"]
    reg = json.loads((tmp_path / "sandbox" / "repo" / "factors" / "registry.json").read_text())
    f = next(f for f in reg["factors"] if f["id"] == "mom_12_1")
    assert f["status"] == "paused" and f["owner"] == "用户R9"   # 用户改动不被覆盖
    r2 = T.engine_metrics_add({"id": "new_fac", "type": "signal"}, ctx)
    assert r2["added"] is True and r2["status"] == "draft"       # 默认 draft
    reg2 = json.loads((tmp_path / "sandbox" / "repo" / "factors" / "registry.json").read_text())
    assert reg2["operations"][-1]["op"] == "add"                 # 留痕不可关闭


def test_add_rejects_bad_input(tmp_path):
    ctx = _mk_ctx(tmp_path)
    with pytest.raises(ValueError, match="id"):
        T.engine_metrics_add({"type": "alpha"}, ctx)
    with pytest.raises(ValueError, match="type 非法"):
        T.engine_metrics_add({"id": "x", "type": "gamma"}, ctx)


def test_update_field_level_with_before_snapshot(tmp_path):
    ctx = _mk_ctx(tmp_path, factors=[{"id": "f1", "status": "active", "version": "v1.0"}])
    r = T.engine_metrics_update({"id": "f1", "fields": {"status": "paused", "note": "人工暂停"}}, ctx)
    assert r["updated"] is True and r["changed"] == {"status": "active"}   # before 快照
    reg = json.loads((tmp_path / "sandbox" / "repo" / "factors" / "registry.json").read_text())
    f = reg["factors"][0]
    assert f["status"] == "paused" and f["note"] == "人工暂停" and f["version"] == "v1.0"
    assert reg["operations"][-1]["fields"] == ["note", "status"]
    with pytest.raises(ValueError, match="id 不可修改"):
        T.engine_metrics_update({"id": "f1", "fields": {"id": "f2"}}, ctx)
    r3 = T.engine_metrics_update({"id": "ghost", "fields": {"status": "x"}}, ctx)
    assert r3["updated"] is False


def test_remove_orphan_keeps_physical_entry(tmp_path):
    ctx = _mk_ctx(tmp_path, factors=[{"id": "old_f", "status": "active"}])
    r = T.engine_metrics_remove({"id": "old_f"}, ctx)
    assert r["removed"] is True and r["status"] == "orphan"
    reg = json.loads((tmp_path / "sandbox" / "repo" / "factors" / "registry.json").read_text())
    f = reg["factors"][0]
    assert f["status"] == "orphan" and "removed_at" in f          # 条目物理保留
    assert reg["operations"][-1]["op"] == "remove" and reg["operations"][-1]["before"] == {"status": "active"}
    r2 = T.engine_metrics_remove({"id": "old_f"}, ctx)            # 二次 remove 幂等
    assert r2["removed"] is False and "orphan" in r2["reason"]


def test_write_protection_pipeline_after_tool_ops(tmp_path):
    """工具写库后 → fetch 合并（writeguard.registry_merge）：机器字段新胜/用户字段保留（联动）。"""
    from applib.writeguard import merge_registry
    ctx = _mk_ctx(tmp_path, factors=[{"id": "f1", "status": "active", "owner": "用户R9"}])
    T.engine_metrics_update({"id": "f1", "fields": {"note": "工具改动"}}, ctx)
    local = json.loads((tmp_path / "sandbox" / "repo" / "factors" / "registry.json").read_text())
    remote_like = {"factors": [{"id": "f1", "status": "active", "version": "v2.0"}]}   # 拉取覆盖写
    merged = merge_registry(local, remote_like)
    f = next(x for x in merged["factors"] if x["id"] == "f1")
    assert f["owner"] == "用户R9" and f["note"] == "工具改动"       # 用户/工具改动保留
    assert f["version"] == "v2.0"                                  # 机器字段新胜
