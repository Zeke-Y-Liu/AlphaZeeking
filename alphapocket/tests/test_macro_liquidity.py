"""宏观流动性数据层测试（T-C1：fetch_macro_liquidity，离线合成，不触网）

- 降级：伪 akshare 两接口抛异常 → 其余指标正常、失败指标 series=[] 且 meta 带 error
- 解析：中文月标签/紧凑月标签 → 月末 ISO；rrr 升序重排；tsf 12 月滚动和；
  cn_us_10y 同日相减；fed 今值缺失前值兜底；cpi 预期差 = 今值−预测值

运行：``cd alphapocket && python -m pytest tests/test_macro_liquidity.py -q``
"""
from __future__ import annotations

import json
import sys
import types

import pytest

from applib import datasource as DS


class _DF:
    """最小 DataFrame 替身：iterrows() + columns（datasource 只用到这两者）"""

    def __init__(self, rows):
        self._rows = rows
        self.columns = list(rows[0].keys()) if rows else []

    def iterrows(self):
        return list(enumerate(self._rows))


def _fake_akshare(raise_on=()):
    """伪 akshare：12 接口的最小返回；raise_on 中的接口抛异常。"""
    m = types.ModuleType("akshare")

    def _guard(name, df):
        if name in raise_on:
            raise RuntimeError(f"upstream {name} down")
        return df

    m.macro_china_money_supply = lambda: _guard("m2", _DF([
        {"月份": "2026年08月份", "货币和准货币(M2)-同比增长": 6.3},
        {"月份": "2026年09月份", "货币和准货币(M2)-同比增长": 6.8},
    ]))
    m.macro_china_shrzgm = lambda: _guard("tsf", _DF(
        [{"月份": f"2025{i:02d}", "社会融资规模增量": 1000.0} for i in range(1, 13)]
        + [{"月份": "202601", "社会融资规模增量": 2000.0}]))
    m.rate_interbank = lambda **kw: _guard("shibor", _DF([
        {"报告日": "2026-10-08", "利率": 1.85},
    ]))
    m.macro_china_lpr = lambda: _guard("lpr", _DF([
        {"TRADE_DATE": "2026-09-20", "LPR1Y": 3.0},
    ]))
    m.macro_china_reserve_requirement_ratio = lambda: _guard("rrr", _DF([
        {"生效时间": "2026年05月15日", "大型金融机构-调整后": 9.0},      # 新→旧（尾部最老）
        {"生效时间": "2025年01月01日", "大型金融机构-调整后": 9.5},
    ]))
    m.bond_zh_us_rate = lambda: _guard("bond", _DF([
        {"日期": "2026-10-08", "美国国债收益率10年": 4.1, "中国国债收益率10年": 1.8,
         "美国国债收益率10年-2年": 0.35},
        {"日期": "2026-10-09", "美国国债收益率10年": None, "中国国债收益率10年": 1.75,
         "美国国债收益率10年-2年": 0.40},
    ]))
    m.macro_bank_usa_interest_rate = lambda: _guard("fed", _DF([
        {"日期": "2026-10-08", "今值": 4.0, "前值": 4.25},
        {"日期": "2026-10-09", "今值": None, "前值": 4.0},              # 今值缺失 → 前值兜底
    ]))
    m.macro_usa_cpi_yoy = lambda: _guard("cpiyoy", _DF([
        {"时间": "2026-07-01", "发布日期": "2026-08-12", "现值": 3.5, "前值": 3.4},
        {"时间": "2026-08-01", "发布日期": "2026-09-11", "现值": None, "前值": 3.5},  # 未发布 → 过滤
    ]))
    m.macro_usa_cpi_monthly = lambda: _guard("cpi", _DF([
        {"商品": "美国CPI月率", "日期": "2026-09-30", "今值": 0.3, "预测值": 0.2},
    ]))
    m.macro_usa_non_farm = lambda: _guard("nonfarm", _DF([
        {"商品": "美国非农就业人数", "日期": "2026-10-03", "今值": 13.9, "预测值": 12.6},
    ]))
    m.macro_usa_gdp_monthly = lambda: _guard("gdp", _DF([
        {"商品": "美国国内生产总值(GDP)", "日期": "2026-09-30", "今值": 2.1, "预测值": None},
    ]))
    return m


