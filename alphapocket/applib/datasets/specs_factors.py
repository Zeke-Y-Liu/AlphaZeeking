"""因子域数据集声明（T-R9）：财务面 + 指标注册表。"""
from __future__ import annotations

from .base import DatasetSpec, SourceRef

SPECS: list[DatasetSpec] = [
    DatasetSpec(
        name="fundamental_values", path="factors/fundamental_values.json", category="factors",
        merge_policy="fill_missing", always=True,
        duckdb_table="dwd_financial_report+dwd_dividend",
        verifications=["adjust_check"],
        sources=[SourceRef("sina_em", "primary",
                           "ak.stock_financial_analysis_indicator(A股) + "
                           "stock_financial_hk_analysis_indicator_em(港股) + "
                           "stock_financial_us_analysis_indicator_em(美股) + "
                           "stock_financial_report_sina(A股三表：利润表/资产负债表/现金流量表)",
                           "A股逐报告期指标 + 港股东财（年度+报告期合并）+ 美股东财（单季 TTM/年报）"
                           "+ A股三表科目；分红明细见 datasource 财务段",
                           "新浪财经/东方财富")],
        note="财务报告期/分红（引擎A 估值/质量/成长/红利因子输入；PIT 按披露窗口推算）"),
    DatasetSpec(
        name="factors_registry", path="factors/registry.json", category="factors",
        merge_policy="registry_merge", always=True, duckdb_table=None,
        verifications=[],
        sources=[SourceRef("code_defaults", "primary",
                           "engines.base.export_registry_v2（内置 defaults）"),
                 SourceRef("on_disk", "reconcile", "engines.base.merge_registry（T-R6 写保护合并）",
                           "唯一写入口 apply_metric_change（T-R7）；用户改动优先，盘上独有条目标 orphan 不删")],
        note="指标注册表（配置非行情；登记为数据集以纳入同步/审计面，写入口在 T-R6 已实现）"),
]
