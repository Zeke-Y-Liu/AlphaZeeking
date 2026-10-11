"""引擎C T-C1 数据层测试（全密封，不触网）：carry_old 合并 / 增量防丢 / 透明降级 / provenance

- 落盘与溯源：14 指标声明、11 实测命中 + HK 3 件套 H-0006 缺失恒降级
- carry_old（红线4）：拉取失败/空窗口 → 旧数据零丢失，黑名单登记
- union 合并：旧点全保留、同 dt 新值覆盖、缺值行跳过
- 纪律标记：不决策（payload 无 signals 字段）、merge_policy=carry_old
运行：``cd alphapocket && python -m pytest tests/test_macro_liquidity.py -q``
"""
from __future__ import annotations

import json

import pandas as pd

from applib import datasource as DS
from applib.ctx import Ctx


# ── 密封夹具：各接口迷你帧（列名与 2026-10-11 实测契约一致）──────────────

def _frames() -> dict:
    return {
        "macro_china_money_supply": pd.DataFrame(
            [["2024年12月份", 7.3], ["2024年11月份", 7.1]],
            columns=["月份", "货币和准货币(M2)-同比增长"]),
        "macro_china_shrzgm": pd.DataFrame(
            [["202412", 30000.0]], columns=["月份", "社会融资规模增量"]),
        "macro_china_shibor_all": pd.DataFrame(
            [["2024-12-30", 1.52], ["2024-12-31", 1.50], ["2024-12-29", None]],
            columns=["日期", "1W-定价"]),
        "macro_china_lpr": pd.DataFrame(
            [["2024-12-20", 3.1]], columns=["TRADE_DATE", "LPR1Y"]),
        "repo_rate_hist": pd.DataFrame(
            [["2024-12-31", 1.48]], columns=["date", "FR007"]),
        "bond_zh_us_rate": pd.DataFrame(
            [["2024-12-30", 2.0, None, None],          # 美方缺值 → 利差跳过
             ["2024-12-31", 1.9, 4.2, -0.1]],
            columns=["日期", "中国国债收益率10年", "美国国债收益率10年", "美国国债收益率10年-2年"]),
        "macro_bank_usa_interest_rate": pd.DataFrame(
            [["美联储利率决议报告", "2024-12-18", 4.25, 4.25, 4.5],
             ["美联储利率决议报告", "2025-01-29", None, None, 4.25]],   # 未来会议行 → dropna 跳过
            columns=["商品", "日期", "今值", "预测值", "前值"]),
        "macro_usa_cpi_yoy": pd.DataFrame(
            [["2024-11-01", "2024-12-11", 2.7]], columns=["时间", "发布日期", "现值"]),
        "macro_usa_non_farm": pd.DataFrame(
            [["美国非农就业人数", "2024-12-06", 22.7]], columns=["商品", "日期", "今值"]),
        "macro_usa_gdp_monthly": pd.DataFrame(
            [["美国国内生产总值(GDP)", "2024-12-19", 3.1]], columns=["商品", "日期", "今值"]),
    }


def _fake_iface(frames, fail=(), empty=()):
    """替换 _macro_call_iface：按接口名返回迷你帧；fail 注入异常；empty 注入空窗口"""
    def fake(ak, spec, start, end, cache):
        name = spec["interface"]
        if name in fail:
            raise RuntimeError(f"注入失败：{name}")
        if name in empty:
            return pd.DataFrame()
        return frames[name]
    return fake


def _ctx(tmp_path) -> Ctx:
    return Ctx(root=tmp_path)


def _payload(tmp_path) -> dict:
    return json.loads((tmp_path / "remote/repo/market/macro_liquidity.json").read_text())


# ── 落盘与溯源（总验收 #1 基座）──────────────────────────────────────────

