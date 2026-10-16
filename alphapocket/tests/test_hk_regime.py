"""港美利率/汇率 regime 条件变量测试（H-0006：carry_hk_us / peg_pressure）

覆盖「数据 + 计算 + 工具」三层：
- 计算：``factors.compute_hk_regime`` 的息差/弱方保证压力/前向填充/as_of 截断/状态触发
- 数据：``datasource.fetch_hk_rates`` 的三源合并、前值兜底、失败降级（不抛异常）
- 工具：``factor.regime`` 从 ``market/hk_rates.json`` 读取并输出市场级 state（缺失给 error）

运行：``cd alphapocket && python -m pytest tests/test_hk_regime.py -q``
"""
from __future__ import annotations

import json
import sys
import types

import pytest

from applib import datasource as DS
from applib import factors as F
from applib.ctx import Ctx
from applib.tools import call_tool


# ── 计算层：compute_hk_regime ───────────────────────────
def _series(n: int, hibor=4.0, fed=3.0, usdhkd=7.75, fed_step=0.0):
    """合成逐日序列：dt / hibor1m / fed / usdhkd（fed_step=每日联邦基金增量）"""
    return [{"dt": f"2024-{1 + i // 28:02d}-{1 + i % 28:02d}",
             "hibor1m": hibor, "fed": fed + fed_step * i, "usdhkd": usdhkd}
            for i in range(n)]


def test_carry_and_peg_pressure_values():
    r = F.compute_hk_regime(_series(3, hibor=4.2, fed=3.5, usdhkd=7.80))
    assert r["carry_hk_us"] == pytest.approx(0.7)                 # 4.2 − 3.5
    assert r["peg_pressure"] == pytest.approx(0.5)                # (7.80 − 7.75)/0.10
    assert r["usdhkd"] == pytest.approx(7.80)
    assert r["as_of"] == "2024-01-03"


def test_state_neutral_when_within_band():
    r = F.compute_hk_regime(_series(30, usdhkd=7.80))
    assert r["peg_weak_side"] is False and r["carry_widen"] is False
    assert r["state"] == "neutral"


def test_peg_weak_side_triggers_outflow_pressure():
    r = F.compute_hk_regime(_series(30, usdhkd=7.86))
    assert r["peg_pressure"] >= 1.0
    assert r["peg_weak_side"] is True
    assert r["state"] == "outflow_pressure"


def test_carry_widen_triggers_outflow_pressure():
    # fed 每日 +0.03 → 20 日后 carry 走阔约 −0.6 pct（≥ 0.5 阈值）
    r = F.compute_hk_regime(_series(25, hibor=4.0, fed=3.0, usdhkd=7.78, fed_step=0.03))
    assert r["carry_chg20"] <= -F.HK_CARRY_WIDEN_PT
    assert r["carry_widen"] is True and r["peg_weak_side"] is False
    assert r["state"] == "outflow_pressure"


def test_carry_widen_insufficient_history():
    r = F.compute_hk_regime(_series(10, hibor=4.0, fed=3.0, fed_step=0.05))
    assert r["carry_chg20"] is None                              # 样本不足 chg_win
    assert r["carry_widen"] is False and r["state"] == "neutral"


def test_forward_fill_and_as_of_truncation():
    series = [
        {"dt": "2024-01-02", "hibor1m": 4.0, "fed": None, "usdhkd": 7.76},
        {"dt": "2024-01-03", "hibor1m": None, "fed": 5.0, "usdhkd": None},
        {"dt": "2024-01-04", "hibor1m": None, "fed": None, "usdhkd": 7.90},
    ]
    r = F.compute_hk_regime(series)                              # 末点：各字段取最近非空
    assert r["hibor1m"] == 4.0 and r["fed"] == 5.0 and r["usdhkd"] == 7.90
    assert r["as_of"] == "2024-01-04"
    early = F.compute_hk_regime(series, as_of="2024-01-03")      # 截断到 01-03：usdhkd 取 7.76
    assert early["as_of"] == "2024-01-03" and early["usdhkd"] == pytest.approx(7.76)


def test_empty_series_degrades():
    r = F.compute_hk_regime([])
    assert r["carry_hk_us"] is None and r["peg_pressure"] is None
    assert r["state"] == "neutral" and r["as_of"] is None


# ── 数据层：fetch_hk_rates（akshare 打桩，不触网）───────
class _DF:
    """最小 DataFrame 替身：仅提供 iterrows()（datasource 只用到它）"""

    def __init__(self, rows):
        self._rows = rows

    def iterrows(self):
        return list(enumerate(self._rows))


