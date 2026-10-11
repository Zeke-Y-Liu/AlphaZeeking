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
