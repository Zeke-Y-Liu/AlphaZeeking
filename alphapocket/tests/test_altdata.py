"""二线数据源（a-stock-data v3.10.1 摘取）测试 —— 全部离线，不触网。

覆盖：
- ``six`` 代码归一化（``601058.SH`` / ``SH601058`` / ``601058`` 互认）
- ``normalize_sw_table`` 申万原表规范化（整数代码 zfill、层级码补位、结构变更报错）
- ``sw_industry_as_of`` **PIT 无前视**（只用 as_of 及之前生效的分类）
- ``industries_as_of`` 批量映射 → 直接喂 ``factors.industry_neutralize``
运行：``cd alphapocket && python -m pytest tests/ -q``
"""
from __future__ import annotations

import pandas as pd
import pytest

from applib import altdata as A
from applib import factors as F

# 平安银行 000001 的真实行业变迁（申万官方表实测值）
SW_RECORDS = [
    {"code": "000001", "start_date": "1991-04-03", "industry_code": "440101",
     "l1_code": "440000", "l2_code": "440100"},
    {"code": "000001", "start_date": "2014-02-21", "industry_code": "480101",
     "l1_code": "480000", "l2_code": "480100"},
    {"code": "000001", "start_date": "2021-07-30", "industry_code": "480301",
     "l1_code": "480000", "l2_code": "480300"},
]


# ── 代码归一化 ──────────────────────────────────────────
def test_six_normalizes_all_forms():
    for form in ("601058", "601058.SH", "SH601058", "sh601058", "601058.XSHG"):
        assert A.six(form) == "601058"


def test_six_rejects_short_code():
    with pytest.raises(ValueError):
        A.six("1234")


# ── 申万原表规范化 ───────────────────────────────────────
def test_normalize_sw_table_zfills_int_code_and_levels():
    # 上游实测：股票代码是整数 1（不是 "000001"），行业代码也是整数
    df = pd.DataFrame({"股票代码": [1, 601058], "计入日期": ["1991-04-03", "2011-01-22"],
                       "行业代码": [480301, 220601], "更新日期": ["2024-09-27", "2024-09-27"]})
    recs = A.normalize_sw_table(df)
    assert recs[0]["code"] == "000001"                 # 整数 1 → "000001"，非 "1"
    assert recs[0]["industry_code"] == "480301"
    assert recs[0]["l1_code"] == "480000" and recs[0]["l2_code"] == "480300"
    assert recs[1]["code"] == "601058" and recs[1]["l1_code"] == "220000"
    assert [r["code"] for r in recs] == ["000001", "601058"]      # 按 code 排序


def test_normalize_sw_table_raises_on_schema_change():
    with pytest.raises(RuntimeError, match="表结构变化"):
        A.normalize_sw_table(pd.DataFrame({"股票代码": [1], "行业代码": [480301]}))


def test_normalize_sw_table_skips_invalid_date():
    df = pd.DataFrame({"股票代码": ["000001", "002014"], "计入日期": ["1991-04-03", "不是日期"],
                       "行业代码": [440101, 480301], "更新日期": ["", ""]})
    assert [r["code"] for r in A.normalize_sw_table(df)] == ["000001"]


# ── PIT：无前视（核心）──────────────────────────────────
def test_sw_industry_as_of_no_lookahead():
    # 2013-12-31 时 480101 尚未生效（2014-02-21 才生效）→ 必须仍取 440101
    r = A.sw_industry_as_of(SW_RECORDS, "000001", "2013-12-31", "l3")
    assert r["industry"] == "440101" and r["since"] == "1991-04-03"
    # 生效当日即可切换（start_date <= as_of，含当日）
    assert A.sw_industry_as_of(SW_RECORDS, "000001", "2014-02-21", "l3")["industry"] == "480101"
    assert A.sw_industry_as_of(SW_RECORDS, "000001", "2026-10-07", "l3")["industry"] == "480301"
    # 首次归属之前 → 无记录（不得回填未来分类）
    assert A.sw_industry_as_of(SW_RECORDS, "000001", "1990-01-01", "l3") is None


def test_sw_industry_as_of_levels():
    assert A.sw_industry_as_of(SW_RECORDS, "000001", "2016-01-01", "l1")["industry"] == "480000"
    assert A.sw_industry_as_of(SW_RECORDS, "000001", "2016-01-01", "l2")["industry"] == "480100"
    assert A.sw_industry_as_of(SW_RECORDS, "000001", "2016-01-01", "l3")["industry"] == "480101"
    assert A.sw_industry_as_of(SW_RECORDS, "601058.SH", "2026-10-07") is None   # 未收录


def test_sw_industry_as_of_accepts_secid_suffix():
    assert A.sw_industry_as_of(SW_RECORDS, "000001.SZ", "2016-01-01")["code"] == "000001"


# ── 批量映射 → 行业中性化（集成）─────────────────────────
def test_industries_as_of_feeds_neutralize():
    recs = SW_RECORDS + [
        {"code": "601058", "start_date": "2011-01-22", "industry_code": "220601",
         "l1_code": "220000", "l2_code": "220600"},
        {"code": "601058", "start_date": "2021-07-30", "industry_code": "280204",
         "l1_code": "280000", "l2_code": "280200"},
        {"code": "600660", "start_date": "1993-06-10", "industry_code": "280201",
         "l1_code": "280000", "l2_code": "280200"},
    ]
    secids = ["000001.SZ", "601058.SH", "600660.SH"]
    # PIT：2016 年时 601058 属化工 220000，与 600660 的 280000 不同组
    m2016 = A.industries_as_of(recs, secids, "2016-01-01")
    assert m2016["601058.SH"] == "220000" and m2016["600660.SH"] == "280000"
    # 2021-07-30 起 601058 转为汽车 280000 → 与 600660 同组
    same = A.industries_as_of(recs, secids, "2026-10-07")
    assert same["601058.SH"] == "280000" and same["600660.SH"] == "280000"

    # 直接消费：同行业去均值（2,4 → 均值 3 → −1/+1）；单只成组 → 减自身 → 0
    vals = {"601058.SH": 2.0, "600660.SH": 4.0, "000001.SZ": 9.0}
    z = F.industry_neutralize(vals, {k: (v or k) for k, v in same.items()})
    assert z["601058.SH"] == pytest.approx(-1.0)
    assert z["600660.SH"] == pytest.approx(1.0)
    assert z["000001.SZ"] == pytest.approx(0.0)


# ── 数值工具 ────────────────────────────────────────────
def test_numeric_helpers():
    assert A._f("1.975") == 1.975 and A._f(None) is None and A._f("") is None
    assert A._i("25") == 25 and A._i("25.0") == 25 and A._i(None) is None