def test_fetch_persists_with_provenance(tmp_path, monkeypatch):
    monkeypatch.setattr(DS, "_macro_call_iface", _fake_iface(_frames()))
    res = DS.fetch_macro_liquidity(_ctx(tmp_path), start="2024-01-01", end="2024-12-31")

    assert res["fetched"] is True and res["merge_policy"] == "carry_old"
    assert res["n_ok"] == 11 and res["n_unavailable"] == 3      # HK 3 件套缺失降级

    payload = _payload(tmp_path)
    inds = payload["indicators"]
    assert len(inds) == 14 and all(v["institution"] for v in inds.values())
    # 日期契约：中文月份 / 紧凑月份 / ISO 三种格式实测解析
    assert inds["cn_m2_yoy"]["last_dt"] == "2024-12-01"
    assert inds["cn_srzg"]["last_dt"] == "2024-12-01"
    assert inds["lpr_1y"]["last_dt"] == "2024-12-20"
    # 利差：美方缺值行跳过，仅两列齐备处产点
    assert inds["cn_us_spread_10y"]["series"] == [{"dt": "2024-12-31", "value": -2.3}]
    # 同源共享接口：美债 10Y-2Y 从缓存帧取数
    assert inds["us_10y_2y"]["last_dt"] == "2024-12-31"
    # 未来会议行（今值 NaN）dropna 跳过
    assert inds["us_fed_rate"]["series"] == [{"dt": "2024-12-18", "value": 4.25}]
    # 缺值行跳过（shibor 2024-12-29 为 None）
    assert inds["shibor_1w"]["rows"] == 2
    # 黑名单：仅 HK 资产缺失
    assert {b["indicator"] for b in payload["blacklist"]} == {"hk_hibor_1m", "hk_hibor_3m", "hk_usdcnh"}
    assert all(b["kind"] == "asset_missing" for b in payload["blacklist"])


# ── carry_old（红线4：空窗口/拉取失败 → 旧数据完整保留）───────────────────

def test_carry_old_on_fetch_failure(tmp_path, monkeypatch):
    monkeypatch.setattr(DS, "_macro_call_iface", _fake_iface(_frames()))
    first = DS.fetch_macro_liquidity(_ctx(tmp_path))
    old_series = _payload(tmp_path)["indicators"]["cn_m2_yoy"]["series"]

    monkeypatch.setattr(DS, "_macro_call_iface", _fake_iface(_frames(), fail=("macro_china_money_supply",)))
    second = DS.fetch_macro_liquidity(_ctx(tmp_path))

    ind = _payload(tmp_path)["indicators"]["cn_m2_yoy"]
    assert ind["status"] == "carried_old" and ind["series"] == old_series   # 零丢失
    assert second["n_carried_old"] == 1 and second["n_ok"] == 10
    blk = [b for b in second["blacklist"] if b["indicator"] == "cn_m2_yoy"]
    assert blk and blk[0]["kind"] == "upstream_fail" and blk[0]["reason"]


def test_carry_old_on_empty_window(tmp_path, monkeypatch):
    monkeypatch.setattr(DS, "_macro_call_iface", _fake_iface(_frames()))
    DS.fetch_macro_liquidity(_ctx(tmp_path))
    old_series = _payload(tmp_path)["indicators"]["lpr_1y"]["series"]

    monkeypatch.setattr(DS, "_macro_call_iface", _fake_iface(_frames(), empty=("macro_china_lpr",)))
    res = DS.fetch_macro_liquidity(_ctx(tmp_path))

    ind = _payload(tmp_path)["indicators"]["lpr_1y"]
    assert ind["status"] == "carried_old" and ind["series"] == old_series   # 空窗口不覆盖旧值
    assert res["n_carried_old"] == 1


def test_incremental_union_never_drops(tmp_path, monkeypatch):
    """增量防丢：旧点全保留 + 同 dt 新值覆盖（点位修正）+ 新点追加"""
    monkeypatch.setattr(DS, "_macro_call_iface", _fake_iface(_frames()))
    DS.fetch_macro_liquidity(_ctx(tmp_path))
    # 构造旧历史：两期 LPR
    payload = _payload(tmp_path)
    payload["indicators"]["lpr_1y"]["series"] = [
        {"dt": "2024-10-21", "value": 3.1}, {"dt": "2024-11-20", "value": 3.1}]
    (tmp_path / "remote/repo/market/macro_liquidity.json").write_text(
        json.dumps(payload, ensure_ascii=False), encoding="utf-8")

    frames = _frames()
    frames["macro_china_lpr"] = pd.DataFrame(
        [["2024-11-20", 3.0], ["2024-12-20", 3.1]], columns=["TRADE_DATE", "LPR1Y"])
    monkeypatch.setattr(DS, "_macro_call_iface", _fake_iface(frames))
    DS.fetch_macro_liquidity(_ctx(tmp_path))

    series = _payload(tmp_path)["indicators"]["lpr_1y"]["series"]
    assert [r["dt"] for r in series] == ["2024-10-21", "2024-11-20", "2024-12-20"]  # 旧点未丢
    assert series[0]["value"] == 3.1        # 旧点原值保留
    assert series[1]["value"] == 3.0        # 同 dt 新值覆盖（修正）
    assert _payload(tmp_path)["indicators"]["lpr_1y"]["status"] == "ok"


