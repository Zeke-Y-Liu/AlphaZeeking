"""引擎D 包装器（2026-10-26 新 Goal）：指数级别趋势（位置 + 趋势判断）。

零公式：``run`` 委托 ``factors.compute_index_trend``（纯函数真源）；
registry ``engines.index_trend`` 节为指数清单/regime 参数/启停的配置来源——
改注册表即可增删指数（第一批：纳斯达克综合/沪深300/恒生科技/创业板指），
无需改代码；``overrides`` 优先于 registry（调试用，同引擎C 语义）。

纪律（同引擎C）：市场级条件变量——不决策、不产生信号、不参与权重；
失败透明降级（调用方 portfolio_review 捕获 → unavailable，不阻断评估）。
"""
from __future__ import annotations

from .. import factors as F
from .base import BaseEngine


class IndexTrendEngine(BaseEngine):
    id = "index_trend"
    name = "引擎D·指数趋势"
    kind = "index_regime"

    def _config_from_registry(self) -> dict:
        """registry index_trend 节 → compute_index_trend 的配置注入。

        - ``active_ids``：metrics 中 status == active 的指数白名单
          （inactive/experimental 视同停用：登记行情、不产出判断）；
        - ``regime``：quantile_win/ma_fast/ma_slow/pos_high/pos_low 参数
          （键集受 validate_registry 约束；缺键沿用 factors 内置默认）。
        """
        eng = self._eng
        active_ids = {m["id"] for m in eng.get("metrics", [])
                      if isinstance(m, dict) and not m.get("orphan")
                      and m.get("status", "active") == "active"}
        cfg: dict = {"active_ids": active_ids or None}
        rg = eng.get("regime")
        if isinstance(rg, dict):
            cfg["regime"] = dict(rg)
        return cfg

    def run(self, bars: dict, as_of: str | None = None,
            meta: dict | None = None, overrides: dict | None = None) -> dict:
        """委托 factors.compute_index_trend：registry 配置注入 + overrides 覆盖。

        overrides 支持 active_ids 与 regime 内参数（quantile_win 等），
        优先级 overrides > registry > 内置默认。
        """
        cfg = self._config_from_registry()
        rg = {k: v for k, v in (cfg.pop("regime", None) or {}).items()}
        if overrides:
            ov = dict(overrides)
            rg.update(ov.pop("regime", {}) or {})
            cfg.update(ov)
        # regime 键 → compute_index_trend 具名参数（未知键忽略，键集受校验器约束）
        kwargs = {k: rg[k] for k in
                  ("quantile_win", "ma_fast", "ma_slow", "pos_high", "pos_low") if k in rg}
        return F.compute_index_trend(bars, as_of=as_of, meta=meta,
                                      active_ids=cfg.get("active_ids"), **kwargs)
