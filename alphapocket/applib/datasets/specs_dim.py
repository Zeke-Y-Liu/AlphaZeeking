"""维度域数据集声明（T-R9）：市场/行业/市值等截面维度。"""
from __future__ import annotations

from .base import DatasetSpec, SourceRef

SPECS: list[DatasetSpec] = [
    DatasetSpec(
        name="market_cap", path="dim/market_cap.json", category="dim",
        merge_policy="fill_missing", always=False, duckdb_table=None,
        verifications=[],
        sources=[SourceRef("baidu", "primary",
                           "ak.stock_zh_valuation_baidu(indicator='总市值', period='全部')",
                           "百度估值接口仅覆盖 A 股股票；基金/港股/美股无此口径（透明告警跳过）",
                           "百度股市通")],
        note="P0.3 敞口报告规模维度（增量仅补缺失 secid，已有标的全量重写不降级）"),
    DatasetSpec(
        name="security", path="dim/security.json", category="dim",
        merge_policy="replace", always=True, duckdb_table="dim_security",
        verifications=[],
        sources=[SourceRef("builtin_universe", "primary",
                           "代码内置 UNIVERSE（A股/港股/美股）+ 拉取时名称/行业/币种补全")],
        note="证券主档（secid → 名称/市场/资产类型/行业；data.py 入库 dim_security）"),
    DatasetSpec(
        name="industry_sw", path="dim/industry_sw.json", category="dim",
        merge_policy="replace", always=False, duckdb_table=None,
        verifications=[],
        sources=[SourceRef("sw", "primary",
                           "swsresearch SwClass2021 XLS（摘取 a-stock-data v3.10.1 (2026-10-07) §6.7）",
                           "PIT：取 as_of 前最后一次调整，无前视；申万官方仅发布行业代码",
                           "申万宏源研究 swsresearch.com")],
        note="申万行业变迁史（`ap altdata-sync` 手动触发，消除行业前视偏差）"),
]
