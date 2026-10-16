"""M1 变换链测试：winsorize_mad / zscore / rank_pct / industry 中性化 + 链式执行。

运行：``cd alphapocket && python -m pytest tests/test_factorlib_transform.py -q``
"""
from __future__ import annotations

import pytest

from factorlib.transform import (TransformError, apply_chain, industry_demean,
                                 rank_pct, winsorize_mad, zscore)

K_SCALE = 3.0 * 1.4826


# ── winsorize_mad ─────────────────────────────────────
def test_winsorize_clips_outlier():
    xs = {"a": 1.0, "b": 2.0, "c": 3.0, "d": 4.0, "e": 100.0}
    out = winsorize_mad(xs, k=3.0)
    # med=3, MAD=1 → 上下界 3 ± 3·1.4826 → 100 被截断，其余不动
    assert out["e"] == pytest.approx(3.0 + K_SCALE)
    for k_ in "abcd":
        assert out[k_] == xs[k_]


def test_winsorize_zero_mad_noop():
    xs = {"a": 1.0, "b": 1.0, "c": 1.0, "d": 1.0, "e": 9.0}
    assert winsorize_mad(xs) == xs          # MAD=0：不截断（避免整截面坍缩）


def test_winsorize_small_section_noop():
    xs = {"a": 1.0, "b": 100.0}
    assert winsorize_mad(xs) == xs          # 样本 < 3：不做截断


# ── zscore ────────────────────────────────────────────
def test_zscore_known_values():
    out = zscore({"a": 1.0, "b": 2.0, "c": 3.0})
    assert out == {"a": pytest.approx(-1.0), "b": pytest.approx(0.0), "c": pytest.approx(1.0)}


def test_zscore_constant_section():
    assert zscore({"a": 5.0, "b": 5.0}) == {"a": 0.0, "b": 0.0}


def test_zscore_single_name():
    assert zscore({"a": 5.0}) == {"a": 0.0}


# ── rank_pct ──────────────────────────────────────────
def test_rank_pct_ties_average_rank():
    out = rank_pct({"a": 1.0, "b": 2.0, "c": 2.0, "d": 3.0})
    assert out["a"] == pytest.approx(0.25)          # 秩1/4
    assert out["b"] == out["c"] == pytest.approx(0.625)   # 平均秩 2.5/4
    assert out["d"] == pytest.approx(1.0)


# ── industry 中性化 ───────────────────────────────────
def test_industry_demean_group_means_zero():
    xs = {"a": 1.0, "b": 3.0, "c": 10.0, "d": 20.0}
    ind = {"a": "X", "b": "X", "c": "Y", "d": "Y"}
    out = industry_demean(xs, ind)
    assert out["a"] == pytest.approx(-1.0) and out["b"] == pytest.approx(1.0)
    assert out["c"] == pytest.approx(-5.0) and out["d"] == pytest.approx(5.0)


def test_industry_demean_missing_industry_bucket():
    xs = {"a": 1.0, "b": 3.0, "z": 100.0}
    ind = {"a": "X", "b": "X"}                       # z 无行业 → 独立桶（均值=自身→0）
    out = industry_demean(xs, ind)
    assert out["z"] == pytest.approx(0.0)
    assert out["a"] == pytest.approx(-1.0)


# ── None 透传 ─────────────────────────────────────────
def test_none_passthrough_all_steps():
    xs = {"a": 1.0, "b": None, "c": 3.0}
    for fn in (winsorize_mad, zscore, rank_pct):
        assert fn(dict(xs))["b"] is None
    assert industry_demean(dict(xs), {"a": "X", "c": "X"})["b"] is None
    chained = apply_chain(xs, ["winsorize_mad", "zscore"])
    assert chained["b"] is None


# ── 链式执行 ──────────────────────────────────────────
def test_apply_chain_winsorize_then_zscore():
    xs = {k: float(v) for k, v in zip("abcde", (1, 2, 3, 4, 100))}
    out = apply_chain(xs, ["winsorize_mad", "zscore"])
    vals = [v for v in out.values()]
    assert abs(sum(vals)) < 1e-9                     # 标准化后均值 0
    assert max(vals) < 3.0                           # 截断后无极端 z 值


def test_apply_chain_neutralization_runs_last():
    xs = {"a": 1.0, "b": 3.0, "c": 2.0}
    ind = {"a": "X", "b": "X", "c": "Y"}
    out = apply_chain(xs, ["rank_pct"], ["industry"], ind)
    # rank 后 (0.333, 1.0, 0.667)；行业内去均值：X 组 0.333-0.667=-0.333 / +0.333；Y 组 0
    assert out["a"] == pytest.approx(-1 / 3)
    assert out["b"] == pytest.approx(1 / 3)
    assert out["c"] == pytest.approx(0.0)


def test_apply_chain_unknown_step_raises():
    with pytest.raises(TransformError, match="未知变换步"):
        apply_chain({"a": 1.0}, ["winsorize_mad", "quantile_99"])


def test_apply_chain_industry_requires_map():
    with pytest.raises(TransformError, match="industries"):
        apply_chain({"a": 1.0}, [], ["industry"], None)
