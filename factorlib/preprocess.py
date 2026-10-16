"""截面预处理链（实施计划 §4 任务 1.3）：winsorize → 中性化（可选）→ zscore/rank。

链式配置于 YAML ``transform`` 字段（顺序即执行序）::

    transform:
      - winsorize: {q: 0.01}
      - neutralize: {by: industry}     # 行业 demean（市值中性化 M2 起随市值表提供）
      - zscore: {}
      # 或 rank: {pct: true}

全部为截面运算：输入 DataFrame 须含 value 列（单日截面或 dt+value 长表按 dt 分组）。
"""
from __future__ import annotations

import pandas as pd

TRANSFORM_NAMES = ("winsorize", "neutralize", "zscore", "rank")


def winsorize(s: pd.Series, q: float = 0.01) -> pd.Series:
    """分位截断：低于 q 分位/高于 1-q 分位的值截到分位值（不删除样本）。"""
    if not 0 <= q < 0.5:
        raise ValueError(f"winsorize 分位 q 须 ∈ [0, 0.5)，实得 {q}")
    lo, hi = s.quantile(q), s.quantile(1 - q)
    return s.clip(lo, hi)


def zscore(s: pd.Series) -> pd.Series:
    """截面标准化（均值 0 / 标准差 1；std=0 时全 NaN，不除零）。"""
    std = s.std(ddof=1)
    if not std or pd.isna(std):
        return s * float("nan")
    return (s - s.mean()) / std


def rank(s: pd.Series, pct: bool = True) -> pd.Series:
    """截面秩（并列取平均秩；pct=True 输出 [0,1] 分位）。"""
    return s.rank(pct=pct)


def neutralize(df: pd.DataFrame, by: str = "industry", value_col: str = "value") -> pd.DataFrame:
    """组内去均值：按 by 列（industry 等）分组 demean。

    缺组标签的样本 → value 置 NaN（显式暴露，不静默跳过）。
    """
    if by not in df.columns:
        raise ValueError(f"中性化分组列缺失：{by}")
    out = df.copy()
    has_group = out[by].notna()
    out.loc[~has_group, value_col] = float("nan")
    grp_mean = out.loc[has_group].groupby(by)[value_col].transform("mean")
    out.loc[has_group, value_col] = out.loc[has_group, value_col] - grp_mean
    return out


def apply_transform(df: pd.DataFrame, transform: list[dict], value_col: str = "value") -> pd.DataFrame:
    """按 YAML transform 链执行（未知步骤显式报错；inplace 语义返回新 df）。"""
    out = df.copy()
    for step in transform or []:
        if not isinstance(step, dict) or len(step) != 1:
            raise ValueError(f"transform 步骤须为单键映射，实得 {step!r}")
        (name, cfg), = step.items()
        if name not in TRANSFORM_NAMES:
            raise ValueError(f"未知 transform：{name}（可选 {TRANSFORM_NAMES}）")
        cfg = cfg or {}
        if name == "winsorize":
            out[value_col] = winsorize(out[value_col], q=float(cfg.get("q", 0.01)))
        elif name == "neutralize":
            out = neutralize(out, by=str(cfg.get("by", "industry")), value_col=value_col)
        elif name == "zscore":
            out[value_col] = zscore(out[value_col])
        elif name == "rank":
            out[value_col] = rank(out[value_col], pct=bool(cfg.get("pct", True)))
    return out