def _fake_akshare(raise_on=None):
    m = types.ModuleType("akshare")

    def _guard(name):
        if raise_on == name:
            raise RuntimeError(f"upstream {name} down")

    m.rate_interbank = lambda **kw: (_guard("hibor"), _DF([
        {"报告日": "2024-01-02", "利率": 4.0},
        {"报告日": "2024-01-04", "利率": 4.5},
    ]))[1]
    m.macro_bank_usa_interest_rate = lambda: (_guard("fed"), _DF([
        {"日期": "2024-01-03", "今值": 5.25, "前值": 5.25},
        {"日期": "2024-01-05", "今值": None, "前值": 5.25},      # 今值缺失 → 前值兜底
    ]))[1]

    def _boc(symbol, start_date, end_date):
        _guard("fx")
        if symbol == "美元":
            return _DF([{"日期": "2024-01-02", "中行折算价": 700.0},
                        {"日期": "2024-01-04", "中行折算价": 710.0}])
        return _DF([{"日期": "2024-01-02", "中行折算价": 89.0},
                    {"日期": "2024-01-04", "中行折算价": 90.0}])

    m.currency_boc_sina = _boc
    return m


@pytest.fixture
def patched_datasource(monkeypatch):
    monkeypatch.setattr(DS, "_retry", lambda fn, n=3, wait=2.0: fn())   # 去退避等待


def test_fetch_hk_rates_merges_and_forward_fills(monkeypatch, patched_datasource):
    monkeypatch.setitem(sys.modules, "akshare", _fake_akshare())
    out = DS.fetch_hk_rates(start_date="2024-01-01")
    assert "error" not in out
    s = out["series"]
    assert [r["dt"] for r in s] == ["2024-01-02", "2024-01-03", "2024-01-04", "2024-01-05"]
    assert s[0]["fed"] is None                                   # 首值之前 → None
    assert s[0]["usdhkd"] == pytest.approx(700.0 / 89.0)         # USDCNY/HKDCNY 交叉
    assert s[1]["hibor1m"] == 4.0 and s[1]["fed"] == 5.25        # 前向填充
    assert s[3]["fed"] == 5.25 and s[3]["hibor1m"] == 4.5        # 今值缺失 → 前值兜底
    assert out["as_of"] == "2024-01-05"
    # 端到端：序列直接喂给 compute_hk_regime（USDHKD 7.89 ≥ 7.85 → 弱方保证）
    r = F.compute_hk_regime(s)
    assert r["state"] == "outflow_pressure" and r["peg_weak_side"] is True


def test_fetch_hk_rates_failure_returns_error(monkeypatch, patched_datasource):
    monkeypatch.setitem(sys.modules, "akshare", _fake_akshare(raise_on="hibor"))
    out = DS.fetch_hk_rates()
    assert out["series"] == [] and out["as_of"] is None
    assert "hibor" in out["error"]                               # 降级为 error，不抛异常


# ── 工具层：factor.regime ───────────────────────────────
def _write_hk_rates(root, payload):
    d = root / "sandbox" / "repo" / "market"
    d.mkdir(parents=True, exist_ok=True)
    (d / "hk_rates.json").write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    return Ctx(root=str(root))


def test_tool_factor_regime_reads_and_reports_state(tmp_path):
    series = _series(30, hibor=4.0, fed=3.0, usdhkd=7.86)
    ctx = _write_hk_rates(tmp_path, {
        "as_of": series[-1]["dt"], "start": "2024-01-01",
        "source": {"hibor1m": "stub"}, "series": series})
    res = call_tool("factor.regime", {}, ctx)
    assert "error" not in res
    assert res["state"] == "outflow_pressure"
    assert set(res["factors"]) == set(F.HK_REGIME_FACTORS)
    assert res["factors"]["peg_pressure"] >= 1.0
    assert res["as_of"] == series[-1]["dt"]


def test_tool_factor_regime_missing_data_errors(tmp_path):
    res = call_tool("factor.regime", {}, Ctx(root=str(tmp_path)))
    assert "error" in res and "未就绪" in res["error"]


def test_tool_factor_regime_empty_series_errors(tmp_path):
    ctx = _write_hk_rates(tmp_path, {"series": [], "error": "RuntimeError: upstream down"})
    res = call_tool("factor.regime", {}, ctx)
    assert "error" in res and "upstream down" in res["error"]
