"""引擎D·指数级别趋势测试（2026-10-26 Goal：四指数位置/趋势判断）

分层覆盖（对齐 test_macro_liquidity.py 引擎C 模式）：
- 计算层 compute_index_trend：三态趋势 × 三档位置、位置百分位数学、窗口参数、
  as_of 截断（PIT）、active_ids 停用、三态降级（insufficient/unavailable/顶层）、
  meta 透传（含降级分支）、确定性；
- 数据层 fetch_index_bars：离线合成四接口（列名统一英文 date/close）、window 截断、
  start 过滤、单指数失败跳过、封闭集对拍；
- 引擎层 IndexTrendEngine：默认对拍（registry 缺省路径逐位一致）、registry 停用 +
  regime 覆盖 + overrides 优先；
- 工具层 factor.index_trend：正常 / 缺文件 / as_of 参数；
- 集成层 portfolio_review：index_trend 区块 + 与评估同日截断 + 纪律红线（不产生信号、
  不进 rebalance_plan）+ 降级不阻断；
- 报告层 render_html / render_md：②.6 章节（正常 + unavailable 降级）。

运行：cd alphapocket && python -m pytest tests/test_factor_index_trend.py -q
"""
from __future__ import annotations

import datetime as _d
import inspect
import json
import types

import pytest

from applib import datasource as DS
from applib import factors as F


def _mk_bars(n, base=100.0, drift=1.0, start="2026-01-01"):
    """n 根日线：日期连续日历日递增、close 线性漂移（drift=0 水平 / >0 涨 / <0 跌）。"""
    cur = _d.date.fromisoformat(start)
    rows = []
    for i in range(n):
        rows.append({"dt": cur.isoformat(), "close": base + drift * i})
        cur += _d.timedelta(days=1)
    return rows


# ── 计算层：compute_index_trend（纯函数，2026-10-26 第一批四指数）──


def test_compute_up_down_range_three_states():
    """单边涨→up/high_zone/100%；单边跌→down/low_zone/0%；水平→range/mid_zone/50%。"""
    r = F.compute_index_trend({
        "ndx": _mk_bars(60, 100.0, 1.0),
        "csi300": _mk_bars(60, 4000.0, -5.0),
        "hstech": _mk_bars(60, 4000.0, 0.0),
    })
    up = r["indexes"]["ndx"]
    assert up["trend"] == "up" and up["position"] == "high_zone"
    assert up["pos_q"] == 100.0 and up["dist_high"] == 0 and up["dist_low"] > 0
    assert up["ma_fast_above"] is True and up["ma_fast_above_slow"] is True
    down = r["indexes"]["csi300"]
    assert down["trend"] == "down" and down["position"] == "low_zone"
    assert down["pos_q"] == 0.0 and down["dist_low"] == 0 and down["dist_high"] < 0
    assert down["ma_fast_above"] is False and down["ma_fast_above_slow"] is False
    flat = r["indexes"]["hstech"]
    assert flat["trend"] == "range" and flat["position"] == "mid_zone"
    assert flat["pos_q"] == 50.0                       # 平区间恒 50（hi == lo）
    assert flat["ma_fast_above"] is False              # close == ma → 严格大于为 False


def test_compute_position_quantile_math():
    """先涨后跌：末点恰在区间中部 → pos_q≈51.7（(130−100)/(158−100)），距高 −17.7%。"""
    vals = ([100.0 + 2.0 * i for i in range(30)]          # 100 → 158（30 根）
            + [158.0 - 28.0 / 30 * i for i in range(1, 31)])   # 158 → 130（30 根）
    bars = [{"dt": f"2026-{1 + i // 28:02d}-{1 + i % 28:02d}", "close": v}
            for i, v in enumerate(vals)]
    e = F.compute_index_trend({"ndx": bars})["indexes"]["ndx"]
    assert e["pos_q"] == round(30.0 / 58.0 * 100, 1)      # 51.7
    assert e["position"] == "mid_zone"
    assert e["dist_high"] == pytest.approx(130.0 / 158.0 - 1, abs=1e-9)
    assert e["dist_low"] == pytest.approx(0.30, abs=1e-9)
    assert e["pos_high_252"] == 158.0 and e["pos_low_252"] == 100.0
    assert e["trend"] == "down"                          # 末 20 根下行 → 快线下方 + 20 日负收益


