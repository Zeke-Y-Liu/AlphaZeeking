"""engines 端点测试（指标 CRUD + 数据集联动；tmp workspace，无 PG / 无外网）。

覆盖：
- list/describe/datasets 查询面（注册表 v2 透传 + 数据集落盘状态汇总）；
- add：门禁拒绝（未知 engine / 缺 hypothesis_id / 未知 formula）422；
- add 成功 + dataset 缺失 → data_missing 提示（不阻断）；
- update：params 深合并 + version 自增；patch 改 dataset 附缺失提示；
- remove：软删（status=inactive）+ 复活；
- datasets：missing 计数与 hint。
"""
from __future__ import annotations

import json

ADD_BODY = {
    "engine": "macro",
    "spec": {
        "id": "test_m2_yoy",
        "formula": "builtin:macro_series",
        "dim": "D1",
        "market": "A",
        "dataset": "macro_liquidity",
        "scorer": {"type": "step", "arg": [8.0, 10.0]},
        "params": {},
        "status": "active",
        "note": "测试指标",
    },
    "hypothesis_id": "H-TEST-001",
    "reason": "引擎板块验收",
}


def _get(client, path: str):
    r = client.get(path)
    assert r.status_code == 200, r.text
    return r.json()


def test_list_and_describe(client):
    data = _get(client, "/api/v1/engines/metrics")
    assert data["changelog_n"] >= 0
    engines = {m["engine"] for m in data["metrics"]}
    assert engines <= {"alpha", "beta", "macro"}
    assert any(m["engine"] == "alpha" for m in data["metrics"])   # 内置默认清单非空

    desc = _get(client, "/api/v1/engines/describe")
    ids = {e["id"] for e in desc["engines"]}
    assert ids == {"alpha", "beta", "macro"}
    for e in desc["engines"]:
        assert e["metric_count"] > 0 and e["active_count"] > 0


def test_add_rejects_unknown_engine(client):
    body = {**ADD_BODY, "engine": "gamma"}
    r = client.post("/api/v1/engines/metrics", json=body)
    assert r.status_code == 422
    assert "allowed" in r.json()["detail"]


def test_add_requires_hypothesis_id(client):
    body = {k: v for k, v in ADD_BODY.items() if k != "hypothesis_id"}
    r = client.post("/api/v1/engines/metrics", json=body)
    assert r.status_code == 422


def test_add_rejects_unknown_formula(client):
    body = json.loads(json.dumps(ADD_BODY))
    body["spec"]["formula"] = "builtin:not_a_formula"
    r = client.post("/api/v1/engines/metrics", json=body)
    assert r.status_code == 422
    assert "formula" in r.text


def test_add_ok_with_data_missing_hint(client):
    """新增成功 + macro_liquidity.json 未落盘 → data_missing 提示（不阻断）。"""
    r = client.post("/api/v1/engines/metrics", json=ADD_BODY)
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["ok"] is True
    assert body["metric"]["id"] == "test_m2_yoy"
    # tmp workspace 无 macro_liquidity.json → 缺失提示
    assert body["data_missing"][0]["dataset"] == "macro_liquidity"
    assert not body["data_missing"][0]["synced"]
    assert "同步" in body["data_missing"][0]["hint"]


def test_update_deep_merge_and_version_bump(client):
    client.post("/api/v1/engines/metrics", json=ADD_BODY)
    r = client.patch(
        "/api/v1/engines/metrics/test_m2_yoy",
        json={"patch": {"params": {"n": 5}, "note": "改参数"}, "reason": "调参"},
    )
    assert r.status_code == 200, r.text
    after = r.json()["after"]
    assert after["version"] == 2                       # version 自增
    assert after["params"]["n"] == 5
    assert after["note"] == "改参数"
    # scorer 等未 patch 字段保持
    assert after["scorer"] == {"type": "step", "arg": [8.0, 10.0]}


def test_update_dataset_change_with_missing_hint(client):
    client.post("/api/v1/engines/metrics", json=ADD_BODY)
    r = client.patch(
        "/api/v1/engines/metrics/test_m2_yoy",
        json={"patch": {"dataset": "hk_rates"}},
    )
    assert r.status_code == 200
    assert r.json()["data_missing"][0]["dataset"] == "hk_rates"


def test_remove_soft_delete_and_revive(client):
    client.post("/api/v1/engines/metrics", json=ADD_BODY)
    r = client.request(
        "DELETE", "/api/v1/engines/metrics/test_m2_yoy",
        json={"hypothesis_id": "H-TEST-001", "reason": "下线"},
    )
    assert r.status_code == 200, r.text
    assert r.json()["metric"]["status"] == "inactive"

    # 复活（update status 翻转需 hypothesis_id）
    r = client.patch(
        "/api/v1/engines/metrics/test_m2_yoy",
        json={"patch": {"status": "active"}, "hypothesis_id": "H-TEST-001"},
    )
    assert r.status_code == 200
    assert r.json()["after"]["status"] == "active"


def test_update_unknown_metric_422(client):
    r = client.patch(
        "/api/v1/engines/metrics/no_such_metric",
        json={"patch": {"note": "x"}},
    )
    assert r.status_code == 422
    assert "不存在" in r.text


def test_datasets_endpoint(client):
    """新增指标后 datasets 汇总其引用 + missing 计数（数据板块卡片数据源）。"""
    client.post("/api/v1/engines/metrics", json=ADD_BODY)
    data = _get(client, "/api/v1/engines/datasets")
    names = [d["dataset"] for d in data["datasets"]]
    assert "macro_liquidity" in names
    ml = next(d for d in data["datasets"] if d["dataset"] == "macro_liquidity")
    assert "test_m2_yoy" in ml["metrics"]
    assert ml["synced"] is False                       # tmp workspace 未落盘
    assert data["missing"] >= 1
    assert "hint" in ml


def test_datasets_synced_when_file_exists(client, settings_override):
    """落盘 macro_liquidity.json 后 → synced=True 且 as_of/series 透出。"""
    from applib.ctx import Ctx

    ctx = Ctx(root=settings_override.workspace_path)
    ctx.ensure_sandbox()
    market = ctx.local_repo / "market"
    market.mkdir(parents=True, exist_ok=True)
    (market / "macro_liquidity.json").write_text(
        json.dumps({"series": {"m2": [1, 2], "lpr": [1]}, "as_of": "2026-10-09"}),
        encoding="utf-8",
    )
    data = _get(client, "/api/v1/engines/datasets")
    ml = next(d for d in data["datasets"] if d["dataset"] == "macro_liquidity")
    assert ml["synced"] is True
    assert ml["as_of"] == "2026-10-09"
    assert ml["series"] == 2
