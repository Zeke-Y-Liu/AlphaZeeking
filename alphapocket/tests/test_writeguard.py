"""registry 写保护三语义测试（T-R4）：覆盖→merge / 用户优先 / orphan 标记。"""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from applib.writeguard import (GUARDED_RELS, merge_bars, merge_portfolio,
                               merge_registry, restore_guard, snapshot_guard)

MACHINE_BASE = {"factors": [
    {"id": "mom_12_1", "name": "12-1月动量", "type": "alpha", "status": "active",
     "version": "v1.0", "universe": ["CN", "HK", "US"]},
    {"id": "val_pe_ttm", "name": "市盈率TTM", "type": "alpha", "status": "active",
     "version": "v1.0", "universe": ["CN", "HK", "US"]},
    {"id": "vol_20d", "name": "20日年化波动", "type": "signal", "status": "active",
     "version": "v1.0", "universe": ["CN", "HK", "US"], "horizon_days": 20}]}


# ── merge_registry：用户字段保留 / 机器字段新胜 / 新增 / orphan 三分支 ──────
def test_merge_registry_user_fields_win_and_orphan():
    old = {"factors": [
        {**MACHINE_BASE["factors"][0], "status": "paused", "owner": "用户甲"},   # 改动+自定义字段
        MACHINE_BASE["factors"][1],                                             # 无冲突
        {"id": "gone_factor", "name": "已下线", "type": "alpha", "status": "active"}]}
    new = {"factors": [
        {**MACHINE_BASE["factors"][0], "version": "v1.1"},                      # 机器 bump version
        {**MACHINE_BASE["factors"][1], "status": "active"},
        MACHINE_BASE["factors"][2]]}                                            # gone_factor 消失
    out = merge_registry(old, new)
    by = {f["id"]: f for f in out["factors"]}
    # 用户改动胜出（红线 2）：status 冲突保 old；机器字段新胜：version=v1.1
    assert by["mom_12_1"]["status"] == "paused" and by["mom_12_1"]["version"] == "v1.1"
    assert by["mom_12_1"]["owner"] == "用户甲"                    # 用户自定义字段保留
    assert by["val_pe_ttm"]["status"] == "active"                 # 无冲突 → 机器值
    # orphan 标记不物理删（红线 3）：条目仍在 + 可查询
    assert by["gone_factor"]["orphan"] is True and by["gone_factor"]["orphaned_at"]
    assert by["gone_factor"]["name"] == "已下线"
    assert out["_merge"]["orphaned"] == ["gone_factor"]
    assert "mom_12_1" in out["_merge"]["user_fields_kept"]


def test_merge_registry_no_conflict_equals_new():
    out = merge_registry(MACHINE_BASE, MACHINE_BASE)
    assert out["factors"] == MACHINE_BASE["factors"] and out["_merge"]["orphaned"] == []


# ── merge_portfolio：顶层标量 + positions ─────────────────────────────────
def test_merge_portfolio():
    old = {"id": "cn4", "name": "cn4 · 用户改名", "note": "用户手记",
           "positions": [{"secid": "600015.SH", "weight": 30.0},          # 改权重
                         {"secid": "002014.SZ", "weight": 25.0}]}
    new = {"id": "cn4", "name": "cn4 · 赛轮/永新/福耀/分众", "note": "机器注记",
           "positions": [{"secid": "600015.SH", "weight": 25.0},
                         {"secid": "601601.SH", "weight": 25.0}]}
    out = merge_portfolio(old, new)
    assert out["name"] == "cn4 · 用户改名" and out["note"] == "用户手记"   # 顶层用户改动胜出
    by = {p["secid"]: p for p in out["positions"]}
    assert by["600015.SH"]["weight"] == 30.0                             # 权重用户改动胜出
    assert by["601601.SH"]["weight"] == 25.0                             # 新增条目
    assert by["002014.SZ"]["orphan"] is True                             # 消失 → orphan
    assert out["_merge"]["orphaned"] == ["002014.SZ"]


# ── merge_bars：union 不丢历史 / 同 dt 新覆盖旧 / pct 接链 / 旧 secid 保留 ──
def _bar(d, c, pct=None):
    return {"dt": d, "close": c, "pct": pct}