# ── 透明降级（HK H-0006 / 全量失败不阻断）─────────────────────────────────

def test_hk_unavailable_then_file_present(tmp_path, monkeypatch):
    monkeypatch.setattr(DS, "_macro_call_iface", _fake_iface(_frames()))
    res = DS.fetch_macro_liquidity(_ctx(tmp_path))          # hk_rates.json 缺失 → 不抛异常
    assert res["indicators"]["hk_hibor_1m"]["status"] == "unavailable"

    hk = tmp_path / "remote/repo/market/hk_rates.json"
    hk.write_text(json.dumps({"hk_hibor_1m": [{"dt": "2024-12-31", "value": 4.8}]}), encoding="utf-8")
    res2 = DS.fetch_macro_liquidity(_ctx(tmp_path))
    assert res2["indicators"]["hk_hibor_1m"]["status"] == "ok"
    assert res2["indicators"]["hk_hibor_1m"]["last_dt"] == "2024-12-31"
    assert res2["indicators"]["hk_hibor_3m"]["status"] == "unavailable"     # 其余两件仍降级


def test_all_fail_full_degrade_no_exception(tmp_path, monkeypatch):
    every = ("macro_china_money_supply", "macro_china_shrzgm", "macro_china_shibor_all",
             "macro_china_lpr", "repo_rate_hist", "bond_zh_us_rate",
             "macro_bank_usa_interest_rate", "macro_usa_cpi_yoy",
             "macro_usa_non_farm", "macro_usa_gdp_monthly")
    monkeypatch.setattr(DS, "_macro_call_iface", _fake_iface(_frames(), fail=every))
    res = DS.fetch_macro_liquidity(_ctx(tmp_path))          # 全失败 → 文件照常落盘，不抛异常

    assert res["n_ok"] == 0 and res["n_unavailable"] == 14 and len(res["blacklist"]) == 14
    payload = _payload(tmp_path)
    assert payload["dataset"] == "macro_liquidity"
    assert all(v["series"] == [] for v in payload["indicators"].values())   # 无旧可保 → 空序列


def test_all_fail_with_old_carry(tmp_path, monkeypatch):
    monkeypatch.setattr(DS, "_macro_call_iface", _fake_iface(_frames()))
    DS.fetch_macro_liquidity(_ctx(tmp_path))
    old = _payload(tmp_path)

    every = ("macro_china_money_supply", "macro_china_shrzgm", "macro_china_shibor_all",
             "macro_china_lpr", "repo_rate_hist", "bond_zh_us_rate",
             "macro_bank_usa_interest_rate", "macro_usa_cpi_yoy",
             "macro_usa_non_farm", "macro_usa_gdp_monthly")
    monkeypatch.setattr(DS, "_macro_call_iface", _fake_iface(_frames(), fail=every))
    DS.fetch_macro_liquidity(_ctx(tmp_path))
    new = _payload(tmp_path)

    carried = [sid for sid, v in new["indicators"].items() if v["status"] == "carried_old"]
    assert sorted(carried) == sorted(sid for sid, v in old["indicators"].items() if v["series"])
    # 逐指标零丢失核对（增量防丢判定口径：同步前后逐项 diff 一致）
    for sid in carried:
        assert new["indicators"][sid]["series"] == old["indicators"][sid]["series"]


# ── 解析器与纪律标记 ─────────────────────────────────────────────────────

