"""T-R5：引擎A 配置化测试——五维权重/锚点/启停注册表驱动 + golden_test 清单收敛。

钉死六件事：
1. 默认调用对拍基线（包装器/显式注入 == factors 直调，逐位）；
2. 单因子 inactive（mom_12_1）→ 值 None + flags 标记 + 动量维剔除重归一化
   （composite == 动量零权参考，逐位）；
3. 全部估值因子 inactive → 估值维 None + 权重重归一化（复用现语义）；
4. registry 真实文件驱动（改权重/锚点/停用 qua_fcf）→ 与同参直调逐位一致 + 方向正确；
5. golden_test 对账清单 registry 驱动：inactive 因子跳过对账仍全绿；
6. sensitivity 权重取法经引擎（惰性取 + 单例降级 + 显式覆盖）。

运行：``cd alphapocket && python -m pytest tests/test_engines_alpha_cfg.py -q``
"""
from __future__ import annotations

import datetime as dt
import json
import logging
import re

import duckdb
import pytest

from applib import engines as E
from applib import factors as F
from applib import runner as RG
from applib import sensitivity as SEN
from applib.ctx import Ctx
from applib.engines import AlphaEngine
from applib.engines.base import export_registry_v2

# ── 合成数据（确定性，无随机）────────────────────────────────


def _bars(n=300, base=10.0):
    d0 = dt.date(2025, 1, 1)
    px, arr = base, []
    for i in range(n):
        px *= 1 + ((i % 7) - 3) / 500.0        # 温和锯齿走势
        arr.append({"dt": (d0 + dt.timedelta(days=i)).isoformat(), "open": px,
                    "high": px * 1.01, "low": px * 0.99, "close": px,
                    "volume": 1e6, "amount": 1e7, "pct": ((i % 7) - 3) / 500.0})
    return arr


def _reports():
    """A 股口径（YTD 递推 TTM）：FY2024 + FY2023同期 + 2025H1，as_of=2025-10-27 可用。"""
    return [
        {"period": "2024-06-30", "announce_date": "2024-08-25", "eps_ytd": 1.15, "bps": 23.5,
         "margin": 17.5, "rev_g": 11.0, "np_g": 14.0, "total_assets": 195.0, "total_liab": 88.0,
         "ocf": 38.0, "capex": 9.0, "revenue": 155.0, "net_profit": 23.0},
        {"period": "2024-12-31", "announce_date": "2025-03-28", "eps_ytd": 2.40, "bps": 24.0,
         "margin": 18.0, "rev_g": 12.0, "np_g": 15.0, "total_assets": 200.0, "total_liab": 90.0,
         "ocf": 40.0, "capex": 10.0, "revenue": 160.0, "net_profit": 24.0},
        {"period": "2025-06-30", "announce_date": "2025-08-26", "eps_ytd": 1.30, "bps": 24.8,
         "margin": 19.0, "rev_g": 14.0, "np_g": 17.0, "total_assets": 204.0, "total_liab": 91.0,
         "ocf": 44.0, "capex": 10.0, "revenue": 170.0, "net_profit": 26.0},
    ]


def _dividends():
    return [{"ex_date": "2025-06-16", "announce": "2025-05-20", "dps": 0.6}]


def _fund():
    return {"reports": _reports(), "dividends": _dividends()}


def _asof():
    return _bars()[-1]["dt"]


@pytest.fixture(autouse=True)
def _reset_singletons():
    """防跨用例污染：单例引擎回归内置默认（前后各清一次，防 tmp registry 泄漏）。"""
    for eid in ("alpha", "beta", "macro", "index_trend"):
        E.get_engine(eid).registry = None
    yield
    for eid in ("alpha", "beta", "macro", "index_trend"):
        E.get_engine(eid).registry = None


# ── ① 默认对拍基线 ────────────────────────────────────────────


def test_default_parity_engine_vs_factors():
    bars, fd = _bars(), _fund()
    for kw in ({}, {"as_of": "2025-09-01"},
               {"weights": {"估值": .4, "质量": .2, "成长": .2, "动量": .1, "红利低波": .1}}):
        assert AlphaEngine().run("600519.SH", bars, fd["reports"], fd["dividends"], **kw) == \
            F.compute_alpha("600519.SH", bars, fd["reports"], fd["dividends"], **kw)


def test_explicit_anchors_weights_parity():
    """注入与内置同值的 weights/anchors → 与默认调用全字段逐位一致。"""
    bars, fd = _bars(), _fund()
    anc = {k: [list(p) for p in v] for k, v in F._ALPHA_ANCHORS.items()}
    a = F.compute_alpha("600519.SH", bars, fd["reports"], fd["dividends"],
                        weights=dict(F.DEFAULT_DIM_WEIGHTS), anchors=anc)
    assert a == F.compute_alpha("600519.SH", bars, fd["reports"], fd["dividends"])


# ── ② 单因子 inactive：mom_12_1 ───────────────────────────────


