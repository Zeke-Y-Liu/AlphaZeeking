"""数据同步主链单元测试：refresh 单标的的孤儿清理 universe 口径（M11 修复回归）。

背景缺陷（2026-10-23 E2E 发现）：``sync_workspace(mode=refresh)`` 曾把 fetch 的请求
子集 ``universe=[secid]`` 传给 ``_prune_orphans`` → sandbox repo 其余标的数据被整体
误删（美股标的随后在 DuckDB 物化 ``frep=[]`` 处炸于 88%；A 股标的则静默毁掉数据面，
靠后续全量增量「自愈」掩盖）。修复后 prune 一律按 **PG 权威全集** 清理——本文件
锁死该语义。全离线：不打 PG / Redis / 外网。
"""
from __future__ import annotations

import json
from pathlib import Path

import app.services.data_sync as ds


def _write_repo(ws: Path) -> Path:
    """伪造含 2 只有效标的 + 1 只孤儿（不在 PG）的 sandbox repo。"""
    repo = ws / "sandbox" / "repo"
    (repo / "market").mkdir(parents=True, exist_ok=True)
    (repo / "factors").mkdir(parents=True, exist_ok=True)
    (repo / "dim").mkdir(parents=True, exist_ok=True)
    def bar() -> list[dict]:
        return [{"dt": "2026-10-22", "open": 1, "high": 1, "low": 1, "close": 1,
                 "volume": 10, "amount": 10, "pct": 0.1}]

    (repo / "market" / "daily_bars.json").write_text(
        json.dumps({"A.SH": bar(), "B.SH": bar(), "OLD.SH": bar()}), encoding="utf-8")
    (repo / "market" / "adjust_ex_dates.json").write_text(
        json.dumps({"A.SH": ["2023-01-06"], "OLD.SH": ["2022-01-06"]}), encoding="utf-8")
    (repo / "factors" / "fundamental_values.json").write_text(
        json.dumps({"A.SH": {"reports": [{"period": "2026-06-30", "announce_date": "2026-08-30",
                                          "eps_ytd": 1.0, "bps": 2.0, "roe": 1.0, "roe_w": 1.0,
                                          "rev_g": 0.1, "np_g": 0.1, "margin": 0.1,
                                          "revenue": 1, "net_profit": 1, "total_assets": 1,
                                          "total_liab": 1, "ocf": 1, "capex": 1,
                                          "cash": 1, "interest_debt": 1}],
                                  "dividends": [{"ex_date": "2026-07-01", "announce": "2026-06-01", "dps": 0.1}]},
                     "OLD.SH": {"reports": [], "dividends": []}}), encoding="utf-8")
    (repo / "dim" / "security.json").write_text(
        json.dumps([{"secid": "A.SH", "name": "甲", "market": "A", "asset_type": "STOCK",
                     "industry": "汽车"},
                    {"secid": "OLD.SH", "name": "旧", "market": "A", "asset_type": "STOCK",
                     "industry": None}]), encoding="utf-8")
    return repo


def _mock_pipeline(monkeypatch, ws: Path) -> None:
    """打桩 fetch/clone/materialize/duckdb/熔断，只留 prune 真跑（repo 即断言面）。"""

    def _fake_fetch_all(ctx, *, start, universe, incremental_from, allow_rebuild):
        assert universe == ["B.SH"], f"refresh 的 fetch universe 应为单标的子集：{universe}"
        return {"per_secid": {"B.SH": {"bars": 5}}, "as_of_bars": "2026-10-22",
                              "universe_fetched": list(universe), "universe_missing": [],
                              "universe_failed": []}

    monkeypatch.setattr(ds, "_pg_universe", lambda: ["A.SH", "B.SH"])
    monkeypatch.setattr(ds.datasource, "fetch_all", _fake_fetch_all)
    monkeypatch.setattr(ds, "_clone_to_sandbox", lambda ctx: 0)
    monkeypatch.setattr(ds, "materialize_from_pg", lambda ctx: {"securities": 0, "portfolios": 0})
    monkeypatch.setattr(ds, "build_duckdb", lambda ctx: {"built": False, "reason": "test"})
    monkeypatch.setattr(ds, "record_upstream_failure", lambda: 0)
    monkeypatch.setattr(ds, "record_upstream_success", lambda: None)


def test_refresh_prunes_by_pg_universe_not_request_subset(tmp_path, monkeypatch) -> None:
    """refresh 单标的：repo 其余标的数据保留（按 PG 全集清理），孤儿仍被清。"""
    ws = tmp_path / "ws"
    repo = _write_repo(ws)
    _mock_pipeline(monkeypatch, ws)

    result = ds.sync_workspace(ws, mode="refresh", secid="B.SH")

    bars = json.loads((repo / "market" / "daily_bars.json").read_text(encoding="utf-8"))
    assert set(bars) == {"A.SH", "B.SH"}, (
        "refresh 单标的不得误删其余持仓/注册标的的数据面（曾按请求子集清理酿灾）"
    )
    assert "OLD.SH" not in bars, "PG universe 外的孤儿仍应被清理"

    fund = json.loads((repo / "factors" / "fundamental_values.json").read_text(encoding="utf-8"))
    assert "A.SH" in fund and "OLD.SH" not in fund

    adjust = json.loads((repo / "market" / "adjust_ex_dates.json").read_text(encoding="utf-8"))
    assert "A.SH" in adjust and "OLD.SH" not in adjust

    dims = json.loads((repo / "dim" / "security.json").read_text(encoding="utf-8"))
    assert {d["secid"] for d in dims} == {"A.SH"}

    assert result["mode"] == "refresh" and result["universe"] == ["B.SH"]


def test_incremental_prunes_by_pg_universe(tmp_path, monkeypatch) -> None:
    """incremental 全集语义不变：PG universe 外孤儿被清，全集保留。"""
    ws = tmp_path / "ws"
    repo = _write_repo(ws)
    _mock_pipeline(monkeypatch, ws)
    monkeypatch.setattr(
        ds, "_last_bar_dates",
        lambda ctx: {"A.SH": "2026-10-22", "B.SH": "2026-10-22"},
    )
    monkeypatch.setattr(
        ds.datasource, "fetch_all",
        lambda ctx, *, start, universe, incremental_from, allow_rebuild: {
            "per_secid": {}, "as_of_bars": "2026-10-22",
            "universe_fetched": list(universe), "universe_missing": [], "universe_failed": [],
        },
    )

    ds.sync_workspace(ws, mode="incremental")

    bars = json.loads((repo / "market" / "daily_bars.json").read_text(encoding="utf-8"))
    assert set(bars) == {"A.SH", "B.SH"}, "incremental 应仅清 PG 外孤儿"
