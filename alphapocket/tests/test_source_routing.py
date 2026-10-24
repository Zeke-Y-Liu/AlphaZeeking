"""多源路由声明式 + provenance 口径收敛测试（T-R11）

覆盖（DoD）：
- 全 spec 的 interface 反射存在（ak.* 可调用对象，防声明漂移）；
- _reg 口径解析（_reg_spec_lookup）：已注册 → spec 主源 interface/upstream 与
  改造前手工字符串**逐字一致**（provenance sources[] 键值不变的对拍锚）；
- 未注册名 → WARNING + 手工参数照常返回；手工参数优先于 spec；
- 每个 fallback 源 note 非空且含触发条件关键词（ggt 引用 GGT_SNAPSHOT_DATE）；
- provenance sha256 计算与现实现一致（同文件同哈希）；
- 真实 ws provenance.json（若在）：sources[] 七键结构 + spec 查询逐条对拍。

运行：``cd alphapocket && python -m pytest tests/test_source_routing.py tests/test_datasource_merge.py -q``
"""
from __future__ import annotations

import hashlib
import json
import logging
import re
from pathlib import Path

import pytest

from applib import datasource as DS
from applib.datasets import (
    all_specs,
    get_spec_safe,
    spec_primary_interface,
    spec_primary_upstream,
)

# 改造前 fetch_all _reg 手工登记口径（T-R11 对拍锚：spec 主源声明必须与此逐字一致）
_LEGACY_REG = {
    ("dwd_trade_calendar", "market/trade_calendar.json"):
        ("ak.tool_trade_date_hist_sina()", "新浪财经"),
    ("dwd_daily_bar", "market/daily_bars.json"):
        ("ak.stock_zh_a_daily/stock_hk_daily(qfq) + 腾讯usK线(raw口径) + ak.fund_etf_hist_sina",
         "新浪财经/腾讯证券"),
    ("dwd_daily_bar_raw", "market/daily_bars_raw.json"):
        ("ak.stock_zh_a_daily/stock_hk_daily/stock_us_daily(adjust='')", "新浪财经"),
    ("cross_check_tx", "market/cross_check_tx.json"):
        ("ak.stock_zh_a_hist_tx() + web.ifzq.gtimg.cn(hk/us)", "腾讯证券"),
    ("adjust_ex_dates", "market/adjust_ex_dates.json"):
        ("web.ifzq.gtimg.cn(hk/us, qfq 事件元数据)", "腾讯证券"),
    ("dwd_financial_report", "factors/fundamental_values.json"):
        (("ak.stock_financial_analysis_indicator(A股) + stock_financial_hk_analysis_indicator_em(港股) + "
         "stock_financial_us_analysis_indicator_em(美股) + stock_financial_report_sina(A股三表：利润表/资产负债表/现金流量表)"),
         "新浪财经/东方财富"),
    ("dim_ggt_components", "market/ggt_components.json"):
        ("雪球港股通筛选 market=HK&type=ggt（降级 ggt_static 快照）", "雪球/静态快照"),
    ("dim_market_cap", "dim/market_cap.json"):
        ("ak.stock_zh_valuation_baidu(indicator='总市值', period='全部')", "百度股市通"),
    ("dwd_trade_calendar_hk", "market/trade_calendar_hk.json"):
        ("K线日期并集推导（hk 市场）", "新浪财经"),
    ("dwd_trade_calendar_us", "market/trade_calendar_us.json"):
        ("K线日期并集推导（us 市场）", "新浪财经"),
    ("macro_hk_rates", "market/hk_rates.json"):
        (("ak.rate_interbank(HIBOR1M) + ak.macro_bank_usa_interest_rate(Fed) + "
         "ak.currency_boc_sina(USD/HKD 中行折算价交叉 USDHKD)"), "新浪财经/中国银行"),
    ("macro_liquidity", "market/macro_liquidity.json"):
        ("akshare 宏观接口族（见 meta 各指标 source）",
         "中国人民银行/全国银行间同业拆借中心/中债/美联储/BLS"),
}


# ── 声明反射（防漂移）────────────────────────────────────────────

def test_spec_interface_reflection():
    """全部 spec 的 ak.* interface 令牌在 akshare 中真实存在（可调用）。"""
    import akshare as ak
    tokens = set()
    for s in all_specs():
        assert s.sources, f"{s.name}: sources 不能为空"
        assert any(r.role == "primary" for r in s.sources), f"{s.name}: 缺 primary 源"
        for r in s.sources:
            tokens |= set(re.findall(r"ak\.(\w+)", r.interface))
    assert tokens, "未发现任何 ak.* 接口令牌"
    for t in sorted(tokens):
        assert callable(getattr(ak, t, None)), f"akshare 缺少声明的接口：ak.{t}"


