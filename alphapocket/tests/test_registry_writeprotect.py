"""registry 写保护 merge 测试（T-R6）

覆盖 merge_registry 五条核心规则 + v1 继承 + 空盘 bootstrap + 权重表整取 +
signals merge + anchors 过滤 + 逐键合并 + datasource 接入端到端（用户改动在
数据同步后完整保留、merge 结果必过 validate_registry 红线）。

运行：cd alphapocket && python -m pytest tests/test_registry_writeprotect.py -q
"""
from __future__ import annotations

import json
import logging
import shutil

import pytest

from applib.engines.base import (
    ALPHA_ANCHOR_KEYS,
    REGISTRY_VERSION,
    BaseEngine,
    export_registry_v2,
    load_registry,
    load_registry_raw,
    merge_registry,
    validate_registry,
)

LOG = logging.getLogger("ap.engines")


def _mk_alpha_metric(mid: str) -> dict:
    """从 defaults 中复制一条 alpha 指标声明（保形状）。"""
    for m in export_registry_v2()["engines"]["alpha"]["metrics"]:
        if m["id"] == mid:
            return json.loads(json.dumps(m, ensure_ascii=False))
    raise KeyError(mid)


# ── 规则1：defaults 有、on_disk 没有 → 补默认条目 ────────────────

def test_rule1_default_fills_missing():
    """盘上缺新指标（旧版代码导出）→ merge 后自动补默认条目。"""
    disk = export_registry_v2()
    disk["engines"]["alpha"]["metrics"] = [
        m for m in disk["engines"]["alpha"]["metrics"] if m["id"] != "mom_12_1"]
    disk["version"] = REGISTRY_VERSION

    merged = merge_registry(disk, export_registry_v2())

    ids = {m["id"] for m in merged["engines"]["alpha"]["metrics"]}
    assert "mom_12_1" in ids                                  # 新指标自动出现
    got = next(m for m in merged["engines"]["alpha"]["metrics"] if m["id"] == "mom_12_1")
    assert got["status"] == "active" and got["dim"] == "动量"


# ── 规则2：两边都有 → 用户改动优先（代码所有字段不随盘上）────────

def test_rule2_user_edits_win():
    """status/params/version/note 取盘上；formula/id/engine 恒取 defaults。"""
    disk = export_registry_v2()
    target = next(m for m in disk["engines"]["alpha"]["metrics"] if m["id"] == "mom_12_1")
    target.update(status="inactive", params={"n": 10}, version=3,
                  hypothesis_id="H-9001", note="用户调参",
                  formula="builtin:custom_hack", engine="beta")   # 代码所有字段尝试篡改
    disk["version"] = REGISTRY_VERSION

    merged = merge_registry(disk, export_registry_v2())
    got = next(m for m in merged["engines"]["alpha"]["metrics"] if m["id"] == "mom_12_1")

    assert got["status"] == "inactive" and got["params"] == {"n": 10}
    assert got["version"] == 3 and got["hypothesis_id"] == "H-9001" and got["note"] == "用户调参"
    assert got["formula"] == "builtin:mom_12_1"               # formula 属代码所有
    assert got["engine"] == "alpha"                           # engine 属代码所有
    # 其余未改动指标保持默认
    other = next(m for m in merged["engines"]["alpha"]["metrics"] if m["id"] == "val_pe_ttm")
    assert other["status"] == "active"


# ── 规则3：on_disk 独有 → orphan 保留，不入运行时面 ─────────────

