"""引擎C 包装器（T-R2/T-R3）：宏观流动性三层聚合。

零公式：``run`` 委托 ``factors.compute_macro_liquidity``（T-C2 纯函数）；
T-R3 起 registry 作为维度归属/打分器/维度权重/启停的配置来源 —— 改注册表即可增删改
宏观指标，无需改代码；``overrides`` 优先于 registry（调试用：临时改参数不动盘上注册表）。
"""
from __future__ import annotations

from .. import factors as F
from .base import MACRO_DIMS, MACRO_MARKETS, BaseEngine


class MacroEngine(BaseEngine):
    id = "macro"
    name = "引擎C·宏观流动性"
    kind = "macro_regime"

    def _config_from_registry(self) -> dict:
        """registry macro 节 → compute_macro_liquidity 的配置注入（T-R3）。

        - ``dim_of``：metrics（market+dim 非空）按 A/HK/US × D1..D4 **规范序**装配，
          与 factors._MACRO_DIM_OF 同构 —— 默认注册表输出与直调逐位一致（对拍红线，
          维度/市场求和顺序敏感）；dim=None 的背景指标（us_cpi_yoy）不进维度，
          由 factors 侧既有特例登记进 indicators；
        - ``scorers``：指标 → (type, arg)，同 factors._MACRO_SCORERS 形状；
          scorer 未登记的指标沿用 factors 内置 ("clip", 1.0) 兜底（背景指标语义）；
        - ``active_ids``：status == active 白名单（experimental 视同停用）；
        - ``dim_weights``：registry 缺 D 键时由聚合层按剩余权重重归一化（现语义）。
        """
        eng = self._eng
        raw: dict[str, dict[str, list[str]]] = {}
        scorers: dict[str, tuple] = {}
        active_ids: set[str] = set()
        for m in eng.get("metrics", []):
            if m.get("orphan"):
                continue                                 # T-R6：orphan 条目不入运行时面
            mkt, dim = m.get("market"), m.get("dim")
            if mkt in MACRO_MARKETS and dim in MACRO_DIMS:
                raw.setdefault(mkt, {}).setdefault(dim, []).append(m["id"])
            if m.get("scorer"):
                scorers[m["id"]] = (m["scorer"].get("type"), m["scorer"].get("arg"))
            if m.get("status", "active") == "active":
                active_ids.add(m["id"])
        dim_of = {mkt: {d: raw[mkt][d] for d in MACRO_DIMS if d in raw[mkt]}
                  for mkt in MACRO_MARKETS if mkt in raw}
        cfg: dict = {"dim_of": dim_of, "scorers": scorers, "active_ids": active_ids}
        dw = eng.get("dim_weights")
        if dw:
            cfg["dim_weights"] = dict(dw)
        return cfg

    def run(self, series: dict, hk_series: list | None = None, as_of: str | None = None,
            quantile_win: int = 252, market_weights: dict | None = None,
            meta: dict | None = None, overrides: dict | None = None) -> dict:
        """委托 factors.compute_macro_liquidity：registry 配置注入 + overrides 覆盖。

        overrides 与 _config_from_registry 同键（dim_of/scorers/dim_weights/active_ids），
        亦可透传 quantile_win 等其余入参；优先级 overrides > registry > 内置默认。
        """
        cfg = self._config_from_registry()
        if overrides:
            cfg.update(overrides)
        return F.compute_macro_liquidity(series, hk_series, as_of=as_of,
                                         quantile_win=quantile_win,
                                         market_weights=market_weights, meta=meta, **cfg)