def test_month_parsers():
    assert DS._macro_month_cn("2008年02月份") == "2008-02-01"
    assert DS._macro_month_cn("2024年12月份") == "2024-12-01"
    assert DS._macro_month_cn("bad") is None
    assert DS._macro_month_compact("202603") == "2026-03-01"
    assert DS._macro_month_compact("202412") == "2024-12-01"
    assert DS._macro_month_compact("2024-12") is None      # 非紧凑格式拒绝


def test_dedup_last_wins(tmp_path, monkeypatch):
    frames = _frames()
    frames["macro_china_lpr"] = pd.DataFrame(
        [["2024-12-20", 3.0], ["2024-12-20", 3.1]],       # 同 dt 两行 → 后值覆盖
        columns=["TRADE_DATE", "LPR1Y"])
    monkeypatch.setattr(DS, "_macro_call_iface", _fake_iface(frames))
    DS.fetch_macro_liquidity(_ctx(tmp_path))
    assert _payload(tmp_path)["indicators"]["lpr_1y"]["series"] == [{"dt": "2024-12-20", "value": 3.1}]


def test_discipline_markers(tmp_path, monkeypatch):
    monkeypatch.setattr(DS, "_macro_call_iface", _fake_iface(_frames()))
    DS.fetch_macro_liquidity(_ctx(tmp_path))
    text = (tmp_path / "remote/repo/market/macro_liquidity.json").read_text()
    payload = json.loads(text)
    assert payload["merge_policy"] == "carry_old"
    assert payload["discipline"]["decision_free"] is True
    assert "signals" not in text and '"signal"' not in text   # 不决策 grep 红线（数据层无信号字段）
    assert all(payload["indicators"][sid]["institution"]
               for sid in ("cn_m2_yoy", "us_fed_rate", "hk_hibor_1m"))  # provenance 机构名


# ══ 引擎C T-C2 计算层（纯函数，全密封）═════════════════════════════════════

import datetime as _dt

from applib.factors import (MACRO_BAND_HI, MACRO_MIN_DAILY, MACRO_MIN_MONTHLY,
                            compute_macro_liquidity)


def _monthly(values, y0=2024, m0=1) -> list[dict]:
    dts, y, m = [], y0, m0
    for _ in values:
        dts.append(f"{y:04d}-{m:02d}-01")
        m += 1
        if m > 12:
            y, m = y + 1, 1
    return [{"dt": d, "value": v} for d, v in zip(dts, values)]


def _daily(values, base=_dt.date(2025, 6, 15) - _dt.timedelta(days=59)) -> list[dict]:
    """默认尾部对齐 AS_OF（2025-06-15）→ 日度最新点不 stale（path=ok）。"""
    return [{"dt": (base + _dt.timedelta(days=i)).isoformat(), "value": v}
            for i, v in enumerate(values)]


def _ind(sid, market, dim, series, *, value=None) -> dict:
    return {"market": market, "dim": dim, "name": sid, "unit": "%", "institution": "测试机构",
            "interface": "test", "status": "ok", "rows": len(series),
            "first_dt": series[0]["dt"] if series else None,
            "last_dt": series[-1]["dt"] if series else None,
            "series": value if value is not None else series}


AS_OF = "2025-06-15"          # 组合 K 线最后交易日（调用方传入，PIT 不取未来）


# ── scorer 表四路径：方向 / 阈值触发 / 阈值跳平 / 回退 ─────────────────────