def test_single_factor_inactive_mom_12_1():
    bars, fd = _bars(), _fund()
    base = F.compute_alpha("600519.SH", bars, fd["reports"], fd["dividends"])
    act = set(F.ENGINE_A_FACTORS) - {"mom_12_1"}
    out = F.compute_alpha("600519.SH", bars, fd["reports"], fd["dividends"], active_ids=act)
    assert base["mom_12_1"] is not None
    assert out["mom_12_1"] is None and "factor_mom_12_1_inactive" in out["flags"]
    assert out["dims"]["动量"] is None                      # 退出维度评分输入
    # 维度缺失缩分母 + 权重重归一化 == 动量零权参考（逐位，现语义）
    ref = F.compute_alpha("600519.SH", bars, fd["reports"], fd["dividends"],
                          weights={**F.DEFAULT_DIM_WEIGHTS, "动量": 0.0})
    assert out["composite"] == ref["composite"] and out["composite"] != base["composite"]


# ── ③ 全部估值因子 inactive：估值维整体退场 ───────────────────


def test_all_valuation_inactive_renormalize():
    bars, fd = _bars(), _fund()
    val_ids = {"val_pe_ttm", "val_pb", "val_pe_pct", "val_pb_pct"}
    act = set(F.ENGINE_A_FACTORS) - val_ids
    out = F.compute_alpha("600519.SH", bars, fd["reports"], fd["dividends"], active_ids=act)
    assert all(out[f] is None for f in val_ids)
    assert {f"factor_{f}_inactive" for f in val_ids} <= set(out["flags"])
    assert out["dims"]["估值"] is None
    ref = F.compute_alpha("600519.SH", bars, fd["reports"], fd["dividends"],
                          weights={**F.DEFAULT_DIM_WEIGHTS, "估值": 0.0})
    assert out["composite"] == ref["composite"]
    assert out["dims"]["质量"] is not None                  # 其余维度不受影响


# ── ④ registry 真实文件驱动 ──────────────────────────────────


def _write_registry(tmp_path, reg):
    d = tmp_path / "factors"
    d.mkdir(parents=True, exist_ok=True)
    (d / "registry.json").write_text(json.dumps(reg, ensure_ascii=False), encoding="utf-8")


def test_registry_file_drives_run(tmp_path):
    reg = export_registry_v2()
    reg["engines"]["alpha"]["dim_weights"]["动量"] = 0.30
    reg["engines"]["alpha"]["dim_weights"]["估值"] = 0.10
    reg["engines"]["alpha"]["anchors"]["s_mom"] = [[-30, 20], [0, 50], [60, 100]]
    reg["engines"]["alpha"]["metrics"] = [dict(m, status="inactive") if m["id"] == "qua_fcf" else m
                                          for m in reg["engines"]["alpha"]["metrics"]]
    _write_registry(tmp_path, reg)
    eng = AlphaEngine().load(tmp_path)
    bars, fd = _bars(), _fund()
    assert eng.weights()["动量"] == 0.30
    assert eng.active_ids() == set(F.ENGINE_A_FACTORS) - {"qua_fcf"}
    r = eng.run("600519.SH", bars, fd["reports"], fd["dividends"])
    ref = F.compute_alpha("600519.SH", bars, fd["reports"], fd["dividends"],
                          weights=dict(reg["engines"]["alpha"]["dim_weights"]),
                          anchors=dict(reg["engines"]["alpha"]["anchors"]),
                          active_ids=eng.active_ids())
    assert r == ref                                         # registry 驱动 == 同参直调（逐位）
    assert r["qua_fcf"] is None and "factor_qua_fcf_inactive" in r["flags"]
    base = F.compute_alpha("600519.SH", bars, fd["reports"], fd["dividends"])
    assert r["dims"]["动量"] != base["dims"]["动量"]         # 锚点/权重生效 → 方向变化
    assert r["composite"] != base["composite"]
    # overrides 优先于 registry：权重覆盖生效（anchors/active_ids 仍随 registry）
    ovr_w = {"估值": .5, "质量": .1, "成长": .1, "动量": .1, "红利低波": .2}
    r2 = eng.run("600519.SH", bars, fd["reports"], fd["dividends"], overrides={"weights": ovr_w})
    assert r2["composite"] == F.compute_alpha("600519.SH", bars, fd["reports"], fd["dividends"],
                                              weights=ovr_w,
                                              anchors=dict(reg["engines"]["alpha"]["anchors"]),
                                              active_ids=eng.active_ids())["composite"]


def test_registry_missing_degrades_to_builtin(tmp_path, caplog):
    with caplog.at_level(logging.WARNING, logger="ap.engines"):
        eng = AlphaEngine().load(tmp_path)                  # 无 registry.json → 透明降级
    assert "注册表不存在" in caplog.text
    bars, fd = _bars(), _fund()
    assert eng.run("600519.SH", bars, fd["reports"], fd["dividends"]) == \
        F.compute_alpha("600519.SH", bars, fd["reports"], fd["dividends"])
    assert eng.active_ids() == set(F.ENGINE_A_FACTORS)      # 16 因子全 active