def test_compute_quantile_window_effective():
    """quantile_win=20 只看末 20 根区间（hi=139）；默认 252 看全量（hi=239）→ pos_q 不同。"""
    vals = ([200.0 + i for i in range(40)] + [100.0 + i for i in range(40)])
    bars = [{"dt": f"2026-{1 + i // 28:02d}-{1 + i % 28:02d}", "close": v}
            for i, v in enumerate(vals)]                  # 末点 139
    full = F.compute_index_trend({"ndx": bars})["indexes"]["ndx"]
    short = F.compute_index_trend({"ndx": bars}, quantile_win=20)["indexes"]["ndx"]
    assert full["pos_high_252"] == 239.0 and full["pos_q"] == round(39.0 / 139.0 * 100, 1)
    assert short["pos_high_252"] == 139.0 and short["pos_q"] == 100.0   # 末点=短窗高点
    assert full["pos_q"] != short["pos_q"]


def test_compute_as_of_cutoff():
    """as_of 截断（PIT）：bars_n=30 / close=第30根；缺省 as_of = 各序列末点最大值。"""
    bars = {"ndx": _mk_bars(60, 100.0, 1.0)}
    r = F.compute_index_trend(bars, as_of="2026-01-30")
    e = r["indexes"]["ndx"]
    assert e["bars_n"] == 30 and e["close"] == 129.0 and e["as_of"] == "2026-01-30"
    assert r["as_of"] == "2026-01-30"
    # 缺省：多指数不同末点 → 取最大（b 末根 2026-03-02）
    r2 = F.compute_index_trend({"a": _mk_bars(30, 100.0, 1.0, "2026-01-01"),
                                "b": _mk_bars(30, 100.0, 1.0, "2026-02-01")})
    assert r2["as_of"] == "2026-03-02"


def test_compute_active_ids_inactive():
    """active_ids 白名单外 → inactive 登记（close/as_of 可见）+ missing=inactive，不产出趋势。"""
    bars = {"ndx": _mk_bars(30, 100.0, 1.0), "csi300": _mk_bars(30, 2000.0, 1.0)}
    r = F.compute_index_trend(bars, active_ids={"ndx"})
    assert r["indexes"]["ndx"]["trend"] == "up"           # 白名单内正常
    c = r["indexes"]["csi300"]
    assert c["status"] == "inactive" and c["missing"] == "inactive"
    assert c["close"] == 2029.0 and "trend" not in c and "pos_q" not in c
    # 空集 → 全部停用（登记可见、不判断）
    r2 = F.compute_index_trend(bars, active_ids=set())
    assert all(v["status"] == "inactive" for v in r2["indexes"].values())


def test_compute_degrades():
    """三态降级：<21 根 insufficient（带 note）；剔 None 后空 → unavailable；空入参顶层。"""
    r = F.compute_index_trend({"ndx": _mk_bars(10), "csi300": _mk_bars(60, 3000.0)})
    e = r["indexes"]["ndx"]
    assert e["status"] == "insufficient" and "21 根" in e["note"]
    assert "trend" not in e and "pos_q" not in e
    assert r["indexes"]["csi300"]["trend"] == "up"        # 其余指数不受影响
    # 全 None 收盘 → 清洗后空序列 → unavailable
    bad = {"ndx": [{"dt": "2026-01-01", "close": None}, {"dt": "2026-01-02", "close": None}]}
    assert F.compute_index_trend(bad)["indexes"]["ndx"] == {"status": "unavailable"}
    # 顶层：空 dict / 非 dict → unavailable
    top = {"status": "unavailable", "error": "指数序列为空"}
    assert F.compute_index_trend({}) == top
    assert F.compute_index_trend(None) == top