def test_scorer_four_paths():
    from applib.factors import _macro_scorer
    meta = {"id": "x", "name": "x", "market": "A", "dim": "D3"}

    # 触发（极端分位）：12 个月度点，当前值最高 → p≈0.96 → |score|>=HI → 极端档
    up = _monthly(list(range(1, 13)))
    loose = _macro_scorer({**meta, "id": "cn_m2_yoy"}, up, "2025-01-10")     # dir=+1
    assert loose["path"] == "ok" and loose["level"] == 2 and loose["score"] >= MACRO_BAND_HI
    tight = _macro_scorer({**meta, "id": "shibor_1w"}, up, "2025-01-10")     # dir=-1 同分位反转
    assert tight["score"] <= -MACRO_BAND_HI and tight["level"] == -2
    assert loose["score"] == -tight["score"]                                 # 方向路径：镜像

    # 跳平（中位）：当前值在窗口中位（4.5：<4.5 有 4 个，==有 1 个 → p=4.5/9=0.5）→ 0 档
    flat = _macro_scorer({**meta, "id": "cn_m2_yoy"},
                         _monthly([1, 2, 3, 4, 5, 6, 7, 8, 4.5]), "2024-10-10")
    assert flat["pctile"] == 0.5 and flat["level"] == 0 and flat["score"] == 0.0

    # 回退（样本不足）：月度 5 点 < MACRO_MIN_MONTHLY → 0 档 insufficient
    few = _macro_scorer({**meta, "id": "cn_m2_yoy"}, _monthly([1, 2, 3, 4, 5]), "2024-06-10")
    assert few["path"] == "insufficient" and few["level"] == 0 and few["score"] is None

    # 无数据：HK 缺资产 → no_data
    nd = _macro_scorer({**meta, "id": "hk_hibor_1m"}, [], AS_OF)
    assert nd["path"] == "no_data" and nd["level"] is None


def test_scorer_double_period_ranking():
    """新+旧双期排位：先升后降序列，cur=65/prev=75 的窗口分位手工可复算。"""
    from applib.factors import _macro_scorer
    meta = {"id": "cn_m2_yoy", "name": "M2同比", "market": "A", "dim": "D2"}
    vals = [10, 20, 30, 40, 50, 60, 70, 80, 90, 100, 110, 120,
            115, 105, 95, 85, 75, 65, 55, 45, 35, 25, 15, 5]
    # as_of=2025-06-10 → 月度可用 2024-01..2025-05 = 前 17 点，cur=vals[16]=75，prev=85
    sc = _macro_scorer(meta, _monthly(vals), "2025-06-10")
    # cur 窗口=[60..75]（12 点）→ #(<75)=2 (60,70) → p=2.5/12
    assert sc["pctile"] == round(2.5 / 12, 4)
    # prev 窗口=[50..85]（12 点）→ #(<85)=4 (50,60,70,80) → p=4.5/12
    assert sc["prev_pctile"] == round(4.5 / 12, 4)
    assert sc["prev_pctile"] > sc["pctile"]       # 下降段：前期排位高于当前


def test_monthly_lookahead_guard():
    """月度前值站位防前视：as_of 当月的站位点不可用 → 用上月值；日度不取 as_of 之后。"""
    from applib.factors import _macro_scorer
    meta = {"id": "cn_m2_yoy", "name": "M2同比", "market": "A", "dim": "D2"}
    series = _monthly([1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12], y0=2024)   # 2024-01..2024-12
    sc = _macro_scorer(meta, series, "2025-02-15")
    assert sc["cur_dt"] == "2024-12-01"           # 站位到最近可用月（2025-01 点位不存在于序列）
    series_with_cur = series + [{"dt": "2025-02-01", "value": 99.0}]      # 当月点已落库（假想）
    sc2 = _macro_scorer(meta, series_with_cur, "2025-02-15")
    assert sc2["cur_dt"] == "2024-12-01" and sc2["cur_value"] == 12.0     # 当月点被 PIT 过滤

    dmeta = {"id": "shibor_1w", "name": "Shibor", "market": "A", "dim": "D3"}
    ds = _daily([1.0 + 0.01 * i for i in range(60)], base=_dt.date(2025, 1, 1))  # 至 2025-03-01
    dsc = _macro_scorer(dmeta, ds, "2025-02-15")
    assert dsc["cur_dt"] == "2025-02-15"          # 日度严格 dt<=as_of，不取未来


def test_stale_marks_carried_prev():
    """前值站位滞后标注：月度最新可用点早于上一月 → carried_prev + stale。"""
    from applib.factors import _macro_scorer
    meta = {"id": "cn_m2_yoy", "name": "M2同比", "market": "A", "dim": "D2"}
    series = _monthly([1, 2, 3, 4, 5, 6, 7, 8, 9], y0=2024)               # 至 2024-09-01
    sc = _macro_scorer(meta, series, "2025-01-10")
    assert sc["path"] == "carried_prev" and sc["stale"] is True
    assert sc["cur_dt"] == "2024-09-01"           # 站位到四个月前旧值