def test_rule3_orphan_preserved():
    """盘上独有条目标 orphan:true 保留（不物理删除），引擎运行时面不可见。"""
    disk = export_registry_v2()
    ghost = _mk_alpha_metric("val_pe_ttm")
    ghost.update(id="legacy_factor_x", formula="builtin:pe_ttm_pit", status="inactive")
    disk["engines"]["alpha"]["metrics"].append(ghost)
    disk["version"] = REGISTRY_VERSION

    merged = merge_registry(disk, export_registry_v2())
    got = next(m for m in merged["engines"]["alpha"]["metrics"]
               if m["id"] == "legacy_factor_x")
    assert got["orphan"] is True and got["status"] == "inactive"
    assert got["engine"] == "alpha"                           # 归属所在节

    # orphan 不入运行时面：BaseEngine.metrics 过滤
    eng = BaseEngine.__new__(BaseEngine)
    eng.id, eng.name, eng.kind, eng.registry = "alpha", "引擎A", "cross_section", merged
    assert "legacy_factor_x" not in {m.id for m in eng.metrics()}


# ── 规则4：损坏/不可识别结构 → 整体回退 defaults + WARNING ──────

def test_rule4_corrupt_falls_back(caplog):
    """无 version 且无 factors 节（不可识别）→ 整体回退 defaults，记 WARNING。"""
    disk = {"foo": "bar", "engines": "not-a-dict"}
    with caplog.at_level(logging.WARNING, logger="ap.engines"):
        merged = merge_registry(disk, export_registry_v2())
    assert merged == export_registry_v2()
    assert any("不可识别" in r.message for r in caplog.records)


def test_rule4_v2_with_bad_engines_falls_back(caplog):
    """version==2 但 engines 非 dict → 同样整体回退。"""
    disk = {"version": 2, "engines": None}
    with caplog.at_level(logging.WARNING, logger="ap.engines"):
        merged = merge_registry(disk, export_registry_v2())
    assert merged["engines"]["alpha"]["metrics"] == export_registry_v2()["engines"]["alpha"]["metrics"]
    assert any("不可识别" in r.message for r in caplog.records)


# ── 规则5：version 恒为 2、updated_at 刷新 ──────────────────────

def test_rule5_version_and_updated_at_refreshed():
    disk = export_registry_v2()
    disk["version"] = 2
    disk["updated_at"] = "2000-01-01T00:00:00+00:00"
    merged = merge_registry(disk, export_registry_v2())
    assert merged["version"] == REGISTRY_VERSION
    assert merged["updated_at"] > "2000-01-01"                # 已刷新
    # v1 输入同样刷新
    merged_v1 = merge_registry({"factors": [{"id": "mom_12_1", "status": "inactive"}]},
                               export_registry_v2())
    assert merged_v1["version"] == REGISTRY_VERSION
    assert merged_v1["updated_at"] > "2000-01-01"


# ── v1 兼容：仅继承合法 status；独有条目迁入 orphans 停车场 ─────

def test_v1_inherit_status_and_orphan_parking():
    disk = {"factors": [
        {"id": "mom_12_1", "status": "inactive"},             # 合法 status → 继承
        {"id": "val_pe_ttm", "status": "bogus_state"},        # 非法 status → 不继承（取默认 active）
        {"id": "v1_only_indicator", "status": "active"},      # v1 独有 → orphans 停车场
    ]}
    merged = merge_registry(disk, export_registry_v2())

    mom = next(m for m in merged["engines"]["alpha"]["metrics"] if m["id"] == "mom_12_1")
    assert mom["status"] == "inactive" and mom["formula"] == "builtin:mom_12_1"
    val = next(m for m in merged["engines"]["alpha"]["metrics"] if m["id"] == "val_pe_ttm")
    assert val["status"] == "active"                          # 非法 status 不继承
    assert [o["id"] for o in merged["orphans"]] == ["v1_only_indicator"]
    assert "v1 遗留" in merged["orphans"][0]["note"]


# ── 空盘 bootstrap：on_disk=None → 全量 defaults（等价现状）─────

def test_bootstrap_empty_disk():
    d = export_registry_v2()
    assert merge_registry(None, d) == d
    assert merge_registry({}, d)["engines"] == d["engines"]    # 非 dict/空 dict 均回退


