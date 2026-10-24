"""数据集注册表测试（T-R9：DatasetSpec 声明化 + _MERGEABLE 派生）

覆盖：全量 spec 与现状对拍（_MERGEABLE 7 键逐项相等）；枚举合法性
（merge_policy/category/verifications/sources.role）；get_spec 未知名 KeyError +
active_specs 过滤 inactive；虚构 demo_dataset(carry_old) 经 register_spec 自动进
_MERGEABLE（维度增标准路径）；factors_registry 特殊策略登记；序列化 round-trip；
duckdb_table 与 data.py 建表名对拍。

运行：cd alphapocket && python -m pytest tests/test_datasets_registry.py -q
"""
from __future__ import annotations

import importlib
import json

import pytest

from applib import datasets as DTS
from applib import datasource as DS
from applib.datasets import (
    CATEGORIES,
    MERGE_POLICIES,
    NON_SNAPSHOT_POLICIES,
    VERIFICATION_NAMES,
    DatasetSpec,
    SourceRef,
    active_specs,
    all_specs,
    get_spec,
)

# 改造前硬编码的快照合并面（锚点：派生结果必须逐项相等）
# 2026-10-21 扩展：industry_index / stock_news（单公司分析辅助数据集，fill_missing）
_LEGACY_MERGEABLE = {
    "daily_bars": "market/daily_bars.json",
    "daily_bars_raw": "market/daily_bars_raw.json",
    "cross_check_tx": "market/cross_check_tx.json",
    "adjust_ex_dates": "market/adjust_ex_dates.json",
    "fundamental_values": "factors/fundamental_values.json",
    "market_cap": "dim/market_cap.json",
    "macro_liquidity": "market/macro_liquidity.json",
    "industry_index": "industry/industry_index.json",
    "stock_news": "news/stock_news.json",
}

# data.py 的实际建表名（duckdb_table 登记面与建表对拍）
_DATA_PY_TABLES = {"dwd_daily_bar", "dwd_daily_bar_raw", "cross_check_tx",
                   "dwd_trade_calendar", "dwd_trade_calendar_hk", "dwd_trade_calendar_us",
                   "dwd_financial_report", "dwd_dividend", "dim_security"}


# ── 现状对拍（DoD①：行为零变化红线）──────────────────────────

def test_mergeable_derivation_matches_legacy():
    assert set(DS._MERGEABLE) == set(_LEGACY_MERGEABLE)                # 7 键一致
    for k, v in _LEGACY_MERGEABLE.items():
        assert DS._MERGEABLE[k] == v                                   # 每条路径一致
    assert DS._MERGEABLE == _LEGACY_MERGEABLE


def test_registry_merge_not_in_snapshot_plane():
    """factors_registry（registry_merge）与 replace 策略一样不入快照合并面。"""
    spec = get_spec("factors_registry")
    assert spec.merge_policy == "registry_merge"                       # 特殊策略登记
    assert spec.path == "factors/registry.json" and spec.category == "factors"
    assert spec.sources[1].id == "on_disk" and "merge_registry" in spec.sources[1].interface
    assert "factors_registry" not in DS._MERGEABLE                     # 写入口在 T-R6


def test_mergeable_paths_equal_spec_paths():
    for s in active_specs():
        if s.merge_policy not in NON_SNAPSHOT_POLICIES:
            assert DS._MERGEABLE[s.name] == s.path


# ── 枚举合法性（DoD②）────────────────────────────────────────

def test_enum_legality_for_all_specs():
    for s in all_specs():
        assert s.merge_policy in MERGE_POLICIES
        assert s.category in CATEGORIES
        assert all(v in VERIFICATION_NAMES for v in s.verifications)
        assert s.sources and all(src.role in ("primary", "cross", "reconcile", "fallback")
                                 for src in s.sources)
    # 非法值在构造期即拒绝（__post_init__ 校验）
    with pytest.raises(ValueError, match="merge_policy"):
        DatasetSpec(name="x", path="a.json", category="market", merge_policy="harness",
                    always=True, duckdb_table=None, verifications=[], sources=[])
    with pytest.raises(ValueError, match="category"):
        DatasetSpec(name="x", path="a.json", category="nope", merge_policy="replace",
                    always=True, duckdb_table=None, verifications=[], sources=[])
    with pytest.raises(ValueError, match="role"):
        DatasetSpec(name="x", path="a.json", category="market", merge_policy="replace",
                    always=True, duckdb_table=None, verifications=[],
                    sources=[SourceRef("s", "harness", "i")])


def test_specs_unique_and_coverage():
    names = [s.name for s in all_specs()]
    assert len(names) == len(set(names))                               # 无重名
    assert {"daily_bars", "macro_liquidity", "market_cap", "security", "industry_sw",
            "fundamental_values", "factors_registry"} <= set(names)    # 清单一个不少
    assert not any(s.category == "portfolio" for s in all_specs())     # 组合域不登记（红线）


def test_duckdb_tables_match_data_py():
    tables = {s.duckdb_table for s in all_specs() if s.duckdb_table}
    flat = set()
    for t in tables:
        flat |= set(t.split("+"))                                      # 多表用 + 连接
    assert flat <= _DATA_PY_TABLES                                     # 登记名 ⊆ 实际建表


# ── 查询面（DoD③）────────────────────────────────────────────

def test_get_spec_and_active_filter():
    # Q-fix（§14.2 qfq 漂移根治）：qfq 面改为全量自洽 + new_nonempty（拉取失败保旧）
    assert get_spec("daily_bars").merge_policy == "new_nonempty"
    with pytest.raises(KeyError, match="未知数据集"):
        get_spec("ghost_ds")

    demo_off = DatasetSpec(name="demo_off", path="market/demo_off.json", category="market",
                           merge_policy="carry_old", always=True, duckdb_table=None,
                           verifications=[], sources=[SourceRef("demo", "primary", "i")],
                           status="inactive")
    DTS.register_spec(demo_off)
    try:
        assert "demo_off" not in {s.name for s in active_specs()}      # inactive 被过滤
        assert get_spec("demo_off").status == "inactive"               # 但全量清单可查（报告登记面）
    finally:
        DTS._EXTRA.clear()

    with pytest.raises(ValueError, match="已登记"):
        DTS.register_spec(get_spec("daily_bars"))                      # 重名拒绝


# ── 维度增标准路径（DoD④：虚构 demo_dataset 自动进 _MERGEABLE）──

def test_demo_dataset_enters_mergeable():
    DTS._EXTRA.clear()
    DTS.register_spec(DatasetSpec(
        name="demo_dataset", path="market/demo_dataset.json", category="market",
        merge_policy="carry_old", always=True, duckdb_table=None,
        verifications=[], sources=[SourceRef("demo", "primary", "demo interface",
                                             "虚构数据集：验证声明即并入派生面")]))
    try:
        importlib.reload(DS)                                           # 重执行派生线
        assert DS._MERGEABLE["demo_dataset"] == "market/demo_dataset.json"
        assert set(DS._MERGEABLE) == set(_LEGACY_MERGEABLE) | {"demo_dataset"}
    finally:
        DTS._EXTRA.clear()
        importlib.reload(DS)                                           # 恢复模块原状
    assert DS._MERGEABLE == _LEGACY_MERGEABLE


# ── 序列化 round-trip（DoD⑥）─────────────────────────────────

def test_spec_json_round_trip():
    for s in all_specs():
        d = json.loads(json.dumps(s.to_dict(), ensure_ascii=False))
        assert DatasetSpec.from_dict(d) == s                           # asdict→json→再构造相等