@pytest.fixture
def patched_datasource(monkeypatch):
    monkeypatch.setattr(DS, "_retry", lambda fn, n=3, wait=2.0: fn())   # 去退避等待


def test_fetch_degrades_gracefully(monkeypatch, patched_datasource):
    """两接口抛异常 → 绝不抛出；失败指标空序列+meta error；其余按契约解析。"""
    monkeypatch.setitem(sys.modules, "akshare", _fake_akshare(raise_on=("m2", "nonfarm")))
    out = DS.fetch_macro_liquidity(start_date="2025-01-01")

    # 失败指标：series 为 [] 且 meta 带 error（函数未抛异常即通过前半）
    assert out["series"]["m2_yoy"] == [] and "error" in out["meta"]["m2_yoy"]
    assert out["series"]["us_nonfarm"] == [] and "error" in out["meta"]["us_nonfarm"]
    assert len(out["series"]) == 15 and len(out["meta"]) == 15

    # tsf：12 月滚动和（2025-12=12000；2026-01 窗口 2025-02..2026-01=11×1000+2000=13000）
    assert out["series"]["tsf_new_12m"] == [{"dt": "2025-12-31", "v": 12000.0},
                                            {"dt": "2026-01-31", "v": 13000.0}]
    # rrr：原表新→旧 → 升序重排；中文日期解析
    assert out["series"]["rrr_big"] == [{"dt": "2025-01-01", "v": 9.5},
                                        {"dt": "2026-05-15", "v": 9.0}]
    # cn_us_10y：同日相减（4.1−1.8）；第二行美债 NaN 跳过
    assert out["series"]["cn_us_10y"] == [{"dt": "2026-10-08", "v": 2.3}]
    # us_10y_2y：直取列，NaN 行跳过
    assert out["series"]["us_10y_2y"] == [{"dt": "2026-10-08", "v": 0.35},
                                          {"dt": "2026-10-09", "v": 0.40}]
    # fed：今值缺失 → 前值兜底
    assert out["series"]["fed_target"] == [{"dt": "2026-10-08", "v": 4.0},
                                           {"dt": "2026-10-09", "v": 4.0}]
    # cpi：同比独立接口（现值，未发布行过滤）；预期差 = 月率 今值−预测值
    assert out["series"]["us_cpi_yoy"] == [{"dt": "2026-07-01", "v": 3.5}]
    assert out["series"]["us_cpi_surprise"] == [{"dt": "2026-09-30", "v": 0.1}]
    assert out["series"]["us_gdp_qoq"] == [{"dt": "2026-09-30", "v": 2.1}]
    assert out["series"]["shibor_1m"] == [{"dt": "2026-10-08", "v": 1.85}]
    assert out["series"]["lpr_1y"] == [{"dt": "2026-09-20", "v": 3.0}]
    # as_of = 全序列最大 dt；顶层无 error（仍有 10 指标成功）
    assert out["as_of"] == "2026-10-09"
    assert out["error"] is None


def test_fetch_all_fail_degrades(monkeypatch, patched_datasource):
    """全部接口失败 → 顶层 error 登记且不抛异常。"""
    monkeypatch.setitem(sys.modules, "akshare", _fake_akshare(
        raise_on=("m2", "tsf", "shibor", "lpr", "rrr", "bond", "fed",
                  "cpiyoy", "cpi", "nonfarm", "gdp")))
    out = DS.fetch_macro_liquidity()
    assert all(not v for v in out["series"].values())
    assert out["as_of"] is None and out["error"] and "全部拉取失败" in out["error"]


def test_fetch_month_regex():
    """月标签解析：中文/紧凑 → 月末 ISO；非法输入 → None。"""
    assert DS._month_end_cn("2026年09月份") == "2026-09-30"
    assert DS._month_end_cn("202603") == "2026-03-31"
    assert DS._month_end_cn("2026年12月份") == "2026-12-31"      # 跨年月
    assert DS._month_end_cn("garbage") is None
    assert DS._cn_date("2007年02月25日") == "2007-02-25"
    assert DS._cn_date("2026年5月15日") == "2026-05-15"          # 非零填充
    assert DS._cn_date("n/a") is None


# ── 计算层：compute_macro_liquidity（T-C2，纯函数合成数据）──────
import datetime as _d

from applib import factors as F