# ── 缺值归一化（缺维度不折损）与冻结契约 ──────────────────────────────────

def _full_indicators(hk_mode="none") -> dict:
    """构造 A(4 指标 2 维)/US(2 指标 2 维)/HK 场景的 indicators 字典（形状对齐 T-C1 产物）。"""
    a = {
        "cn_m2_yoy": _ind("cn_m2_yoy", "A", "D2", _monthly(list(range(1, 25)))),
        "cn_srzg": _ind("cn_srzg", "A", "D2", _monthly([100 + i for i in range(24)])),
        "shibor_1w": _ind("shibor_1w", "A", "D3", _daily([1.0 + 0.01 * i for i in range(120)])),
        "lpr_1y": _ind("lpr_1y", "A", "D3", _daily([3.0 + 0.01 * i for i in range(120)])),
    }
    us = {
        "us_fed_rate": _ind("us_fed_rate", "US", "D1", _daily([5.0 - 0.01 * i for i in range(120)])),
        "us_10y_2y": _ind("us_10y_2y", "US", "D3", _daily([0.5 + 0.01 * i for i in range(120)])),
    }
    if hk_mode == "partial":     # HK 仅 D3 一维可用（D4 缺）→ 缺维度不折损
        hk = {"hk_hibor_1m": _ind("hk_hibor_1m", "HK", "D3", _daily([4.0 + 0.01 * i for i in range(120)])),
              "hk_hibor_3m": _ind("hk_hibor_3m", "HK", "D3", []),
              "hk_usdcnh": _ind("hk_usdcnh", "HK", "D4", [])}
    elif hk_mode == "full":
        hk = {"hk_hibor_1m": _ind("hk_hibor_1m", "HK", "D3", _daily([4.0 + 0.01 * i for i in range(120)])),
              "hk_hibor_3m": _ind("hk_hibor_3m", "HK", "D3", _daily([4.5 + 0.01 * i for i in range(120)])),
              "hk_usdcnh": _ind("hk_usdcnh", "HK", "D4", _daily([7.8 - 0.001 * i for i in range(120)]))}
    else:
        hk = {"hk_hibor_1m": _ind("hk_hibor_1m", "HK", "D3", []),
              "hk_hibor_3m": _ind("hk_hibor_3m", "HK", "D3", []),
              "hk_usdcnh": _ind("hk_usdcnh", "HK", "D4", [])}
    return {**a, **us, **hk}


def test_missing_dimension_normalization():
    """HK 仅 D3 可用 → HK 档位由该维决定（缺维度不折损已知维度）并标注路径。"""
    out = compute_macro_liquidity(_full_indicators(hk_mode="partial"), AS_OF)
    assert out["markets"]["HK"] in (-2, -1, 0, 1, 2)          # 非 unavailable（部分维度即可判）
    d3 = [v for v in out["detail"]["indicators"].values()
          if v["market"] == "HK" and v["dim"] == "D3"]
    d4 = [v for v in out["detail"]["indicators"].values()
          if v["market"] == "HK" and v["dim"] == "D4"]
    # D3 维部分可用（1m ok、3m no_data）→ 维内均值只取可用项；D4 全缺
    assert any(v["path"] in ("ok", "carried_prev") for v in d3)
    assert all(v["path"] in ("ok", "carried_prev") for v in d3 if v["score"] is not None)
    assert all(v["path"] == "no_data" for v in d4)
    # HK 档位 == D3 可用项均值档（单可用项等价于直接映射）
    assert out["markets"]["HK"] == next(v["level"] for v in d3 if v["score"] is not None)


