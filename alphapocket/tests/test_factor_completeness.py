"""因子有效性完备性审计（RG-5）：registry 每个 active 内置因子必须有测试覆盖。

完备性口径（自动扫描，免手工映射腐化）：
- 证据A：``tests/`` 中**其它测试文件**出现因子 id 文本引用（既有覆盖）；
- 证据B：``applib/runner.py`` golden 对账显式覆盖（R1 真数据全对账）；
- 证据C：本文件 ``LOCAL_NUMERICAL``——在此写了**数值/性质断言**的因子（本文件引用
  不算证据，防自欺）；

任一 active 因子三证据皆无 → audit fail 并列出（驱动补测或下线因子）。
另含 P1.3/P1.4 零覆盖缺口的补测：liq_amihud_20d / liq_roll_60d / liq_cs_60d /
turnover_pct（合成序列性质断言）。
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from applib.engines.base import export_registry_v2
from applib.factors import compute_alpha, compute_beta

TESTS = Path(__file__).resolve().parent
RUNNER_GOLDEN = (Path(__file__).resolve().parent.parent
                 / "applib" / "runner.py").read_text(encoding="utf-8")

# 证据C：本文件写了实际断言的因子（新增补测时在此登记）
LOCAL_NUMERICAL = {"liq_amihud_20d", "liq_roll_60d", "liq_cs_60d", "turnover_pct"}


def _active_builtin_ids() -> set[str]:
    d = export_registry_v2()
    out = set()
    for eng in d["engines"].values():
        for items in eng.values():
            if isinstance(items, list):
                out |= {i["id"] for i in items if i.get("status") == "active"}
    return out


def _other_test_evidence(fid: str) -> bool:
    for p in TESTS.glob("test_*.py"):
        if p.name == Path(__file__).name:
            continue
        if fid in p.read_text(encoding="utf-8"):
            return True
    return False


def test_factor_completeness_audit() -> None:
    """完备性审计：active 内置因子零测试覆盖 → fail（缺一个都不许）。"""
    missing = sorted(
        fid for fid in _active_builtin_ids()
        if not (_other_test_evidence(fid) or fid in RUNNER_GOLDEN
                or fid in LOCAL_NUMERICAL)
    )
    assert not missing, (
        f"以下 {len(missing)} 个 active 内置因子无任何测试覆盖"
        f"（单元引用/golden 对账/本文件数值断言三证据皆无），"
        f"请补测或在 registry 中下线：{missing}"
    )


# ── P1.4 微观流动性族补测（compute_alpha 合成序列性质断言）──────


def _bars(closes: list[float], amounts: list[float | None] | None = None,
          highs: list[float] | None = None, lows: list[float] | None = None) -> list[dict]:
    n = len(closes)
    amounts = amounts if amounts is not None else [None] * n
    return [{"dt": f"2026-{1 + i // 28:02d}-{1 + i % 28:02d}",
             "close": c, "high": (highs[i] if highs else c),
             "low": (lows[i] if lows else c), "amount": amounts[i]}
            for i, c in enumerate(closes)]


def test_liq_amihud() -> None:
    """Amihud 非流动性：|r|/成交额 ×1e9 —— 无额降级 None；有额与手算一致；额越大越不流动→值越小。"""
    # 无 amount → None（透明降级）
    out = compute_alpha("X.SH", _bars([100 + i for i in range(40)]), [], [])
    assert out["liq_amihud_20d"] is None

    # 有 amount：21 根构造窗口，手算对账
    closes = [100 * (1 + 0.001 * i) for i in range(21)]
    amts = [1e9] * 21
    out = compute_alpha("X.SH", _bars(closes, amts), [], [])
    expect = sum(abs(closes[i] / closes[i - 1] - 1) / 1e9
                 for i in range(1, 21)) / 20 * 1e9
    assert out["liq_amihud_20d"] is not None
    assert abs(out["liq_amihud_20d"] - expect) < 1e-6
    # 成交额放大 100 倍 → 非流动性缩 100 倍（单调性）
    out2 = compute_alpha("X.SH", _bars(closes, [1e11] * 21), [], [])
    assert out2["liq_amihud_20d"] is not None
    assert out2["liq_amihud_20d"] < out["liq_amihud_20d"] / 50


def test_liq_roll() -> None:
    """Roll 隐含价差：Δr 负自协（均值回复振荡）→ 正价差；单边趋势（cov≥0）→ None。"""
    # 振荡序列：交替 ±2% → Δr 强负自协 → Roll > 0
    closes = [100.0]
    for i in range(66):
        closes.append(closes[-1] * (0.98 if i % 2 == 0 else 1 / 0.98))
    out = compute_alpha("X.SH", _bars(closes), [], [])
    assert (out["liq_roll_60d"] or 0) > 0

    # 单边二阶加速趋势（Δr 严格递增且量级 ~1e-4 远离浮点噪声 → cov 明显>0）
    # → None（估计无定义，透明降级）
    up = [100.0]
    for i in range(1, 71):
        up.append(up[-1] * (1 + 0.00002 * i * i))
    out2 = compute_alpha("X.SH", _bars(up), [], [])
    assert out2["liq_roll_60d"] is None


def test_liq_cs_spread() -> None:
    """CS 高低价价差：high=low（无日内价差）→ 非负；明显两日跨幅 → 更大价差。"""
    flat = [100.0] * 65
    o1 = compute_alpha("X.SH", _bars(flat, [1e9] * 65, flat, flat), [], [])
    assert (o1["liq_cs_60d"] or 0.0) >= 0.0

    # 高低幅 5% 的两日跨幅序列 → 价差显著大于无价差序列
    wide_h = [c * 1.05 for c in flat]
    wide_l = [c * 0.95 for c in flat]
    o2 = compute_alpha("X.SH", _bars(flat, [1e9] * 65, wide_h, wide_l), [], [])
    assert o2["liq_cs_60d"] is not None and o1["liq_cs_60d"] is not None
    assert o2["liq_cs_60d"] > o1["liq_cs_60d"]


# ── P1.3 换手率代理补测（compute_beta 成交额分位）──────────────────


def test_turnover_pct() -> None:
    """换手率代理 = 当日成交额的历史分位：递增额末值最大 → 1.0；递减额 → 低分位；无额 → None。"""
    # 递增成交额，末值即窗口最大 → 分位 ≈ (n-0.5)/n（并列 0.5 权重语义）
    n = 60
    closes = [100.0] * n                      # 价格恒定 → 不触发信号干扰
    amts = [float(1e8 + i * 1e7) for i in range(n)]
    out = compute_beta("X.SH", _bars(closes, amts))
    assert out["turnover_pct"] == (n - 0.5) / n

    # 末值为窗口最小 → 分位接近 0
    amts_down = [float(1e9 - i * 1e7) for i in range(n)]
    out2 = compute_beta("X.SH", _bars(closes, amts_down))
    assert (out2["turnover_pct"] or 1.0) < 0.01