# ── 权重表整取：alpha 键集须全等 / macro 须非空子集，不逐键混合 ──

def test_dim_weights_wholesale_not_mixed():
    d = export_registry_v2()
    alpha_keys = set(d["engines"]["alpha"]["dim_weights"].keys())

    # alpha：盘上改权重 + 键集全等 → 整表取盘上（不与 defaults 逐键混合）
    disk = export_registry_v2()
    od_w = {k: 0.4 for k in alpha_keys}
    disk["engines"]["alpha"]["dim_weights"] = dict(od_w)
    disk["version"] = REGISTRY_VERSION
    merged = merge_registry(disk, export_registry_v2())
    assert merged["engines"]["alpha"]["dim_weights"] == od_w

    # alpha：盘上键集缺失一键（半新半旧）→ 拒整取，保留 defaults（保 validate）
    disk["engines"]["alpha"]["dim_weights"] = {k: 9.0 for k in list(alpha_keys)[:-1]}
    merged = merge_registry(disk, export_registry_v2())
    assert merged["engines"]["alpha"]["dim_weights"].keys() == alpha_keys

    # macro：非空子集 → 整取
    disk["engines"]["macro"]["dim_weights"] = {"D1": 0.6, "D2": 0.4}
    merged = merge_registry(disk, export_registry_v2())
    assert merged["engines"]["macro"]["dim_weights"] == {"D1": 0.6, "D2": 0.4}

    # macro：含未知键 → 拒整取，保留 defaults
    disk["engines"]["macro"]["dim_weights"] = {"D1": 0.6, "D9": 1.0}
    merged = merge_registry(disk, export_registry_v2())
    assert set(merged["engines"]["macro"]["dim_weights"].keys()) <= {"D1", "D2", "D3", "D4"}


# ── anchors 过滤未知键整取；defaults/regime 逐键合并 ────────────

def test_anchors_filtered_and_defaults_keywise():
    disk = export_registry_v2()
    d = export_registry_v2()

    # anchors：盘上改一个已知锚点 + 塞一个未知键 → 已知整取、未知被过滤
    disk["engines"]["alpha"]["anchors"] = {"s_val": [[0, 0], [100, 100]], "bogus_k": [[0, 0]]}
    merged = merge_registry(disk, d)
    assert merged["engines"]["alpha"]["anchors"] == {"s_val": [[0, 0], [100, 100]]}
    assert set(merged["engines"]["alpha"]["anchors"].keys()) <= set(ALPHA_ANCHOR_KEYS)

    # beta.defaults：盘上改 win、塞未知键 → 已知键逐键覆盖、未知丢弃
    disk2 = export_registry_v2()
    disk2["engines"]["beta"]["defaults"] = {"win": 30, "no_such_key": 1}
    merged2 = merge_registry(disk2, d)
    assert merged2["engines"]["beta"]["defaults"]["win"] == 30
    assert "no_such_key" not in merged2["engines"]["beta"]["defaults"]
    assert merged2["engines"]["beta"]["defaults"]["band"] == d["engines"]["beta"]["defaults"]["band"]

    # macro.regime：同逐键
    disk3 = export_registry_v2()
    disk3["engines"]["macro"]["regime"] = {"peg_strong": 7.70, "ghost": 1}
    merged3 = merge_registry(disk3, d)
    assert merged3["engines"]["macro"]["regime"]["peg_strong"] == 7.70
    assert "ghost" not in merged3["engines"]["macro"]["regime"]


# ── beta.signals：id 为键同规则（改动优先 + orphan + defaults 序）─