def test_compute_meta_passthrough_all_states():
    """meta 透传与可用性无关：active/insufficient/inactive 三分支都带 name/market。"""
    meta = {"ndx": {"name": "纳斯达克综合", "market": "US", "source": "新浪财经"},
            "csi300": {"name": "沪深300", "market": "A", "source": "akshare"}}
    # ndx 白名单内但 10 根 → insufficient 分支；csi300 白名单外 → inactive 分支
    r = F.compute_index_trend(
        {"ndx": _mk_bars(10), "csi300": _mk_bars(60, 100.0)},
        active_ids={"ndx"}, meta=meta)
    e = r["indexes"]["ndx"]
    assert e["name"] == "纳斯达克综合" and e["market"] == "US"
    assert e["source"] == "新浪财经" and e["status"] == "insufficient"
    c = r["indexes"]["csi300"]
    assert c["status"] == "inactive" and c["missing"] == "inactive"
    assert c["name"] == "沪深300" and c["market"] == "A"   # inactive 分支同样透传
    # active 分支（足量数据 + 白名单缺省全参与）
    r2 = F.compute_index_trend({"csi300": _mk_bars(60, 100.0)}, meta=meta)
    assert r2["indexes"]["csi300"]["trend"] == "up"
    assert r2["indexes"]["csi300"]["name"] == "沪深300"


def test_compute_deterministic():
    """同输入两次调用输出完全相等（纯函数确定性）。"""
    bars = {"ndx": _mk_bars(60, 100.0, 1.0), "hstech": _mk_bars(60, 4000.0, -5.0)}
    assert (F.compute_index_trend(bars, meta=F.INDEX_TREND_META)
            == F.compute_index_trend(bars, meta=F.INDEX_TREND_META))


# ── 数据层：fetch_index_bars（离线合成，不触网）──────────────────


class _DF:
    """最小 DataFrame 替身：iterrows()/len（datasource 用到这两者 + 列名）。"""

    def __init__(self, rows):
        self._rows = rows
        self.columns = list(rows[0].keys()) if rows else []

    def __len__(self):
        return len(self._rows)

    def iterrows(self):
        return list(enumerate(self._rows))


def _fake_ak(raise_on=()):
    """伪 akshare：引擎D 三接口四指数（列名统一英文 date/close，与真源口径一致）。"""
    m = types.ModuleType("akshare")

    def _guard(name, df):
        if name in raise_on:
            raise RuntimeError(f"upstream {name} down")
        return df

    def _rows(n, base):
        return [{"date": f"2026-{1 + i // 28:02d}-{1 + i % 28:02d}", "close": base + i}
                for i in range(n)]

    m.index_us_stock_sina = lambda symbol=None: _guard("ndx", _DF(_rows(300, 20000.0)))
    m.stock_hk_index_daily_sina = lambda symbol=None: _guard("hstech", _DF(_rows(300, 4000.0)))

    def _zh(symbol=None):
        return _guard("csi300" if symbol == "sh000300" else "chinext", _DF(_rows(300, 500.0)))
    m.stock_zh_index_daily = _zh                    # csi300/chinext 同接口按 symbol 分流
    return m


@pytest.fixture
def _no_retry(monkeypatch):
    monkeypatch.setattr(DS, "_retry", lambda fn, n=3, wait=2.0: fn())   # 去退避等待


def test_fetch_index_bars_offline(_no_retry):
    """四接口全通 → 四指数；window 截断生效；pct 相对前收；start 过滤。"""
    out = DS.fetch_index_bars(_fake_ak(), "2026-01-01")
    assert set(out) == {"ndx", "csi300", "hstech", "chinext"}
    for iid, rows in out.items():
        assert len(rows) == 280                                  # 300 → 近 280 根
        base = 20000.0 if iid == "ndx" else 4000.0 if iid == "hstech" else 500.0
        assert rows[0]["pct"] == pytest.approx(1 / (base + 19))   # 截断后首行（i=20）相对前收 i=19
        assert all(r["dt"] >= "2026-01-01" for r in rows)
    assert out["ndx"][0]["dt"] == "2026-01-21"                   # 300−280=20 根被截
    assert out["ndx"][-1]["close"] == 20299.0                    # 20000+299


