"""三引擎包装器测试（T-R2）：委托 / 入参校验 / 位约束三面 + grep 公式不搬家红线。"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from applib import factors as F
from applib.engines import (
    BETA_SPEC,
    MACRO_SPEC,
    alpha_engine,
    beta_engine,
    check_output_range,
    macro_engine,
)

ENGINES_DIR = Path(__file__).resolve().parent.parent / "applib" / "engines"


def _bars(n=30, base=10.0):
    """线性上行 K 线（dt 递增、close 单调升）→ state=above / mom 为正。"""
    return [{"dt": f"2024-{1 + i // 28:02d}-{1 + i % 28:02d}", "open": base + i * 0.1,
             "high": base + i * 0.1 + 0.2, "low": base + i * 0.1 - 0.1,
             "close": round(base + i * 0.1, 3), "volume": 1000 + i, "amount": 1e5 + i}
            for i in range(n)]


def _reports():
    return [{"period": "2023-12-31", "announce_date": "2024-03-30", "eps_ytd": 1.0,
             "bps": 10.0, "roe": 12.0, "roe_w": 12.0, "rev_g": 8.0, "np_g": 10.0,
             "margin": 30.0}]


# ── 契约 1 · 委托：包装器绑定 compute_x 本体（identity），公式体零复制 ──
def test_delegation_identity_and_grep_no_formula_copy():
    assert alpha_engine._fn is F.compute_alpha
    assert beta_engine._fn is F.compute_beta
    assert macro_engine._fn is F.compute_macro_liquidity

    # grep 红线（自动化）：engines/ 源码不含公式体特征片段（公式只存在于 factors.py）
    formulas = ["cs[-21]", "cs[-252]", "sum(w) / n", "ttm_eps(", "industry_neutralize(",
                "dim_scores", "_macro_scorer", "shrzgm", "pe_s.append", "hist_c[i - 1]"]
    for p in ENGINES_DIR.glob("*.py"):
        src = p.read_text(encoding="utf-8")
        for frag in formulas:
            assert frag not in src, f"公式复制嫌疑：{p.name} 含 {frag!r}"


# ── 契约 2 · 入参校验：schema 失败在包装层拦截（不进计算函数） ──────────
def test_input_validation_blocks_before_compute():
    calls = []
    for eng in (alpha_engine, beta_engine, macro_engine):
        origin = eng._fn
        eng._fn = lambda *a, **k: (calls.append(1), origin(**k))[1]   # noqa: B023 - spy 当轮消费；见下 try 块
        try:
            out = eng({})                                  # 缺全部 required
            assert out["status"] == "invalid_input" and "缺少必填" in out["error"]
            out = eng({"secid": "s", "bars": [], "reports": [], "dividends": [],
                       "indicators": {}, "as_of": "2024-01-01", "win": "x"})  # 类型错（win 须 int）
            assert out["status"] == "invalid_input"
        finally:
            eng._fn = origin                               # 还原（测试不污染其它用例）
    assert calls == []                                     # 公式从未被调用


# ── 契约 3 · 位约束：真实公式输出必在位内（性质测试） ──────────────────
def test_real_output_within_range():
    a = alpha_engine({"secid": "600519.SH", "bars": _bars(260), "reports": _reports(),
                      "dividends": [], "as_of": "2024-01-28"})
    assert a["_engine"]["status"] == "ok" and "_range_warnings" not in a
    assert a["composite"] is None or 0 <= a["composite"] <= 100

    b = beta_engine({"secid": "600519.SH", "bars": _bars(30), "as_of": "2024-01-29"})
    assert b["state"] in BETA_SPEC.enum_fields["state"]
    assert "_range_warnings" not in b

    ind = {"shibor_1w": {"id": "shibor_1w", "market": "A", "dim": "D3", "direction": -1,
                         "series": [{"dt": f"2024-01-{d:02d}", "value": 1.8 + d * 0.01}
                                    for d in range(1, 11)]}}
    c = macro_engine({"indicators": ind, "as_of": "2024-01-31"})
    lo, hi = MACRO_SPEC.output_range
    for f in ("global", "global_score"):
        # 位内性质：数值档在 [-2,+2]；全缺指标 → "unavailable"/None（契约降级形态）
        assert c[f] in ("unavailable", None) or (
            isinstance(c[f], (int, float)) and lo <= c[f] <= hi)
    assert c["markets"]["A"] == "unavailable" or lo <= c["markets"]["A"] <= hi
    assert "_range_warnings" not in c


# ── 越界告警不截断（包装层行为，引擎C 位约束语义） ─────────────────────
def test_out_of_range_warns_not_truncates():
    from applib.engines.base import EngineSpec
    spec = EngineSpec(engine_id="x", version="v", input_schema={},
                      output_range=(-2, 2), range_fields=("global",),
                      enum_fields={"state": ("above", "below")})
    warns = check_output_range(spec, {"global": 3, "state": "sideways"})
    assert len(warns) == 2 and all("不截断" in w for w in warns)