def _mk(key, vals, dt_start="2026-01-01", freq="D"):
    """构造 {key: [{dt, v}]}：日/月频升序合成序列（月频取每月 1 日）。"""
    cur = _d.date.fromisoformat(dt_start)
    rows = []
    for v in vals:
        rows.append({"dt": cur.isoformat(), "v": v})
        if freq == "M":
            cur = _d.date(cur.year + (cur.month == 12), 1 if cur.month == 12 else cur.month + 1, 1)
        else:
            cur += _d.timedelta(days=1)
    return {key: rows}


def test_compute_easy_regime():
    """A 市场全宽松合成（M2 上行+RRR 降+Shibor 低位）→ level == 2。"""
    series = {}
    series.update(_mk("m2_yoy", [5.0, 5.5, 6.0, 6.5], "2026-06-01", "M"))            # +1（打满）
    series.update(_mk("tsf_new_12m", [200000.0, 210000.0, 220000.0, 230000.0], "2026-06-01", "M"))
    series.update(_mk("rrr_big", [10.5, 10.0, 9.5, 9.0], "2026-06-01", "M"))         # 降准 → +1
    series.update(_mk("shibor_1m", [2.0 - 0.025 * i for i in range(25)], "2026-09-01"))  # 低位 → +0.96
    series.update(_mk("lpr_1y", [3.5, 3.5, 3.0, 3.0], "2026-06-01", "M"))            # 降 → +1
    series.update(_mk("cn_us_10y", [3.0, 3.0, 3.0], "2026-09-01"))                   # +1
    r = F.compute_macro_liquidity(series)
    a = r["markets"]["A"]
    assert a["level"] == 2 and a["score"] > 0.6
    assert a["dims"]["D1"]["score"] == 1.0
    assert a["dims"]["D2"]["n_used"] == 2 and a["dims"]["D2"]["n_total"] == 2
    assert r["indicators"]["A"]["rrr_big"]["value"] == 9.0
    assert r["global"]["level"] == 2 and r["global"]["state"] in ("neutral", "easy_froth")


def test_compute_tight_stress():
    """D1 强紧（RRR 升+fed 升）且全维度负 → global state == tight_stress。"""
    series = {}
    series.update(_mk("rrr_big", [8.0, 8.5, 9.0, 9.5], "2026-06-01", "M"))           # 升准 → −1
    series.update(_mk("m2_yoy", [7.0, 6.5, 6.0, 5.5], "2026-06-01", "M"))            # −1
    series.update(_mk("tsf_new_12m", [260000.0, 250000.0, 240000.0, 230000.0], "2026-06-01", "M"))
    series.update(_mk("shibor_1m", [1.4 + 0.025 * i for i in range(25)], "2026-09-01"))  # 高位 → −0.96
    series.update(_mk("lpr_1y", [3.0, 3.0, 3.5, 3.5], "2026-06-01", "M"))            # 升 → −1
    series.update(_mk("cn_us_10y", [-3.0, -3.0, -3.0], "2026-09-01"))                # −1
    series.update(_mk("fed_target", [4.0, 4.25, 4.5, 5.0], "2026-06-01", "M"))       # 加息 → −1
    series.update(_mk("us_cpi_surprise", [0.3, 0.3], "2026-09-01"))                  # 超预期 → −1
    series.update(_mk("us_10y_2y", [0.2 + 0.1 * i for i in range(11)], "2026-09-01"))  # 倒挂加剧高位
    r = F.compute_macro_liquidity(series)
    assert r["markets"]["A"]["level"] == -2
    assert r["markets"]["US"]["dims"]["D1"]["score"] == -1.0
    assert r["markets"]["HK"] == {"status": "unavailable"}                           # 未给 hk_series
    assert r["global"]["level"] == -2 and r["global"]["state"] == "tight_stress"
    assert r["notes"] == ["D1 以政策利率+CPI 近似（个人版口径限制）", "US D2 维度缺席（免费源缺失）"]