def test_signals_merge():
    d = export_registry_v2()
    disk = export_registry_v2()

    # 用户改动优先：改第一个信号的 status
    first_id = disk["engines"]["beta"]["signals"][0]["id"]
    disk["engines"]["beta"]["signals"][0]["status"] = "inactive"
    # 盘上独有信号 → orphan 保留
    disk["engines"]["beta"]["signals"].append(
        {"id": "legacy_signal_x", "status": "active", "detail": "x"})
    disk["version"] = REGISTRY_VERSION

    merged = merge_registry(disk, d)
    sigs = merged["engines"]["beta"]["signals"]
    by_id = {s["id"]: s for s in sigs}

    assert by_id[first_id]["status"] == "inactive"            # 用户改动优先
    assert by_id["legacy_signal_x"]["orphan"] is True         # 盘上独有 → orphan
    assert [s["id"] for s in sigs][:len(d["engines"]["beta"]["signals"])] == \
        [s["id"] for s in d["engines"]["beta"]["signals"]]    # 规范序以 defaults 为准
    assert len(sigs) == len(d["engines"]["beta"]["signals"]) + 1


# ── 红线：任意 merge 结果必过 validate_registry ─────────────────

@pytest.mark.parametrize("mutate", [
    lambda r: None,                                             # 纯 defaults
    lambda r: r["engines"]["alpha"]["metrics"].append(          # orphan 指标
        {**_mk_alpha_metric("val_pe_ttm"), "id": "x_orphan", "orphan": True}),
    lambda r: r["engines"]["beta"]["signals"].append(           # orphan 信号
        {"id": "sig_orphan", "status": "active", "detail": "d"}),
    lambda r: r.update(orphans=[{"id": "v1_left", "status": "active"}]),  # v1 停车场
    lambda r: r["engines"]["macro"].update(dim_weights={"D1": 1.0}),      # macro 子集权重
])
def test_merged_passes_validate(mutate):
    disk = export_registry_v2()
    mutate(disk)
    disk["version"] = REGISTRY_VERSION
    merged = merge_registry(disk, export_registry_v2())
    assert validate_registry(merged) == []


# ── 端到端：datasource 写入段语义（用户改动在同步后完整保留）─────

def test_end_to_end_datasource_write_protects_user_edits(tmp_path):
    """模拟 fetch_all registry 写入段：盘上用户改动 → merge → 写盘 → 重读仍保留。

    场景：用户把 m2_yoy 置 inactive、alpha 红利低波权重改 0.4、调 rev_20d 参数；
    盘上文件缺新指标 val_fcf_pct（旧版导出）+ 含一条盘上遗留 legacy_k。同步后：
    改动全保留、新指标补齐、legacy_k 标 orphan。
    """
    repo = tmp_path / "repo"
    (repo / "factors").mkdir(parents=True)

    # 1) 用户盘上改动（基于当前 defaults 剪裁/篡改）
    disk = export_registry_v2()
    disk["engines"]["alpha"]["metrics"] = [
        m for m in disk["engines"]["alpha"]["metrics"] if m["id"] != "val_pe_ttm"]  # 模拟旧版缺新指标
    disk["engines"]["alpha"]["metrics"].append(
        {**_mk_alpha_metric("val_pb"), "id": "legacy_k", "status": "inactive"})      # 盘上遗留
    next(m for m in disk["engines"]["macro"]["metrics"] if m["id"] == "m2_yoy")["status"] = "inactive"
    disk["engines"]["alpha"]["dim_weights"] = {
        **disk["engines"]["alpha"]["dim_weights"], "红利低波": 0.4}
    next(m for m in disk["engines"]["alpha"]["metrics"]
         if m["id"] == "rev_20d")["params"] = {"n": 10}
    disk["version"] = REGISTRY_VERSION
    (repo / "factors" / "registry.json").write_text(
        json.dumps(disk, ensure_ascii=False, indent=2), encoding="utf-8")

    # 2) fetch_all 写入段同款调用（datasource.py L1510-1513）
    merged = merge_registry(load_registry_raw(repo), export_registry_v2())
    (repo / "factors" / "registry.json").write_text(
        json.dumps(merged, ensure_ascii=False, indent=2), encoding="utf-8")

    # 3) 走正式加载（含校验）断言
    reg = load_registry(repo)                                 # 校验失败会回退默认 → 断言必失败
    assert validate_registry(merged) == []

    m2 = next(m for m in reg["engines"]["macro"]["metrics"] if m["id"] == "m2_yoy")
    assert m2["status"] == "inactive"                         # 启停保留
    assert reg["engines"]["alpha"]["dim_weights"]["红利低波"] == 0.4   # 权重保留
    rev = next(m for m in reg["engines"]["alpha"]["metrics"] if m["id"] == "rev_20d")
    assert rev["params"] == {"n": 10}                         # 参数保留
    ids = {m["id"] for m in reg["engines"]["alpha"]["metrics"]}
    assert "val_pe_ttm" in ids                                # 新默认条目补齐
    legacy = next(m for m in reg["engines"]["alpha"]["metrics"] if m["id"] == "legacy_k")
    assert legacy["orphan"] is True                           # 遗留条目标 orphan 不删

    # 4) 运行时面：legacy_k 不可见、val_pe_ttm 可见
    eng = BaseEngine.__new__(BaseEngine)
    eng.id, eng.name, eng.kind, eng.registry = "alpha", "引擎A", "cross_section", reg
    live = {m.id for m in eng.metrics()}
    assert "legacy_k" not in live and "val_pe_ttm" in live


