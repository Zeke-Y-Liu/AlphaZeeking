"""R1 参数敏感性扫描测试（《研究缺口R1-R4实施计划》§3.3 DoD）

- 尖峰判定注入测试：构造确认带 0.5% 的噪声档（相邻格绩效差 > 30%）→ 扫描器正确 flag
- 全 6 类参数扫描覆盖 + 2 张热力图
- 锚点不破坏：run_scan 内嵌 anchor_check（stops=False+等权 ↔ run_backtest）逐位一致
运行：``cd alphapocket && python -m pytest tests/ -q``
"""
from __future__ import annotations

import datetime as dt
import math

import pytest

from applib import sensitivity as SEN


# ── 尖峰判定注入测试（DoD③：0.5% 噪声档 → 相邻格差 > 30% → flag）──
def _mk(cagr: dict):
    def run(v):
        return {"metrics": {"cagr": cagr[v], "sharpe": 1.0, "max_dd": -0.1,
                            "ann_vol": 0.2, "total_return": 0.5},
                "turnover": 0.0, "n_trades": 0}
    return run


def test_spike_flag_on_noise_band():
    # 0.5% 噪声档：信号在窄带内频繁触发 → CAGR 骤降至 2%，与相邻档 10% 相差 4 倍
    cagr = {0.005: 0.02, 0.010: 0.10, 0.016: 0.10, 0.020: 0.10, 0.024: 0.10}
    o = SEN._oat("确认带宽", [0.005, 0.010, 0.016, 0.020, 0.024], _mk(cagr), 0.020)
    flagged = [r["value"] for r in o["rows"] if r["flagged"]]
    assert 0.010 in flagged and o["n_flagged"] >= 1
    assert o["rows"][1]["rel_change"] == pytest.approx(4.0)      # |0.10 − 0.02| / 0.02
    assert o["verdict"].startswith("存在尖峰")


def test_flat_scan_not_flagged():
    cagr = {0.010: 0.10, 0.016: 0.10, 0.020: 0.10, 0.024: 0.10}
    o = SEN._oat("确认带宽", [0.010, 0.016, 0.020, 0.024], _mk(cagr), 0.020)
    assert o["n_flagged"] == 0 and o["verdict"].startswith("平坦")


# ── 端到端：合成数据跑全量扫描（6 类参数 + 2 热力图 + 锚点）──
def _synth_bars(n: int = 300) -> dict:
    d0, out = dt.date(2021, 1, 4), {}
    for k in range(2):
        px, prev, arr = 100.0, None, []
        for i in range(n):
            px *= 1 + 0.002 * math.sin((i + k) / 9.0)
            pct = 0.0 if prev is None else px / prev - 1
            arr.append({"dt": (d0 + dt.timedelta(days=i)).isoformat(), "open": round(px, 4),
                        "high": round(px * 1.008, 4), "low": round(px * 0.992, 4),
                        "close": round(px, 4), "volume": 1, "amount": 1.0, "pct": pct})
            prev = px
        out[f"T{k}.XX"] = arr
    return out


def test_run_scan_full_param_coverage_and_anchor():
    pf = {"id": "TST", "name": "测试组合",
          "positions": [{"secid": "T0.XX", "name": "测试0"}, {"secid": "T1.XX", "name": "测试1"}]}
    rep = SEN.run_scan(pf, _synth_bars(), {}, start="2021-03-01", end="2021-10-01")
    assert {"rebalance", "cost_bps", "win", "band", "band_sigma", "weights"} <= set(rep["oat"])
    assert set(rep["heatmaps"]) == {"win_x_band", "rebalance_x_cost"}
    assert rep["anchor_check"]["ok"] is True                     # 重构未破坏 V4-α 锚点
    assert all("verdict" in v for v in rep["oat"].values())      # 每参数输出平坦/敏感结论行
    assert isinstance(rep["flagged"], list)
    assert SEN.render_md(rep) and SEN.render_html(rep)