def test_compute_dim_missing_drops_denominator():
    """shibor 空序列 → D3 n_used==1/n_total==2，维度分只用 lpr。"""
    series = {}
    series.update(_mk("rrr_big", [9.0, 9.0], "2026-06-01", "M"))                     # 未动 → 0
    series.update(_mk("m2_yoy", [6.0, 6.0, 6.0], "2026-06-01", "M"))                 # 0
    series.update(_mk("tsf_new_12m", [200000.0] * 3, "2026-06-01", "M"))             # 0
    series["shibor_1m"] = []                                                         # 空序列
    series.update(_mk("lpr_1y", [3.5, 3.5, 3.0], "2026-06-01", "M"))                 # 降 → +1
    series.update(_mk("cn_us_10y", [0.0], "2026-09-01"))                             # 0
    r = F.compute_macro_liquidity(series)
    d3 = r["markets"]["A"]["dims"]["D3"]
    assert d3["n_used"] == 1 and d3["n_total"] == 2 and d3["score"] == 1.0
    assert r["indicators"]["A"]["shibor_1m"]["missing"] is True
    assert "缺失指标：shibor_1m" in r["notes"]


def test_compute_hk_consistent_with_h0006():
    """HK 输入使 compute_hk_regime 触发 outflow_pressure → carry/peg 指标分均为负。"""
    hk = [{"dt": f"2026-09-{d:02d}", "hibor1m": 4.0, "fed": 3.0, "usdhkd": 7.86}
          for d in range(1, 31)]
    assert F.compute_hk_regime(hk)["state"] == "outflow_pressure"
    r = F.compute_macro_liquidity({}, hk_series=hk)
    hk_ind = r["indicators"]["HK"]
    assert hk_ind["carry_hk_us"]["score"] < 0 and hk_ind["peg_pressure"]["score"] < 0
    assert hk_ind["carry_hk_us"]["value"] == 1.0              # 4.0 − 3.0
    assert r["markets"]["HK"]["dims"]["D4"]["n_total"] == 2


def test_compute_all_empty_unavailable():
    """series 全空 + hk_series=None → 顶层 {"status": "unavailable"}。"""
    assert F.compute_macro_liquidity({}) == {"status": "unavailable"}
    assert F.compute_macro_liquidity({"m2_yoy": []}) == {"status": "unavailable"}


def test_compute_deterministic():
    """同输入两次调用输出完全相等（纯函数确定性）。"""
    series = {}
    series.update(_mk("m2_yoy", [5.0, 5.5, 6.0, 6.5], "2026-06-01", "M"))
    series.update(_mk("shibor_1m", [2.0 - 0.025 * i for i in range(25)], "2026-09-01"))
    series.update(_mk("lpr_1y", [3.5, 3.5, 3.0, 3.0], "2026-06-01", "M"))
    hk = [{"dt": f"2026-09-{d:02d}", "hibor1m": 4.0, "fed": 3.0, "usdhkd": 7.78}
          for d in range(1, 11)]
    assert (F.compute_macro_liquidity(series, hk_series=hk)
            == F.compute_macro_liquidity(series, hk_series=hk))


# ── 工具层：factor.macro + 集成层：portfolio_review ─────────────
from applib.ctx import Ctx
from applib.tools import call_tool


def _write_ws(root, macro_payload=None, hk_payload=None):
    """伪造 workspace：sandbox/repo/market/{macro_liquidity,hk_rates}.json。"""
    d = root / "sandbox" / "repo" / "market"
    d.mkdir(parents=True, exist_ok=True)
    if macro_payload is not None:
        (d / "macro_liquidity.json").write_text(
            json.dumps(macro_payload, ensure_ascii=False), encoding="utf-8")
    if hk_payload is not None:
        (d / "hk_rates.json").write_text(
            json.dumps(hk_payload, ensure_ascii=False), encoding="utf-8")
    return Ctx(root=str(root))


_MACRO_MIN = {k: [{"dt": "2026-09-01", "v": v} for v in vals] for k, vals in {
    "rrr_big": [9.5, 9.0], "m2_yoy": [6.0, 6.5], "tsf_new_12m": [200000.0, 210000.0],
    "shibor_1m": [1.8, 1.7], "lpr_1y": [3.5, 3.0], "cn_us_10y": [2.5],
    "us_10y_2y": [0.4], "fed_target": [4.5], "us_cpi_yoy": [3.4], "us_cpi_surprise": [-0.1],
}.items()}
_MACRO_META_MIN = {k: {"upstream": "测试机构", "freq": "月"} for k in _MACRO_MIN}