def test_fetch_index_bars_pct_first_none(_no_retry):
    """window ≥ 序列长度 → 不截断，全序列首根 pct=None（无前收）。"""
    out = DS.fetch_index_bars(_fake_ak(), "2026-01-01", window=300)
    rows = out["ndx"]
    assert len(rows) == 300 and rows[0]["pct"] is None
    assert rows[1]["pct"] == pytest.approx(1 / 20000.0)           # 第2根相对第1根


def test_fetch_index_bars_start_filter(_no_retry):
    """start 过滤：2026-03-01 起 → 全部行 dt ≥ start 且数量减少。"""
    out = DS.fetch_index_bars(_fake_ak(), "2026-03-01")
    rows = out["ndx"]
    assert all(r["dt"] >= "2026-03-01" for r in rows)
    assert len(rows) == 244                                     # 3 月从 i=56 起（300−56）


def test_fetch_index_bars_single_failure(_no_retry):
    """单指数失败 → 跳过不阻断（增量 fill_missing 用旧值兜底的契约基础）。"""
    out = DS.fetch_index_bars(_fake_ak(raise_on=("hstech",)), "2026-01-01")
    assert set(out) == {"ndx", "csi300", "chinext"}


def test_index_fetch_registry_parity():
    """封闭集对拍：拉取清单 = factors 指数清单 = meta 登记面（改清单须两侧同步）。"""
    assert set(DS._INDEX_FETCH) == set(F.INDEX_TREND_INDEXES)
    assert set(F.INDEX_TREND_META) == set(F.INDEX_TREND_INDEXES)
    assert F.INDEX_TREND_INDEXES == ["ndx", "csi300", "hstech", "chinext"]


# ── 引擎层：IndexTrendEngine（registry 配置注入 + overrides 优先）──


def test_engine_default_parity():
    """registry 缺省路径：run 输出与 F.compute_index_trend 默认调用逐位一致。"""
    from applib.engines import IndexTrendEngine

    bars = {"ndx": _mk_bars(60, 100.0, 1.0)}
    eng = IndexTrendEngine()                                    # 新实例（不污染全局单例）
    assert eng.run(bars, meta=F.INDEX_TREND_META) \
        == F.compute_index_trend(bars, meta=F.INDEX_TREND_META)
    d = eng.describe()
    assert d["id"] == "index_trend" and d["metric_count"] == d["active_count"] == 4
    assert eng.active_ids() == {"ndx", "csi300", "hstech", "chinext"}


def test_engine_registry_stop_and_overrides(tmp_path):
    """registry 停用 chinext → inactive；overrides 覆盖白名单；regime 注入生效。"""
    from applib.engines import IndexTrendEngine
    from applib.engines.base import export_registry_v2

    reg = export_registry_v2()
    for m in reg["engines"]["index_trend"]["metrics"]:
        if m["id"] == "chinext":
            m["status"] = "inactive"
    reg["engines"]["index_trend"]["regime"]["ma_fast"] = 10    # 快线缩至 10 日
    d = tmp_path / "factors"
    d.mkdir(parents=True)
    (d / "registry.json").write_text(json.dumps(reg, ensure_ascii=False), encoding="utf-8")

    eng = IndexTrendEngine().load(tmp_path)
    bars = {"ndx": _mk_bars(60, 100.0, 1.0), "chinext": _mk_bars(60, 2000.0, 1.0)}
    out = eng.run(bars, meta=F.INDEX_TREND_META)
    assert out["indexes"]["chinext"]["status"] == "inactive"   # registry 白名单生效
    assert "trend" not in out["indexes"]["chinext"]
    ndx = out["indexes"]["ndx"]
    assert ndx["ma_fast"] == pytest.approx(154.5)               # regime ma_fast=10：末10根均值
    # overrides 覆盖 registry：active_ids=None → chinext 重新参与判断
    out2 = eng.run(bars, meta=F.INDEX_TREND_META, overrides={"active_ids": None})
    assert out2["indexes"]["chinext"]["trend"] == "up"