def test_frozen_contract_and_idempotent():
    out1 = compute_macro_liquidity(_full_indicators(hk_mode="full"), AS_OF)
    out2 = compute_macro_liquidity(_full_indicators(hk_mode="full"), AS_OF)
    assert out1 == out2                                        # 幂等重算一致（纯函数）

    for key in ("state", "global", "markets", "global_score", "as_of"):
        assert key in out1                                     # 冻结契约五键
    assert out1["as_of"] == AS_OF
    assert set(out1["markets"]) == {"A", "HK", "US"}
    assert out1["global"] in (-2, -1, 0, 1, 2)
    assert -1.0 <= out1["global_score"] <= 1.0
    assert out1["state"] in ("neutral", "偏松", "偏紧", "unavailable")
    for lvl in out1["markets"].values():                       # 输出位范围约束
        assert lvl in (-2, -1, 0, 1, 2, "unavailable")
    assert "signals" not in json.dumps(out1, ensure_ascii=False)   # 不决策 grep 红线
    assert out1["detail"]["bands"]["window_monthly"] == 12     # scorer 表参数可追溯


def test_state_machine_tokens():
    # 偏松：A/US 全部构造为「值越走越松」→ score>=HI
    out = compute_macro_liquidity(_full_indicators(hk_mode="full"), AS_OF)
    if out["global_score"] is not None and out["global_score"] >= MACRO_BAND_HI:
        assert out["state"] == "偏松"
    # 全缺数据 → unavailable
    empty = {sid: _ind(sid, m, d, []) for sid, m, d in
             [("cn_m2_yoy", "A", "D2"), ("us_fed_rate", "US", "D1"), ("hk_hibor_1m", "HK", "D3")]}
    out2 = compute_macro_liquidity(empty, AS_OF)
    assert out2["state"] == "unavailable" and out2["global"] == "unavailable"
    assert out2["global_score"] is None and out2["markets"] == {"A": "unavailable",
                                                                "HK": "unavailable",
                                                                "US": "unavailable"}


def test_all_insufficient_marks_markets_unavailable():
    """全部样本不足（回退路径）→ 指标 0 档 insufficient，市场 unavailable（不虚报中性）。"""
    few = {sid: _ind(sid, m, d, _monthly([1, 2, 3]) if m != "US" else _daily([1.0, 2.0]))
           for sid, m, d in [("cn_m2_yoy", "A", "D2"), ("us_fed_rate", "US", "D1"),
                             ("hk_hibor_1m", "HK", "D3")]}
    out = compute_macro_liquidity(few, AS_OF)
    assert all(v["path"] == "insufficient" and v["level"] == 0
               for v in out["detail"]["indicators"].values())
    assert set(out["markets"].values()) == {"unavailable"}
    assert out["state"] == "unavailable"


def test_min_windows_constants():
    assert MACRO_MIN_MONTHLY == 6 and MACRO_MIN_DAILY == 20 and MACRO_BAND_HI == 0.6


# ══ 引擎C T-C3 工具层（只读面：factor.macro + ap macro）════════════════════

from applib import cli as CLI
from applib import tools as TL


def _seed_macro_repo(tmp_path) -> Ctx:
    """在 tmp sandbox/repo 落一份 macro_liquidity.json（密封 fixture，HK 缺资产降级）。"""
    ctx = _ctx(tmp_path)
    d = ctx.local_repo / "market"
    d.mkdir(parents=True, exist_ok=True)
    payload = {"dataset": "macro_liquidity", "as_of": "2026-10-10", "merge_policy": "carry_old",
               "indicators": _full_indicators(hk_mode="none")}
    (d / "macro_liquidity.json").write_text(json.dumps(payload, ensure_ascii=False))
    return ctx


def test_factor_macro_tool_structure(tmp_path):
    ctx = _seed_macro_repo(tmp_path)
    res = TL.call_tool("factor.macro", {"as_of": "2025-06-15"}, ctx)
    assert res["status"] == "ok"
    assert set(res["markets"]) == {"A", "HK", "US"} and res["markets"]["HK"] == "unavailable"
    # dim/level 结构完整（总验收 #3 基座）：逐指标 market/dim/level/score
    for d in res["detail"]["indicators"].values():
        assert {"market", "dim", "level", "score"} <= set(d)
    # 口径/版本注记（scorer 版本、as_of、来源机构）
    n = res["notes"]
    assert "v1 (T-C2)" in n["scorer_version"] and n["as_of"] == "2025-06-15"
    assert n["institutions"] == ["测试机构"] and n["decision_free"] is True
    assert n["paths"]["no_data"] == 3          # HK 三指标透明标注
    assert "signals" not in json.dumps(res, ensure_ascii=False)   # 不决策 grep 红线（工具面）


