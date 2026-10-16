"""横截面变换链（架构文档 §16「transform」步 + 计划 M1.3）。

对单个交易日的截面值 {secid: value} 依序施加变换；缺失值（None）原样保留。
变换均为「同一 dt 截面内的相对化」，不跨日、不引用未来信息 → PIT 天然安全。

- winsorize_mad  中位数绝对偏差截断（x → clip 至 med ± k·1.4826·MAD；MAD=0 时不动作）
- zscore         标准化（样本标准差；截面无离散度 → 全 0.0）
- rank_pct       截面百分位排名（并列取平均秩，值域 (0,1]）
- industry       行业中性化（行业内去均值；缺行业归属的 secid 归入同一桶）

链式执行顺序 = meta.transform + meta.neutralization（中性化恒在最后）。
未知步骤名抛 TransformError —— 定义错误在物化前暴露，而不是静默跳过。
"""
from __future__ import annotations

import math


class TransformError(Exception):
    """变换链配置/执行违例。"""


def _valid(xs: dict) -> list[float]:
    return [v for v in xs.values() if v is not None]


def winsorize_mad(xs: dict, k: float = 3.0) -> dict:
    vals = _valid(xs)
    if len(vals) < 3:
        return dict(xs)
    s = sorted(vals)
    med = s[len(s) // 2] if len(s) % 2 else (s[len(s) // 2 - 1] + s[len(s) // 2]) / 2
    mad = sorted(abs(v - med) for v in vals)
    mad = mad[len(mad) // 2] if len(mad) % 2 else (mad[len(mad) // 2 - 1] + mad[len(mad) // 2]) / 2
    if mad == 0:      # 过半样本与中位数重合：无法估计尺度，不做截断（避免整截面坍缩到 med）
        return dict(xs)
    lo, hi = med - k * 1.4826 * mad, med + k * 1.4826 * mad
    return {key: None if v is None else min(max(v, lo), hi) for key, v in xs.items()}


def zscore(xs: dict) -> dict:
    vals = _valid(xs)
    n = len(vals)
    if n == 0:
        return dict(xs)
    mean = sum(vals) / n
    if n < 2:
        return {key: None if v is None else 0.0 for key, v in xs.items()}
    var = sum((v - mean) ** 2 for v in vals) / (n - 1)
    std = math.sqrt(var)
    if std == 0:      # 无离散度：截面无相对信息，统一置 0
        return {key: None if v is None else 0.0 for key, v in xs.items()}
    return {key: None if v is None else (v - mean) / std for key, v in xs.items()}


def rank_pct(xs: dict) -> dict:
    vals = [v for v in xs.values() if v is not None]
    n = len(vals)
    if n == 0:
        return dict(xs)
    s = sorted(vals)
    # 并列取平均秩（1-based），百分位 = 平均秩 / n
    pos: dict[float, float] = {}
    i = 0
    while i < n:
        j = i
        while j + 1 < n and s[j + 1] == s[i]:
            j += 1
        avg_rank = (i + j + 2) / 2          # 秩 1..n 的平均
        pos[s[i]] = avg_rank / n
        i = j + 1
    return {key: None if v is None else pos[v] for key, v in xs.items()}


def industry_demean(xs: dict, industries: dict) -> dict:
    groups: dict[str, list] = {}
    for key, v in xs.items():
        if v is None:
            continue
        groups.setdefault(industries.get(key) or "__none__", []).append(key)
    means = {g: sum(xs[key] for key in keys) / len(keys) for g, keys in groups.items()}
    return {key: None if xs[key] is None else xs[key] - means.get(industries.get(key) or "__none__", 0.0)
            for key in xs}


_KNOWN_TRANSFORMS = ("winsorize_mad", "zscore", "rank_pct")
_KNOWN_NEUTRALIZATIONS = ("industry",)


def apply_chain(xs: dict, steps=(), neutralization=(), industries: dict | None = None) -> dict:
    """依序执行变换链；neutralization 恒在 transform 之后。"""
    out = dict(xs)
    for name in list(steps) + list(neutralization):
        if name == "winsorize_mad":
            out = winsorize_mad(out)
        elif name == "zscore":
            out = zscore(out)
        elif name == "rank_pct":
            out = rank_pct(out)
        elif name == "industry":
            if industries is None:
                raise TransformError("industry 中性化需要 industries 映射（fl_dim_security）")
            out = industry_demean(out, industries)
        else:
            raise TransformError(f"未知变换步：{name!r}（transform 允许 {_KNOWN_TRANSFORMS}，"
                                 f"neutralization 允许 {_KNOWN_NEUTRALIZATIONS}）")
    return out