def test_merge_bars_union_keeps_history():
    old = {"600519.SH": [_bar("2024-01-02", 10.0), _bar("2024-01-03", 10.5, 0.05)]}
    new = {"600519.SH": [_bar("2024-01-03", 10.5, 0.05), _bar("2024-01-04", 11.0)]}
    out, info = merge_bars(old, new)
    assert [r["dt"] for r in out["600519.SH"]] == ["2024-01-02", "2024-01-03", "2024-01-04"]
    assert out["600519.SH"][0] == _bar("2024-01-02", 10.0)               # 历史 dt 保留


def test_merge_bars_new_overrides_same_dt_and_resumes_pct():
    old = {"600519.SH": [_bar("2024-01-02", 10.0), _bar("2024-01-03", 10.5)]}
    new = {"600519.SH": [_bar("2024-01-03", 10.6), _bar("2024-01-04", 11.0, 0.037736)]}
    out, info = merge_bars(old, new)
    rows = out["600519.SH"]
    assert rows[1]["close"] == 10.6                                       # 同 dt 新覆盖旧（点位修正）
    assert rows[2]["close"] == 11.0
    assert info["resume"] == ["600519.SH"]                                # 首行 pct 接链重算
    # 断链修复可复核：增量窗首行 pct 基于 old 前日 close
    assert rows[2]["dt"] == "2024-01-04" and rows[2]["pct"] > 0


def test_merge_bars_keeps_secids_not_in_this_run():
    old = {"600519.SH": [_bar("2024-01-02", 10.0)], "0700.HK": [_bar("2024-01-02", 300.0)]}
    new = {"600519.SH": [_bar("2024-01-03", 10.2)]}                       # 本次只拉 A 股
    out, info = merge_bars(old, new)
    assert out["0700.HK"] == old["0700.HK"] and info["kept_secids"] == ["0700.HK"]


# ── 快照-恢复端到端：模拟 fetch_all rmtree 重建 → 用户字段/历史逐项保留 ────
def test_snapshot_restore_end_to_end(tmp_path):
    repo = tmp_path / "repo"
    (repo / "factors").mkdir(parents=True)
    (repo / "portfolio").mkdir()
    (repo / "market").mkdir()
    reg = {**MACHINE_BASE, "factors": [{**MACHINE_BASE["factors"][0], "status": "paused"},
                                       *MACHINE_BASE["factors"][1:]]}
    (repo / "factors/registry.json").write_text(json.dumps(reg, ensure_ascii=False), encoding="utf-8")
    bars = {"600519.SH": [_bar("2024-01-02", 10.0), _bar("2024-01-03", 10.5, 0.05)]}
    (repo / "market/daily_bars.json").write_text(json.dumps(bars), encoding="utf-8")

    snap = snapshot_guard(repo)
    assert set(snap) == {"factors/registry.json", "market/daily_bars.json"}

    # 模拟 fetch_all：rmtree 重建 + 机器覆盖写（增量窗只有 1 天）
    import shutil
    shutil.rmtree(repo)
    (repo / "factors").mkdir(parents=True)
    (repo / "market").mkdir()
    (repo / "factors/registry.json").write_text(json.dumps(MACHINE_BASE, ensure_ascii=False), encoding="utf-8")
    (repo / "market/daily_bars.json").write_text(json.dumps({"600519.SH": [_bar("2024-01-04", 11.0)]}), encoding="utf-8")

    summary = restore_guard(repo, snap)
    reg2 = json.loads((repo / "factors/registry.json").read_text(encoding="utf-8"))
    by = {f["id"]: f for f in reg2["factors"]}
    assert by["mom_12_1"]["status"] == "paused"                # 用户字段逐项保留（DoD 1）
    bars2 = json.loads((repo / "market/daily_bars.json").read_text(encoding="utf-8"))
    assert [r["dt"] for r in bars2["600519.SH"]] == ["2024-01-02", "2024-01-03", "2024-01-04"]
    assert len(bars2["600519.SH"]) == 3                        # 增量同步不丢历史（DoD 3）
    assert summary["factors/registry.json"]["user_fields_kept"] and \
           summary["factors/registry.json"]["orphaned"] == []
    assert summary["market/daily_bars.json"]["dt_total"] == 3


def test_restore_guard_skips_missing_and_broken(tmp_path):
    repo = tmp_path / "repo"
    (repo / "factors").mkdir(parents=True)
    (repo / "factors/registry.json").write_text("not-json{", encoding="utf-8")   # 损坏=缺失
    snap = {"factors/registry.json": {"factors": [{"id": "x", "status": "paused"}]}}
    summary = restore_guard(repo, snap)                        # 机器产出缺/坏 → 旧内容不回写、不炸
    assert summary == {}