def test_tool_macro_ok(tmp_path):
    """workspace 有数据 → 返回 markets/global，无 error；meta 提供来源。"""
    ctx = _write_ws(tmp_path, {"series": _MACRO_MIN, "meta": _MACRO_META_MIN},
                    {"series": [{"dt": "2026-09-01", "hibor1m": 4.0, "fed": 3.0, "usdhkd": 7.78}]})
    res = call_tool("factor.macro", {}, ctx)
    assert "error" not in res
    assert set(res["markets"]) == {"A", "HK", "US"} and "global" in res
    assert res["indicators"]["A"]["rrr_big"]["source"] == "测试机构"


def test_tool_macro_missing_file(tmp_path):
    """无 macro_liquidity.json → error 不抛异常。"""
    res = call_tool("factor.macro", {}, Ctx(root=str(tmp_path)))
    assert res == {"error": "宏观流动性数据未同步（先执行数据同步）"}


def test_tool_macro_as_of_cutoff(tmp_path):
    """as_of 早于全部数据 → 全市场缺失，按 T-C2 语义顶层 unavailable。"""
    ctx = _write_ws(tmp_path, {"series": _MACRO_MIN, "meta": _MACRO_META_MIN})
    res = call_tool("factor.macro", {"as_of": "2020-01-01"}, ctx)
    assert "error" not in res
    assert res == {"status": "unavailable"}


# ── 集成层：portfolio_review 返回 macro_liquidity（T-C4）────────
import inspect

from applib import tools as T


def _stub_alpha(secid, arr, reports, dividends, *a, **kw):
    """compute_alpha 最小替身：跳过财务依赖，字段覆盖 portfolio_review 引用面。"""
    return {"as_of": arr[-1]["dt"], "used_period": None, "used_announce": None,
            "val_pe_ttm": None, "val_pb": None, "val_pe_pct": None, "val_pb_pct": None,
            "div_yield_ttm": None, "qua_roe_ttm": None, "qua_margin": None,
            "grow_rev_yoy": None, "grow_np_yoy": None, "mom_12_1": 0.1, "vol_250": 0.2,
            "pe_hist_n": None, "dims": {}, "grades": {}, "composite": 0.0,
            "composite_grade": "C"}


def _stub_beta(secid, arr, *a, **kw):
    """compute_beta 最小替身：不产生信号（引擎C 纪律断言的基础）。"""
    return {"as_of": arr[-1]["dt"], "close": 10.0, "mom_20d": 0.05, "vol_20d": 0.15,
            "ma20": 1.0, "ma20_break": 0.01, "state": "above", "high20_touch": False,
            "dist_high20": 0.02, "high20_intra": 0.03, "low20": 0.9, "sigma_daily": 0.01,
            "band_low": 0.98, "band_high": 1.02, "signals": []}


def _write_review_ws(tmp_path, macro=True):
    """伪造 portfolio_review 所需 workspace：组合/维度/行情/日历/因子注册表 + 宏观数据。"""
    repo = tmp_path / "sandbox" / "repo"
    bars = {}
    for secid in ("510300.SH", "600519.SH"):
        cur, rows = _d.date(2026, 8, 15), []
        for i in range(30):                    # 末根 2026-09-13 ≥ 宏观末点 2026-09-10
            rows.append({"dt": cur.isoformat(), "pct": 0.1 * (i % 5 - 2)})
            cur += _d.timedelta(days=1)
        bars[secid] = rows
    (repo / "market").mkdir(parents=True, exist_ok=True)
    (repo / "market" / "daily_bars.json").write_text(json.dumps(bars), encoding="utf-8")
    (repo / "market" / "trade_calendar.json").write_text(
        json.dumps(["2026-10-09", "2026-10-12"]), encoding="utf-8")
    (repo / "dim").mkdir(parents=True, exist_ok=True)
    (repo / "dim" / "security.json").write_text(json.dumps([
        {"secid": "510300.SH", "industry": "宽基ETF", "market": "A"},
        {"secid": "600519.SH", "industry": "白酒", "market": "A"}]), encoding="utf-8")
    (repo / "portfolio").mkdir(parents=True, exist_ok=True)
    (repo / "portfolio" / "cn4.json").write_text(json.dumps({
        "id": "cn4", "name": "测试组合", "base_ccy": "CNY", "note": "T-C4 测试",
        "positions": [{"secid": "510300.SH", "name": "沪深300ETF", "weight": 50.0},
                      {"secid": "600519.SH", "name": "贵州茅台", "weight": 50.0}]}),
        encoding="utf-8")
    (repo / "factors").mkdir(parents=True, exist_ok=True)
    (repo / "factors" / "fundamental_values.json").write_text("{}", encoding="utf-8")
    (repo / "factors" / "registry.json").write_text(json.dumps(
        {"factors": [{"id": "sig_ma20_break", "name": "MA20突破", "status": "active"}]}),
        encoding="utf-8")
    if macro:
        # 两点日频序列（末点 2026-09-10 > 行情截断 as_of 之前有效）
        rev = {k: [{"dt": d, "v": v} for d, v in zip(("2026-09-01", "2026-09-10"), vals)]
               for k, vals in {"rrr_big": [9.5, 9.0], "m2_yoy": [6.0, 6.5],
                               "tsf_new_12m": [200000.0, 210000.0], "shibor_1m": [1.8, 1.7],
                               "lpr_1y": [3.5, 3.0], "cn_us_10y": [2.5],
                               "us_10y_2y": [0.4], "fed_target": [4.5],
                               "us_cpi_yoy": [3.4], "us_cpi_surprise": [-0.1]}.items()}
        ctx = _write_ws(tmp_path, {"series": rev, "meta": _MACRO_META_MIN},
                        {"series": [{"dt": "2026-09-10", "hibor1m": 4.0,
                                     "fed": 3.0, "usdhkd": 7.78}]})
    else:
        ctx = Ctx(root=str(tmp_path))
    return ctx