# ── 工具层：factor.index_trend（MCP 工具）──────────────────────


def _write_it_ws(tmp_path, index_bars=None):
    """伪造 workspace：sandbox/repo/market/index_bars.json。"""
    d = tmp_path / "sandbox" / "repo" / "market"
    d.mkdir(parents=True, exist_ok=True)
    if index_bars is not None:
        (d / "index_bars.json").write_text(json.dumps(index_bars), encoding="utf-8")
    from applib.ctx import Ctx
    return Ctx(root=str(tmp_path))


def test_tool_index_trend_ok(tmp_path):
    """workspace 有数据 → 四指数输出 + meta 名称注入；as_of 参数截断生效。"""
    from applib.tools import call_tool

    ctx = _write_it_ws(tmp_path, {"ndx": _mk_bars(45, 100.0, 1.0, "2026-07-15"),
                                  "csi300": _mk_bars(45, 4000.0, 1.0, "2026-07-15")})
    res = call_tool("factor.index_trend", {}, ctx)
    assert "error" not in res
    assert res["indexes"]["ndx"]["trend"] == "up"
    assert res["indexes"]["ndx"]["name"] == "纳斯达克综合"      # meta 注入
    assert res["indexes"]["csi300"]["name"] == "沪深300"
    # as_of=2026-08-01 → 截 18 根 < 21 → insufficient（PIT 参数生效）
    res2 = call_tool("factor.index_trend", {"as_of": "2026-08-01"}, ctx)
    assert res2["indexes"]["ndx"]["status"] == "insufficient"


def test_tool_index_trend_missing_file(tmp_path):
    """无 index_bars.json → error 不抛异常。"""
    from applib.ctx import Ctx
    from applib.tools import call_tool

    res = call_tool("factor.index_trend", {}, Ctx(root=str(tmp_path)))
    assert res == {"error": "指数日线数据未同步（先执行数据同步）"}


# ── 集成层：portfolio_review 返回 index_trend（纪律同引擎C）──────


def _stub_alpha(secid, arr, reports, dividends, *a, **kw):
    """compute_alpha 最小替身：跳过财务依赖，字段覆盖 portfolio_review 引用面。"""
    return {"as_of": arr[-1]["dt"], "used_period": None, "used_announce": None,
            "val_pe_ttm": None, "val_pb": None, "val_pe_pct": None, "val_pb_pct": None,
            "div_yield_ttm": None, "qua_roe_ttm": None, "qua_margin": None,
            "grow_rev_yoy": None, "grow_np_yoy": None, "mom_12_1": 0.1, "vol_250": 0.2,
            "pe_hist_n": None, "dims": {}, "grades": {}, "composite": 0.0,
            "composite_grade": "C"}


def _stub_beta(secid, arr, *a, **kw):
    """compute_beta 最小替身：不产生信号（引擎D 纪律断言的基础）。"""
    return {"as_of": arr[-1]["dt"], "close": 10.0, "mom_20d": 0.05, "vol_20d": 0.15,
            "ma20": 1.0, "ma20_break": 0.01, "state": "above", "high20_touch": False,
            "dist_high20": 0.02, "high20_intra": 0.03, "low20": 0.9, "sigma_daily": 0.01,
            "band_low": 0.98, "band_high": 1.02, "signals": []}


