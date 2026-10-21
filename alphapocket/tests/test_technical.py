"""technical.py / industry.py 纯函数单测（单公司分析 T2.3 / T1.1 的基础件）。

运行：cd alphapocket && python -m pytest tests/test_technical.py -q
"""
from __future__ import annotations

from applib.industry import SW_L1, code_of, match_sw_l1
from applib.technical import kdj, oversold_score, rsi


def _bars(closes: list[float], highs: list[float] | None = None) -> list[dict]:
    highs = highs or [c + 1 for c in closes]
    return [{"dt": f"2026-01-{i + 1:02d}", "open": c, "high": h, "low": c - 1,
             "close": c, "volume": 1, "amount": 1, "pct": None}
            for i, (c, h) in enumerate(zip(closes, highs))]


def test_rsi_extremes_and_insufficient():
    assert rsi(_bars(list(range(100, 60, -1)))) == 0.0        # 单调下跌 → 0
    assert rsi(_bars(list(range(60, 100)))) == 100.0          # 单调上涨 → 100
    assert rsi(_bars([10.0] * 10)) is None                    # 样本不足（<n+1）
    flat = rsi(_bars([10.0] * 20))                            # 零波动 → 100（无下跌）
    assert flat in (None, 100.0)


def test_kdj_shape_and_bounds():
    out = kdj(_bars([10.0] * 3))
    assert out == {"k": None, "d": None, "j": None}
    out = kdj(_bars(list(range(20, 60))))
    assert set(out) == {"k", "d", "j"}
    assert 0 <= out["k"] <= 100 and 0 <= out["d"] <= 100


def test_oversold_score_down_series():
    closes = [100 - i for i in range(40)]
    out = oversold_score(_bars(closes))
    assert out["oversold"] is True
    assert out["score"] >= 2
    assert out["rev_20d"] is not None and out["rev_20d"] > 0
    assert out["dist_high20"] is not None and out["dist_high20"] < 0
    assert out["bars_n"] == 40


def test_oversold_score_short_sample_degrades():
    out = oversold_score(_bars([10.0, 9.9]))
    assert out["rsi"] is None and out["k"] is None
    assert out["oversold"] is False


def test_match_sw_l1_labels():
    assert match_sw_l1("汽车零部件·轮胎") == "汽车"
    assert match_sw_l1("广告传媒") == "传媒"
    assert match_sw_l1("塑料软包装") == "轻工制造"
    assert match_sw_l1("原油(QDII)") is None
    assert match_sw_l1(None, "橡胶和塑料制品业") == "基础化工"


def test_sw_code_roundtrip():
    for code, name in SW_L1.items():
        assert code_of(name) == code
    assert code_of("不存在行业") is None
