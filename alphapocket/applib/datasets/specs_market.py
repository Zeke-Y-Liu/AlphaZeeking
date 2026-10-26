"""行情域数据集声明（T-R9 注册 / T-R11 多源路由填实）。

merge_policy 从 fetch_all 增量段实际行为反推（对照 datasource ②'''' 增量合并段）：
- daily_bars / daily_bars_raw：_merge_bars 按 dt 并集 → bars_union；
- cross_check_tx：新非空用新、否则携带旧 → new_nonempty；
- adjust_ex_dates：去重并集 → set_union；
- macro_liquidity：失败/空指标携带旧值（月频窗口常为空，防丢历史）→ carry_old；
- 其余（fx / ggt / hk_rates / 日历）全量重写 → replace。

T-R11 多源路由：primary 的 interface/upstream 与 fetch_all `_reg` 的 provenance
登记口径**逐字一致**（_reg 经 spec_primary_interface/upstream 取用）；cross/fallback
登记主/备源关系与降级触发条件——**声明与文档化，运行时降级逻辑零改动**。
"""
from __future__ import annotations

from .base import DatasetSpec, SourceRef

SPECS: list[DatasetSpec] = [
    DatasetSpec(
        name="daily_bars", path="market/daily_bars.json", category="market",
        merge_policy="new_nonempty", always=True, duckdb_table="dwd_daily_bar",
        verifications=["golden_test", "cross_source", "calendar_check", "adjust_check"],
        sources=[SourceRef("sina", "primary",
                           "ak.stock_zh_a_daily/stock_hk_daily(qfq) + 腾讯usK线(raw口径) "
                           "+ ak.fund_etf_hist_sina", 
                           "A股/港股 qfq 前复权；美股走腾讯（新浪美股 qfq 有噪声弃用）；"
                           "ETF/LOF 无复权接口（raw）",
                           "新浪财经/腾讯证券"),
                 SourceRef("tx", "cross", "腾讯 web.ifzq.gtimg.cn K线（美股主序列）",
                           "美股无 qfq 接口，raw 口径混入 qfq 面（新浪美股 qfq 有噪声，弃用）"),
                 SourceRef("sina_us_raw", "fallback", "ak.stock_us_daily(adjust='')",
                           "美股腾讯主序列两轮探测（retry_empty）失败/为空 → 新浪 raw 升主源"
                           "（口径登记 methods.us_bars；amount 列缺失透明登记）")],
        note="前复权 K 线主面（因子/回测/验证输入）"),
    DatasetSpec(
        name="daily_bars_raw", path="market/daily_bars_raw.json", category="market",
        merge_policy="bars_union", always=True, duckdb_table="dwd_daily_bar_raw",
        verifications=["adjust_check"],
        sources=[SourceRef("sina", "primary",
                           "ak.stock_zh_a_daily/stock_hk_daily/stock_us_daily(adjust='')",
                           "美股 raw 兼作腾讯主序列的独立对账源",
                           "新浪财经")],
        note="不复权原始收盘（复权一致性校验基准）"),
    DatasetSpec(
        name="cross_check_tx", path="market/cross_check_tx.json", category="market",
        merge_policy="new_nonempty", always=True, duckdb_table="cross_check_tx",
        verifications=["cross_source", "calendar_check"],
        sources=[SourceRef("tx", "primary", "ak.stock_zh_a_hist_tx() + web.ifzq.gtimg.cn(hk/us)",
                           "交叉校验专用（120 日滚动窗口）", "腾讯证券")],
        note="独立源对账面（cross_source 验证的基准之一）"),
    DatasetSpec(
        name="adjust_ex_dates", path="market/adjust_ex_dates.json", category="market",
        merge_policy="set_union", always=True, duckdb_table=None,
        verifications=["adjust_check"],
        sources=[SourceRef("tx", "primary", "web.ifzq.gtimg.cn(hk/us, qfq 事件元数据)",
                           "唯一来源（降级语义：新浪无除权日接口，恒走腾讯）；解析 qfq 事件元数据 "
                           "FHcontent/ggContent 的 cqr；回购 HGcontent 不影响复权，不计入",
                           "腾讯证券")],
        note="港股/美股除权除息日（adjust_check 豁免名单）"),
    DatasetSpec(
        name="trade_calendar", path="market/trade_calendar.json", category="market",
        merge_policy="replace", always=True, duckdb_table="dwd_trade_calendar",
        verifications=["calendar_check"],
        sources=[SourceRef("sina", "primary", "ak.tool_trade_date_hist_sina()",
                           upstream="新浪财经")],
        note="A股交易日历"),
    DatasetSpec(
        name="trade_calendar_hk", path="market/trade_calendar_hk.json", category="market",
        merge_policy="replace", always=False, duckdb_table="dwd_trade_calendar_hk",
        verifications=[],
        sources=[SourceRef("kline_union", "primary", "K线日期并集推导（hk 市场）",
                           "无直接交易日历接口；由本市场 K 线并集推导", "新浪财经")],
        note="港股日历（universe 含港股时生成）"),
    DatasetSpec(
        name="trade_calendar_us", path="market/trade_calendar_us.json", category="market",
        merge_policy="replace", always=False, duckdb_table="dwd_trade_calendar_us",
        verifications=[],
        sources=[SourceRef("kline_union", "primary", "K线日期并集推导（us 市场）",
                           upstream="新浪财经")],
        note="美股日历（universe 含美股时生成）"),
    DatasetSpec(
        name="fx", path="market/fx.json", category="market",
        merge_policy="replace", always=True, duckdb_table=None,
        verifications=[],
        sources=[SourceRef("sina", "primary", "新浪 hq fx_shkdcny / fx_susdcny",
                           upstream="新浪财经")],
        note="港元/美元兑人民币即期（多市场组合折算人民币口径）"),
    DatasetSpec(
        name="ggt_components", path="market/ggt_components.json", category="market",
        merge_policy="replace", always=True, duckdb_table=None,
        verifications=[],
        sources=[SourceRef("xueqiu", "primary", "雪球港股通筛选 market=HK&type=ggt（降级 ggt_static 快照）",
                           "主源雪球在线；降级链见 ggt_static fallback 声明（provenance upstream "
                           "登记「雪球/静态快照」双口径）", "雪球/静态快照"),
                 SourceRef("ggt_static", "fallback", "ggt_static 静态快照",
                           "雪球在线拉取失败/为空 → 降级 ggt_static 静态快照"
                           "（static_snapshot@GGT_SNAPSHOT_DATE，快照基准日见 applib/ggt_static.py；"
                           "source 字段透明登记实际来源）")],
        note="港股通名单（dim/security 的 hk_connect 标记同源）"),
    DatasetSpec(
        name="hk_rates", path="market/hk_rates.json", category="market",
        merge_policy="replace", always=False, duckdb_table=None,
        verifications=[],
        sources=[SourceRef("hibor_fed_usdhkd", "primary",
                           "ak.rate_interbank(HIBOR1M) + ak.macro_bank_usa_interest_rate(Fed) + "
                           "ak.currency_boc_sina(USD/HKD 中行折算价交叉 USDHKD)",
                           upstream="新浪财经/中国银行"),
                 SourceRef("fed_prev", "fallback", "ak.macro_bank_usa_interest_rate(前值)",
                           "美联储目标利率今值缺失（未更新）→ 前值兜底（H-0006 同款；"
                           "meta.source 透明登记「前值兜底」）")],
        note="carry_hk_us / peg_pressure regime 数据（universe 含港股时拉取）"),
    DatasetSpec(
        name="macro_liquidity", path="market/macro_liquidity.json", category="market",
        merge_policy="carry_old", always=True, duckdb_table=None,
        verifications=[],
        sources=[SourceRef("akshare_macro", "primary",
                           "akshare 宏观接口族（见 meta 各指标 source）",
                           "per-metric 拉取口径 = datasource._MACRO_META 逐条声明（保留不搬）；"
                           "spec 层登记机构集合；逐指标独立容错（单源失败该 series 置 [] + meta.error，"
                           "增量合并 carry_old 防丢历史）",
                           "中国人民银行/全国银行间同业拆借中心/中债/美联储/BLS")],
        note="引擎C 市场级条件变量（与持仓 universe 无关，恒拉取）"),
    DatasetSpec(
        name="industry_index", path="industry/industry_index.json", category="market",
        merge_policy="fill_missing", always=False, duckdb_table=None,
        verifications=[],
        sources=[SourceRef("sw_index", "primary",
                           "ak.index_hist_sw(symbol=801xxx, period=day) + 行业归一 "
                           "(applib.industry.match_sw_l1：静态标注/巨潮 stock_profile_cninfo 所属行业)",
                           "2026-10-21 实测：东财 push2（stock_individual_info_em / "
                           "stock_board_industry_*）在沙箱出口 ProxyError 不可达 → 行业维度改走"
                           "申万一级指数；单行业失败跳过，增量 fill_missing 用旧行业兜底；"
                           "分析用数据面，不进六项对账",
                           "申万宏源/巨潮资讯")],
        note="行业现状（申万一级指数日线近250根，成交额单位亿元）：单公司分析「行业趋势」模块数据面"),
    DatasetSpec(
        name="stock_news", path="news/stock_news.json", category="market",
        merge_policy="fill_missing", always=False, duckdb_table=None,
        verifications=[],
        sources=[SourceRef("em_news", "primary", "ak.stock_news_em(symbol=code)",
                           "仅 A 股有公开接口（港股/美股不登记）；每标的近 30 条；"
                           "单标的失败跳过（fill_missing 用旧值兜底）；分析用数据面，不进六项对账",
                           "东方财富")],
        note="个股新闻：单公司分析「最新消息」模块数据面（港美无公开接口）"),
    DatasetSpec(
        name="index_bars", path="market/index_bars.json", category="market",
        merge_policy="fill_missing", always=True, duckdb_table=None,
        verifications=[],
        sources=[SourceRef("index_daily", "primary",
                           "ndx: ak.index_us_stock_sina(.IXIC)；csi300/chinext: "
                           "ak.stock_zh_index_daily(sh000300/sz399006)；"
                           "hstech: ak.stock_hk_index_daily_sina(HSTECH)",
                           "指数清单与 factors.INDEX_TREND_INDEXES 对拍（封闭集）；"
                           "单指数失败跳过（fill_missing 用旧值兜底）；2026-10-26 实测："
                           "港股指数须用代码 HSTECH（中文名报 KeyError）；"
                           "分析用数据面，不进六项对账",
                           "新浪财经")],
        note="引擎D 指数趋势数据面（第一批四指数日线，与持仓 universe 无关，恒拉取）"),
]