def _write_review_ws(tmp_path, index_bars=True):
    """伪造 portfolio_review 所需 workspace（组合/维度/行情/日历/因子注册表 + 指数日线）。"""
    from applib.ctx import Ctx

    repo = tmp_path / "sandbox" / "repo"
    bars = {}
    for secid in ("510300.SH", "600519.SH"):
        cur, rows = _d.date(2026, 8, 15), []
        for i in range(30):                    # 末根 2026-09-13（评估 as_of）
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
        "id": "cn4", "name": "测试组合", "base_ccy": "CNY", "note": "引擎D 集成测试",
        "positions": [{"secid": "510300.SH", "name": "沪深300ETF", "weight": 50.0},
                      {"secid": "600519.SH", "name": "贵州茅台", "weight": 50.0}]}),
        encoding="utf-8")
    (repo / "factors").mkdir(parents=True, exist_ok=True)
    (repo / "factors" / "fundamental_values.json").write_text("{}", encoding="utf-8")
    (repo / "factors" / "registry.json").write_text(json.dumps(
        {"factors": [{"id": "sig_ma20_break", "name": "MA20突破", "status": "active"}]}),
        encoding="utf-8")
    if index_bars:
        # 四指数 45 根（2026-07-15..2026-08-28），末点 ≤ 评估 as_of 09-13（不被截空）
        ib = {iid: _mk_bars(45, base, drift, "2026-07-15")
              for iid, (base, drift) in {
                  "ndx": (20000.0, 10.0), "csi300": (4000.0, 4.0),
                  "hstech": (4000.0, -4.0), "chinext": (3000.0, 3.0)}.items()}
        (repo / "market" / "index_bars.json").write_text(json.dumps(ib), encoding="utf-8")
    return Ctx(root=str(tmp_path))


def test_review_includes_index_trend(tmp_path, monkeypatch):
    """评估含 index_trend 四指数 + 与评估同日截断 + 纪律红线（不产生信号/不进调仓）。"""
    from applib import tools as T

    monkeypatch.setattr(T.F, "compute_alpha", _stub_alpha)
    monkeypatch.setattr(T.F, "compute_beta", _stub_beta)
    ctx = _write_review_ws(tmp_path)
    r = T.portfolio_review({"portfolio": "cn4"}, ctx)
    it = r["index_trend"]
    assert it.get("status") != "unavailable" and "error" not in it
    assert set(it["indexes"]) == {"ndx", "csi300", "hstech", "chinext"}
    assert it["indexes"]["ndx"]["trend"] == "up" and it["indexes"]["ndx"]["position"] == "high_zone"
    assert it["indexes"]["hstech"]["trend"] == "down" and it["indexes"]["hstech"]["position"] == "low_zone"
    assert it["indexes"]["ndx"]["name"] == "纳斯达克综合"
    assert it["as_of"] == r["as_of"] == "2026-09-13"          # 与评估同日截断（PIT）
    # 纪律红线：引擎D 不产生 signals 条目、不参与调仓
    assert all("index_trend" not in json.dumps(s, ensure_ascii=False) for s in r["signals"])
    assert "index_trend" not in inspect.getsource(T.portfolio_rebalance_plan).lower()


def test_review_index_trend_degrades(tmp_path, monkeypatch):
    """无指数日线 → index_trend unavailable 降级，评估其余键不阻断。"""
    from applib import tools as T

    monkeypatch.setattr(T.F, "compute_alpha", _stub_alpha)
    monkeypatch.setattr(T.F, "compute_beta", _stub_beta)
    ctx = _write_review_ws(tmp_path, index_bars=False)
    r = T.portfolio_review({"portfolio": "cn4"}, ctx)
    it = r["index_trend"]
    assert it["status"] == "unavailable" and "error" in it
    assert len(r["positions"]) == 2 and "signals" in r       # 评估主体不受影响
    assert isinstance(r["portfolio_metrics"]["nav"], float)
    assert r["as_of"] == "2026-09-13"


# ── 报告层：render_html / render_md 的 ②.6 章节 ────────────────


def _it_payload():
    """真实引擎输出（ndx 涨 / hstech 跌）作为报告输入。"""
    bars = {"ndx": _mk_bars(60, 100.0, 1.0), "hstech": _mk_bars(60, 4000.0, -5.0)}
    return F.compute_index_trend(bars, meta=F.INDEX_TREND_META)