def test_fallback_notes_have_trigger():
    """每个 fallback 源 note 非空且含触发条件关键词；ggt 快照引用 GGT_SNAPSHOT_DATE。"""
    n_fallback = 0
    for s in all_specs():
        for r in s.sources:
            if r.role != "fallback":
                continue
            n_fallback += 1
            assert r.note.strip(), f"{s.name}/{r.id}: fallback note 不能为空"
            assert any(k in r.note for k in ("失败", "缺失", "降级", "兜底", "为空")), \
                f"{s.name}/{r.id}: fallback note 缺触发条件：{r.note}"
    assert n_fallback >= 3                                  # daily_bars/adjust_ex_dates/ggt/hk_rates
    ggt = next(r for r in get_spec_safe("ggt_components").sources if r.role == "fallback")
    assert "GGT_SNAPSHOT_DATE" in ggt.note


# ── _reg_spec_lookup：口径解析 ───────────────────────────────────

def test_reg_lookup_matches_legacy():
    """12 个登记点：spec 主源 interface/upstream 与改造前手工字符串逐字一致。"""
    for (name, rel), (iface, upstream) in _LEGACY_REG.items():
        spec = get_spec_safe(name, rel)
        assert spec is not None, f"{name}（{rel}）未注册"
        got = DS._reg_spec_lookup(name, rel, None, None)
        assert got == (iface, upstream), f"{name}: {got!r} != 改造前 {iface!r}/{upstream!r}"


def test_reg_lookup_unregistered_warns(caplog):
    with caplog.at_level(logging.WARNING, logger="applib.datasource"):
        assert DS._reg_spec_lookup("ghost_ds", "market/ghost.json", None, None) == (None, None)
        # 未注册但带手工参数 → 照常返回（透明降级，不抛）
        assert DS._reg_spec_lookup("ghost_ds", "market/ghost.json",
                                   "ak.manual()", "手工源") == ("ak.manual()", "手工源")
    assert any("ghost_ds" in rec.message and "未在 datasets/ 注册" in rec.message
               for rec in caplog.records)


def test_reg_lookup_manual_overrides_spec():
    """已注册数据集 + 手工参数 → 手工优先（新口径过渡兼容口）。"""
    assert DS._reg_spec_lookup("daily_bars", "market/daily_bars.json",
                               "ak.custom()", "自定义源") == ("ak.custom()", "自定义源")
    # 手工只给一端 → 另一端由 spec 补齐
    iface, upstream = DS._reg_spec_lookup("daily_bars", "market/daily_bars.json", "ak.custom()", None)
    assert iface == "ak.custom()"
    assert upstream == spec_primary_upstream(get_spec_safe("daily_bars"))


# ── provenance 现实现锚 ──────────────────────────────────────────

def test_sha256_file_unchanged(tmp_path: Path):
    """provenance sha256 口径与现实现一致（同文件同哈希）。"""
    f = tmp_path / "x.json"
    payload = json.dumps({"a": 1, "b": "中文"}, ensure_ascii=False).encode()
    f.write_bytes(payload)
    assert DS._sha256_file(f) == hashlib.sha256(payload).hexdigest()
    f.write_bytes(payload + b"\n")                          # 内容变 → 哈希变
    assert DS._sha256_file(f) != hashlib.sha256(payload).hexdigest()


def test_prov_sources_structure_real_ws():
    """真实 ws provenance.json（若在）：sources[] 七键结构（+ 可选 note）+ spec 查询逐条对拍。

    note 5.4 修订：M7 T-F1 603986 补段（上游数据洞补拉）在 provenance 登记 note 披露
    字段，属合规披露非键漂移——基线七键不变，note 为可选扩展键。
    """
    p = Path("/workspace/alphapocket/remote/repo/market/provenance.json")
    if not p.exists():
        pytest.skip("真实 ws provenance.json 不存在")
    prov = json.loads(p.read_text(encoding="utf-8"))
    base = {"dataset", "interface", "upstream", "params", "file", "sha256", "bytes"}
    for src in prov["sources"]:
        ks = set(src)
        assert base <= ks <= base | {"note"}, f"{src['dataset']}: sources[] 键集合漂移：{ks ^ base}"
        spec = get_spec_safe(src["dataset"], src["file"])
        if spec is not None:
            assert src["interface"] == spec_primary_interface(spec), \
                f"{src['dataset']}: interface 与 spec 声明不一致"
            assert src["upstream"] == spec_primary_upstream(spec), \
                f"{src['dataset']}: upstream 与 spec 声明不一致"


def test_source_ref_round_trip_with_upstream():
    from dataclasses import asdict

    from applib.datasets.base import SourceRef as SR
    r0 = SR("s", "primary", "ak.x()", "n", "机构")
    d = json.loads(json.dumps(asdict(r0), ensure_ascii=False))
    r = SR(**d)                                             # JSON → dict → 再构造相等
    assert (r.id, r.role, r.interface, r.note, r.upstream) == ("s", "primary", "ak.x()", "n", "机构")
