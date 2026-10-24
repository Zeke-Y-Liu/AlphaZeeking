"""factorlib · 中长期因子库（M1：注册中心 + 计算引擎 + 物化 + PIT 审计）

与 applib（α 内核）的关系：
- 单向依赖为零：factorlib 不 import applib，applib 不 import factorlib；
- 数据契约对齐：输入读取 repo JSON（market/daily_bars.json / factors/fundamental_values.json /
  dim/security.json / market/trade_calendar.json）与 α 同源；
- 表命名空间：物化输出一律写 fl_* 前缀表（fl_factor_value_daily / fl_factor_pit_meta 等），
  与 α 的 factor_value_daily（applib.data.data_sync 每次 DROP 重建）隔离，
  待 M0 数据底座统一表结构后合并（届时回写《数据层与因子库架构设计》§18）。

契约依据：《数据层与因子库架构设计》§14–§19；《中长期因子挖掘与回测-实施计划》M1。
"""
from factorlib.registry import (
    FactorMeta,
    load_factor,
    load_registry,
    resolve_backtest_start,
    set_status,
)

__all__ = ["FactorMeta", "load_factor", "load_registry", "resolve_backtest_start", "set_status"]
__version__ = "0.1.0"