def test_review_includes_macro(tmp_path, monkeypatch):
    """组合评估返回体含 macro_liquidity；市场权重=持仓市场计数（仅 A 持仓 → global=A 分）。"""
    monkeypatch.setattr(T.F, "compute_alpha", _stub_alpha)
    monkeypatch.setattr(T.F, "compute_beta", _stub_beta)
    ctx = _write_review_ws(tmp_path)
    r = T.portfolio_review({"portfolio": "cn4"}, ctx)
    ml = r["macro_liquidity"]
    assert ml.get("status") != "unavailable" and "error" not in ml
    assert set(ml["markets"]) == {"A", "HK", "US"} and "global" in ml
    assert ml["as_of"] == r["as_of"]                     # 与评估同日截断（非今天）
    # 市场权重生效：仅 A 持仓 → global 分 = A 市场分（HK/US 不进全局加权）
    assert ml["global"]["score"] == pytest.approx(ml["markets"]["A"]["score"])
    # 纪律红线：signals 无引擎C 条目；rebalance_plan 源码不引用 macro
    assert all("macro" not in json.dumps(s, ensure_ascii=False) for s in r["signals"])
    assert "macro" not in inspect.getsource(T.portfolio_rebalance_plan).lower()


def test_review_macro_degrades(tmp_path, monkeypatch):
    """无宏观数据 → macro_liquidity unavailable 降级，评估其余键不阻断。"""
    monkeypatch.setattr(T.F, "compute_alpha", _stub_alpha)
    monkeypatch.setattr(T.F, "compute_beta", _stub_beta)
    ctx = _write_review_ws(tmp_path, macro=False)
    r = T.portfolio_review({"portfolio": "cn4"}, ctx)
    ml = r["macro_liquidity"]
    assert ml["status"] == "unavailable" and "error" in ml
    # 评估主体不受影响
    assert len(r["positions"]) == 2 and "signals" in r
    assert isinstance(r["portfolio_metrics"]["nav"], float)
    assert r["as_of"] == "2026-09-13"


# ── 报告层：render_html / render_md 的 ②.5 章节（T-C5）──────────
from applib import report as RP


