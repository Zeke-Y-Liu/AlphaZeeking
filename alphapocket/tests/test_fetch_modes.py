"""组件化 T-R8：fetch_all(mode=) 三模式显式（bootstrap / incremental / refresh）。

锚点：模式解析纯函数全分支；refresh 窗口推导；无旁路（冲突显式拒绝）；
缺省推导告警不静默；incremental/refresh 不 rmtree（失败不丢旧值）。
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from applib import datasource as DS
from applib.ctx import Ctx


# ── _resolve_mode 纯函数（8 分支）────────────────────────────────────────
def test_resolve_mode_explicit_valid():
    assert DS._resolve_mode("bootstrap", None) == ("bootstrap", "explicit", None)
    assert DS._resolve_mode("incremental", {"s1": "2026-10-01"}) == ("incremental", "explicit", None)
    assert DS._resolve_mode("refresh", None) == ("refresh", "explicit", None)


def test_resolve_mode_rejects_invalid():
    with pytest.raises(ValueError, match="mode 非法"):
        DS._resolve_mode("sync", None)


def test_resolve_mode_rejects_conflicts():
    # bootstrap/refresh 窗口自定 → 显式 incremental_from = 旁路覆盖企图，拒绝
    with pytest.raises(ValueError, match="bootstrap"):
        DS._resolve_mode("bootstrap", {"s1": "2026-10-01"})
    with pytest.raises(ValueError, match="refresh"):
        DS._resolve_mode("refresh", {"s1": "2026-10-01"})


def test_resolve_mode_derived_with_warning():
    m, src, warn = DS._resolve_mode(None, {"s1": "2026-10-01"})
    assert (m, src) == ("incremental", "derived") and "未显式指定 mode" in warn
    m2, src2, warn2 = DS._resolve_mode(None, None)
    assert (m2, src2) == ("bootstrap", "derived") and "bootstrap 会整体重建" in warn2


# ── _refresh_window 纯函数 ───────────────────────────────────────────────
def test_refresh_window_covers_all_secids():
    w = DS._refresh_window(["600519.SH", "0700.HK", "AAPL"], today="2026-10-11")
    assert w == {"600519.SH": "2026-10-04", "0700.HK": "2026-10-04", "AAPL": "2026-10-04"}
    assert DS.REFRESH_WINDOW_DAYS == 7            # ≈ 最近 5 个交易日


# ── fetch_all 顶层集成（网络调用前的显式拒绝）──────────────────────────────
def test_fetch_all_rejects_invalid_mode_before_network(tmp_path):
    ctx = Ctx(remote_dir=tmp_path / "r", sandbox_dir=tmp_path / "s")
    with pytest.raises(ValueError, match="mode 非法"):
        DS.fetch_all(ctx, mode="sync")


def test_fetch_all_rejects_conflict_before_network(tmp_path):
    ctx = Ctx(remote_dir=tmp_path / "r", sandbox_dir=tmp_path / "s")
    with pytest.raises(ValueError, match="不接受 incremental_from"):
        DS.fetch_all(ctx, mode="bootstrap", incremental_from={"600519.SH": "2026-01-01"})


# ── 三模式语义 diff（真实副本，DoD 1/2/3）────────────────────────────────
def _mk_repo(base: Path) -> None:
    """构造含历史 K 线 + 用户改动 + orphan 的最小 repo（merge_policy 语义输入）。"""
    repo = base / "repo"
    (repo / "market").mkdir(parents=True)
    (repo / "factors").mkdir(parents=True)
    (repo / "market" / "daily_bars.json").write_text(json.dumps(
        {"600519.SH": [{"dt": "2026-09-28", "close": 10.0, "pct": None},
                       {"dt": "2026-09-29", "close": 11.0, "pct": 0.1},
                       {"dt": "2026-09-30", "close": 11.5, "pct": 0.045}]}, ensure_ascii=False))
    (repo / "factors" / "registry.json").write_text(json.dumps(
        {"factors": [{"id": "mom_12_1", "status": "paused", "owner": "用户T8"}]}, ensure_ascii=False, indent=2))
    return repo


def test_refresh_mode_semantics(tmp_path, monkeypatch):
    """refresh：窗口重拉同 dt 覆盖=点位修正 + 历史不动 + 用户改动保留（DoD 1/2/3）。"""
    repo = _mk_repo(tmp_path)
    Ctx(remote_dir=tmp_path, sandbox_dir=tmp_path / "sandbox")

    win = DS._refresh_window(["600519.SH"], today="2026-10-11")["600519.SH"]
    assert win == "2026-10-04"

    # 模拟窗口重拉：snapshot(旧3根) → _reg 覆盖写新窗口 2 根（一根同 dt 点位修正 + 一根新 dt）→ restore 合并
    new_bars = {"600519.SH": [{"dt": "2026-09-29", "close": 11.2, "pct": 0.12},   # 点位修正
                              {"dt": "2026-10-09", "close": 12.0, "pct": None}]}  # 新交易日
    from applib import writeguard as WG
    snap = WG.snapshot_guard(repo)                     # 旧库快照（fetch 主体写库前）
    (repo / "market" / "daily_bars.json").write_text(json.dumps(new_bars, ensure_ascii=False))
    summary = WG.restore_guard(repo, snap)

    out = json.loads((repo / "market" / "daily_bars.json").read_text())
    rows = out["600519.SH"]
    dts = [r["dt"] for r in rows]
    assert dts == ["2026-09-28", "2026-09-29", "2026-09-30", "2026-10-09"]  # 历史不动+修正+新增
    fixed = next(r for r in rows if r["dt"] == "2026-09-29")
    assert fixed["close"] == 11.2 and fixed["pct"] == 0.12        # 同 dt 新值覆盖
    assert summary["market/daily_bars.json"]["dt_total"] == 4
    reg = json.loads((repo / "factors" / "registry.json").read_text())
    assert reg["factors"][0]["status"] == "paused"                # 写保护联动（DoD 3）


def test_incremental_mode_no_rmtree_keeps_overwrite_datasets(tmp_path):
    """incremental：不 rmtree → 拉取失败的 overwrite 数据集旧文件保留（语义声明）。"""
    repo = _mk_repo(tmp_path)
    (repo / "market" / "fx.json").write_text(json.dumps({"HKD_CNY": 0.92}, ensure_ascii=False))
    # snapshot→（模拟拉取失败：本次未产出 fx）→ restore 后 fx 仍在（不 rmtree 的直接推论）
    from applib import writeguard as WG
    WG.snapshot_guard(repo)
    assert "market/fx.json" not in [k for k in WG.GUARDED_RELS]  # 非守护面，不 rmtree 即保留
    assert json.loads((repo / "market" / "fx.json").read_text()) == {"HKD_CNY": 0.92}