# ── ⑤ golden_test 对账清单 registry 驱动 ─────────────────────


def _build_ws(tmp_path):
    """最小 ws：repo JSON（1 股票 + 1 ETF）+ factor_value_daily（factors 真源落库）。"""
    ctx = Ctx(root=str(tmp_path / "ws"))
    (ctx.local_repo / "market").mkdir(parents=True, exist_ok=True)
    (ctx.local_repo / "factors").mkdir(parents=True, exist_ok=True)
    ctx.db_path.parent.mkdir(parents=True, exist_ok=True)
    bars = {"600519.SH": _bars(), "510300.SH": _bars(base=4.0)}
    fund = {"600519.SH": _fund()}
    (ctx.local_repo / "market" / "daily_bars.json").write_text(json.dumps(bars), encoding="utf-8")
    (ctx.local_repo / "factors" / "fundamental_values.json").write_text(json.dumps(fund), encoding="utf-8")
    last = _asof()
    rows = []
    for secid, arr in bars.items():
        f = fund.get(secid, {})
        a = F.compute_alpha(secid, arr, f.get("reports", []), f.get("dividends", []), as_of=last)
        b = F.compute_beta(secid, arr, as_of=last)
        for fid in (*F.ENGINE_A_FACTORS, *F.ENGINE_B_FACTORS):
            v = a.get(fid, b.get(fid))
            if v is not None:
                rows.append((last, secid, fid, float(v)))
    con = duckdb.connect(str(ctx.db_path))
    con.execute("CREATE TABLE factor_value_daily (dt DATE, secid TEXT, factor_id TEXT, value DOUBLE)")
    con.executemany("INSERT INTO factor_value_daily VALUES (?,?,?,?)", rows)
    con.close()
    return ctx


def _checked(rep: dict) -> int:
    m = re.match(r"pass\((\d+) 项对账\)", rep["golden_test"])
    assert m, rep["golden_test"]
    return int(m.group(1))


def test_golden_test_registry_active_list(tmp_path):
    ctx = _build_ws(tmp_path)
    r0 = RG._golden_test(ctx)
    n0 = _checked(r0)                                       # 1 股票 16 项 + 1 ETF 7 项
    assert n0 == 23
    # registry：qua_margin 停用 → 退出对账面（预期行为），其余仍全绿
    reg = export_registry_v2()
    reg["engines"]["alpha"]["metrics"] = [dict(m, status="inactive") if m["id"] == "qua_margin" else m
                                          for m in reg["engines"]["alpha"]["metrics"]]
    (ctx.local_repo / "factors" / "registry.json").write_text(json.dumps(reg, ensure_ascii=False),
                                                              encoding="utf-8")
    r1 = RG._golden_test(ctx)
    assert _checked(r1) == n0 - 1                           # 仅 qua_margin（仅股票有）退出
    # 全 16 因子停用 → 引擎A整面退出对账，仅剩引擎B 5×2=10 项
    reg2 = export_registry_v2()
    reg2["engines"]["alpha"]["metrics"] = [dict(m, status="inactive")
                                           for m in reg2["engines"]["alpha"]["metrics"]]
    (ctx.local_repo / "factors" / "registry.json").write_text(json.dumps(reg2, ensure_ascii=False),
                                                              encoding="utf-8")
    r2 = RG._golden_test(ctx)
    assert _checked(r2) == 10
    assert all(r["golden_test"].startswith("pass(") for r in (r0, r1, r2))


# ── ⑥ sensitivity 权重取法经引擎 ─────────────────────────────


def test_sensitivity_alpha_weights_via_engine(tmp_path):
    assert SEN._alpha_weights() == dict(F.DEFAULT_DIM_WEIGHTS)   # 单例未加载 → 内置默认
    bars, fd = _bars(), _fund()
    d = _asof()
    # 综合分经引擎 == 直调（默认路径逐位）
    assert SEN._alpha_composite("600519.SH", bars, fd, d, None) == \
        F.compute_alpha("600519.SH", bars, fd["reports"], fd["dividends"], d)["composite"]
    # 显式 weights（OAT 变体）→ 覆盖 registry，与直调一致
    w = {**F.DEFAULT_DIM_WEIGHTS, "动量": 0.0}
    assert SEN._alpha_composite("600519.SH", bars, fd, d, w) == \
        F.compute_alpha("600519.SH", bars, fd["reports"], fd["dividends"], d, weights=w)["composite"]
    # registry 加载到单例 → weights()/composite 随 registry
    reg = export_registry_v2()
    reg["engines"]["alpha"]["dim_weights"]["动量"] = 0.30
    _write_registry(tmp_path, reg)
    eng = E.get_engine("alpha").load(tmp_path)
    assert SEN._alpha_weights()["动量"] == 0.30
    assert SEN._alpha_composite("600519.SH", bars, fd, d, None) == eng.run(
        "600519.SH", bars, fd["reports"], fd["dividends"], as_of=d)["composite"]