def _min_review(it):
    """render_html/render_md 最小 review（1 持仓 + 宏观降级 + 指数趋势）。"""
    alpha = {"val_pe_ttm": None, "val_pb": None, "val_pe_pct": None, "val_pb_pct": None,
             "div_yield_ttm": None, "qua_roe_ttm": None, "qua_margin": None,
             "grow_rev_yoy": None, "grow_np_yoy": None, "mom_12_1": 0.1, "vol_250": 0.2,
             "used_period": None}
    beta = {"state": "above", "mom_20d": 0.05, "vol_20d": 0.15, "ma20": 1.0,
            "ma20_break": 0.01, "high20_touch": False, "dist_high20": 0.02,
            "high20_intra": 0.03, "low20": 0.9, "sigma_daily": 0.01,
            "band_low": 0.98, "band_high": 1.02}
    return {"id": "cn4", "name": "测试组合", "as_of": "2026-09-13", "note": "引擎D 报告测试",
            "next_trade_date": "2026-09-14", "holiday_days": 0,
            "portfolio_metrics": {"day_ret": 0.001, "ret_20d": 0.02, "ret_ytd": 0.05,
                                  "ret_1y": 0.1, "max_dd_1y": -0.08, "nav": 1.05,
                                  "method": "等权"},
            "macro_liquidity": {"status": "unavailable", "error": "x"},
            "index_trend": it,
            "positions": [{"secid": "510300.SH", "name": "沪深300ETF", "industry": "宽基ETF",
                           "weight": 100.0, "asset_type": "ETF", "close": 10.0, "day_pct": 0.005,
                           "alpha": alpha, "beta": beta, "grades": {}, "composite": None,
                           "composite_grade": "C", "flags": ["测试"]}],
            "signals": [], "decay_caveats": []}


_PLAN_MIN = {"plan_id": "p0", "status": "draft", "diff_trades": [], "turnover_est": "0%",
             "cost_est_cny": 0, "cost_note": ""}


def _prov_ctx(tmp_path):
    from applib.ctx import Ctx

    d = tmp_path / "sandbox" / "repo" / "market"
    d.mkdir(parents=True, exist_ok=True)
    (d / "provenance.json").write_text(json.dumps({
        "akshare_version": "test", "fetched_at": "2026-09-13",
        "pit_rules": {"note": "t"}, "sources": []}), encoding="utf-8")
    return Ctx(root=str(tmp_path))


def test_render_html_index_trend_section(tmp_path):
    """正常态：②.6 标题 + 位置温度计 + 明细表 + 口径注记。"""
    from applib import report as RP

    html = RP.render_html(_min_review(_it_payload()), _PLAN_MIN, _prov_ctx(tmp_path),
                          "run_smoke", "v0", "deadbeef", {})
    assert "②.6 指数级别趋势" in html and "Engine D" in html
    assert "高位区" in html and "上行" in html               # ndx 温度计行
    assert "低位区" in html and "下行" in html               # hstech 温度计行
    assert "纳斯达克综合" in html and "恒生科技" in html       # 明细表指数名
    assert "不构成买卖信号" in html                          # 纪律口径
    assert "不参与权重决策" in html                          # warn 注记
    assert "指数趋势数据未同步" not in html


def test_render_html_index_trend_unavailable(tmp_path):
    """unavailable：单行降级提示，无温度计/明细表。"""
    from applib import report as RP

    html = RP.render_html(_min_review({"status": "unavailable", "error": "数据未同步"}),
                          _PLAN_MIN, _prov_ctx(tmp_path), "run_smoke", "v0", "deadbeef", {})
    assert "指数趋势数据未同步" in html and "不影响本报告其余结论" in html
    assert "高位区" not in html and "上行" not in html        # 无档位/趋势词
    assert ">None<" not in html


def test_render_md_index_trend_section():
    """MD 小节：'## 指数级别趋势' + 指数行 + 口径注记；unavailable 单行降级。"""
    from applib import report as RP

    md = RP.render_md(_min_review(_it_payload()), _PLAN_MIN, "run_smoke", "v0", "deadbeef", {})
    assert "## 指数级别趋势" in md and "| 纳斯达克综合" in md
    assert "不参与权重决策" in md and "不构成买卖信号" in md
    md_bad = RP.render_md(_min_review({"status": "unavailable", "error": "x"}),
                          _PLAN_MIN, "run_smoke", "v0", "deadbeef", {})
    assert "## 指数级别趋势" in md_bad and "不影响本报告其余结论" in md_bad