def test_fetch_rmtree_preserves_user_registry(tmp_path):
    """回归（T-R12 总验收实测发现）：fetch_all 各模式都 rmtree(repo) 整体重建，
    注册表若在 rmtree 后才读取，用户改动必丢。修复语义：rmtree 前先 load_registry_raw
    快照、尾部以快照参与 merge_registry——按 fetch_all 的真实顺序重放该序列钉死。
    """
    repo = tmp_path / "repo"
    (repo / "factors").mkdir(parents=True)
    disk = export_registry_v2()
    next(m for m in disk["engines"]["macro"]["metrics"] if m["id"] == "m2_yoy")["status"] = "inactive"
    disk["version"] = REGISTRY_VERSION
    (repo / "factors" / "registry.json").write_text(
        json.dumps(disk, ensure_ascii=False, indent=2), encoding="utf-8")

    # ── fetch_all 真实顺序（datasource.py：函数头部快照 → rmtree → 尾部 merge）──
    pre_registry = load_registry_raw(repo)                # rmtree 前取走（修复点）
    shutil.rmtree(repo)                                   # 整体重建（注册表一并消失）
    (repo / "factors").mkdir(parents=True)                # 重建目录（fetch 过程不写注册表）
    merged = merge_registry(pre_registry, export_registry_v2())
    (repo / "factors" / "registry.json").write_text(
        json.dumps(merged, ensure_ascii=False, indent=2), encoding="utf-8")

    reg = load_registry(repo)
    m2 = next(m for m in reg["engines"]["macro"]["metrics"] if m["id"] == "m2_yoy")
    assert m2["status"] == "inactive"                     # 用户改动跨 rmtree 保留
    assert validate_registry(reg) == []


def test_load_registry_raw_passthrough(tmp_path):
    """load_registry_raw：合法 v2 原样带回；v1 原样带回；坏 JSON/缺失 → None。"""
    d = export_registry_v2()
    p = tmp_path / "factors" / "registry.json"
    p.parent.mkdir(parents=True)

    p.write_text(json.dumps(d, ensure_ascii=False), encoding="utf-8")
    assert load_registry_raw(tmp_path) == d

    p.write_text(json.dumps({"factors": [{"id": "x"}]}), encoding="utf-8")
    assert load_registry_raw(tmp_path) == {"factors": [{"id": "x"}]}

    p.write_text("{oops", encoding="utf-8")
    assert load_registry_raw(tmp_path) is None

    assert load_registry_raw(tmp_path / "nowhere") is None