def test_tool_cli_output_consistent(tmp_path, capsys, monkeypatch):
    """工具与 CLI 输出一致（同 as_of 冻结键逐字段相等）。"""
    ctx = _seed_macro_repo(tmp_path)
    monkeypatch.setattr(CLI, "Ctx", lambda: ctx)
    res = TL.call_tool("factor.macro", {"as_of": "2025-06-15"}, ctx)
    CLI.cmd_macro(type("A", (), {"as_of": "2025-06-15"})())
    out = capsys.readouterr().out
    head, _ = json.JSONDecoder().raw_decode(out)     # 第一个 JSON 块（冻结键头）
    assert head == {k: res.get(k) for k in
                    ("status", "state", "global", "markets", "global_score", "as_of")}


def test_macro_degrade_paths(tmp_path):
    """降级路径：缺文件 / indicators 空 / 全 no_data → unavailable，不抛异常。"""
    ctx = _ctx(tmp_path)
    res = TL.call_tool("factor.macro", {}, ctx)                      # 文件缺失
    assert res["status"] == "unavailable" and "reason" in res

    ctx2 = _ctx(tmp_path / "ws2")
    d = ctx2.local_repo / "market"
    d.mkdir(parents=True, exist_ok=True)
    (d / "macro_liquidity.json").write_text(json.dumps({"indicators": {}, "as_of": "2026-10-10"}))
    res2 = TL.call_tool("factor.macro", {}, ctx2)                    # indicators 空
    assert res2["status"] == "unavailable"

    ctx3 = _ctx(tmp_path / "ws3")
    d3 = ctx3.local_repo / "market"
    d3.mkdir(parents=True, exist_ok=True)
    empty = {sid: _ind(sid, m, "D3", []) for sid, m in
             [("cn_m2_yoy", "A"), ("us_fed_rate", "US"), ("hk_hibor_1m", "HK")]}
    (d3 / "macro_liquidity.json").write_text(
        json.dumps({"indicators": empty, "as_of": "2026-10-10"}))
    res3 = TL.call_tool("factor.macro", {}, ctx3)                    # 全 no_data
    assert res3["status"] == "unavailable" and res3["global"] == "unavailable"


def test_macro_tool_readonly(tmp_path):
    """只读红线：调用前后 sandbox 文件集合与内容不变。"""
    ctx = _seed_macro_repo(tmp_path)

    def snap():
        return {str(p.relative_to(ctx.sandbox_dir)): p.read_bytes()
                for p in sorted(ctx.sandbox_dir.rglob("*")) if p.is_file()}

    before = snap()
    TL.call_tool("factor.macro", {"as_of": "2025-06-15"}, ctx)
    assert snap() == before                                          # 无写入


def test_macro_default_as_of_from_payload(tmp_path):
    """缺省 as_of：取 payload 落盘 as_of（组合 K 线最后交易日，T-C1 写入）。"""
    ctx = _seed_macro_repo(tmp_path)
    res = TL.call_tool("factor.macro", {}, ctx)
    assert res["status"] == "ok" and res["as_of"] == "2026-10-10"
    assert res["notes"]["as_of_source"] == "payload（T-C1 落盘 as_of）"


def test_macro_as_of_fallback_from_indicators(tmp_path):
    """旧文件兜底：payload 无顶层 as_of → 各指标最新可用 dt（T-C1 旧产物兼容）。"""
    ctx = _seed_macro_repo(tmp_path)
    f = ctx.local_repo / "market" / "macro_liquidity.json"
    payload = json.loads(f.read_text())
    payload.pop("as_of")
    f.write_text(json.dumps(payload, ensure_ascii=False))
    res = TL.call_tool("factor.macro", {}, ctx)
    assert res["status"] == "ok"
    latest = max(v["last_dt"] for v in payload["indicators"].values() if v["last_dt"])
    assert res["as_of"] == latest
    assert "兜底" in res["notes"]["as_of_source"]