def _min_review(ml):
    """render_html/render_md 最小 review：1 个最小持仓（覆盖被访问键）+ macro_liquidity。"""
    alpha = {"val_pe_ttm": None, "val_pb": None, "val_pe_pct": None, "val_pb_pct": None,
             "div_yield_ttm": None, "qua_roe_ttm": None, "qua_margin": None,
             "grow_rev_yoy": None, "grow_np_yoy": None, "mom_12_1": 0.1, "vol_250": 0.2,
             "used_period": None}
    beta = {"state": "above", "mom_20d": 0.05, "vol_20d": 0.15, "ma20": 1.0,
            "ma20_break": 0.01, "high20_touch": False, "dist_high20": 0.02,
            "high20_intra": 0.03, "low20": 0.9, "sigma_daily": 0.01,
            "band_low": 0.98, "band_high": 1.02}
    return {"id": "cn4", "name": "测试组合", "as_of": "2026-09-13", "note": "T-C5 测试",
            "next_trade_date": "2026-09-14", "holiday_days": 0,
            "portfolio_metrics": {"day_ret": 0.001, "ret_20d": 0.02, "ret_ytd": 0.05,
                                  "ret_1y": 0.1, "max_dd_1y": -0.08, "nav": 1.05,
                                  "method": "等权"},
            "macro_liquidity": ml,
            "positions": [{"secid": "510300.SH", "name": "沪深300ETF", "industry": "宽基ETF",
                           "weight": 100.0, "asset_type": "ETF", "close": 10.0, "day_pct": 0.005,
                           "alpha": alpha, "beta": beta, "grades": {}, "composite": None,
                           "composite_grade": "C", "flags": ["测试"]}],
            "signals": [], "decay_caveats": []}


_PLAN_MIN = {"plan_id": "p0", "status": "draft", "diff_trades": [], "turnover_est": "0%",
             "cost_est_cny": 0, "cost_note": ""}

_ML_OK = {
    "as_of": "2026-09-13",
    "markets": {"A": {"score": 0.62, "level": 1, "dims": {}},
                "HK": {"status": "unavailable"},
                "US": {"score": 0.1, "level": 0, "dims": {}}},
    "global": {"score": 0.46, "level": 1, "state": "tight_stress"},
    "indicators": {"A": {"rrr_big": {"value": 9.0, "score": 1.0, "as_of": "2026-09-10",
                                     "missing": False, "source": "测试机构", "freq": "月"}},
                   "HK": {}, "US": {}},
    "notes": ["D1 以政策利率+CPI 近似（个人版口径限制）"],
}


def _prov_ctx(tmp_path):
    d = tmp_path / "sandbox" / "repo" / "market"
    d.mkdir(parents=True, exist_ok=True)
    (d / "provenance.json").write_text(json.dumps({
        "akshare_version": "test", "fetched_at": "2026-09-13",
        "pit_rules": {"note": "t"}, "sources": []}), encoding="utf-8")
    return Ctx(root=str(tmp_path))


def test_render_html_macro_section(tmp_path):
    """正常态：②.5 标题 + 温度计档位文案 + 琥珀徽章 + 指标表 + 注记。"""
    html = RP.render_html(_min_review(_ML_OK), _PLAN_MIN, _prov_ctx(tmp_path),
                          "run_smoke", "v0", "deadbeef", {})
    assert "②.5 宏观流动性" in html and "Engine C" in html
    assert "A市场" in html and "偏松" in html                 # 温度计行（HK unavailable 被跳过）
    assert "流动性紧缩提示" in html                            # tight_stress 琥珀徽章
    assert "→宽松" in html and "rrr_big" in html               # 指标明细表
    assert "不参与权重决策" in html                             # 固定口径注记
    assert "宏观流动性数据未同步" not in html


def test_render_html_macro_unavailable(tmp_path):
    """unavailable：单行灰条，不输出温度计/空表格/'None'。"""
    html = RP.render_html(_min_review({"status": "unavailable", "error": "x"}),
                          _PLAN_MIN, _prov_ctx(tmp_path), "run_smoke", "v0", "deadbeef", {})
    assert "不影响本报告其余结论" in html
    assert "偏松" not in html and "→宽松" not in html           # 无档位词/明细表
    assert ">None<" not in html


def test_render_md_macro_section():
    """MD 小节：'## 宏观流动性' 出现且含全局行。"""
    md = RP.render_md(_min_review(_ML_OK), _PLAN_MIN, "run_smoke", "v0", "deadbeef", {})
    assert "## 宏观流动性" in md and "| 全局 |" in md
    assert "不参与权重决策" in md
    md_bad = RP.render_md(_min_review({"status": "unavailable", "error": "x"}),
                          _PLAN_MIN, "run_smoke", "v0", "deadbeef", {})
    assert "## 宏观流动性" in md_bad and "不影响本报告其余结论" in md_bad
