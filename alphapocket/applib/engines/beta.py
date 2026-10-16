"""引擎B 包装器（T-R2/T-R4）：20 日择时信号 + 港美 regime。

零公式：``run``/``run_hk_regime``/``signal_events`` 一行委托 factors.compute_beta /
compute_hk_regime / iter_signal_events；T-R4 起 registry 作为配置来源 ——
``defaults``（win/band/…）与 ``signals``（四信号启停/文案/cooldown）由注册表驱动，
改注册表即可调参/启停信号，无需改代码；``overrides`` 优先于 registry（调试用：
临时改参数/文案不动盘上注册表）。
"""
from __future__ import annotations

from .. import factors as F
from .base import BaseEngine


class BetaEngine(BaseEngine):
    id = "beta"
    name = "引擎B·短期择时"
    kind = "timing"

    def _config_from_registry(self) -> dict:
        """registry beta 节 → 计算入参（T-R4）。

        - ``defaults``：win/band/band_sigma/bias_thresh/vpd_thresh/pct_window
          （键 ⊆ BETA_DEFAULT_KEYS，校验器已把关）；
        - ``signal_specs``：``signals`` 节原文透传（factors._BETA_SIGNALS 运行时形状：
          {id, status, type, action, detail, evidence, cooldown}，detail 为 str.format
          模板，数值由 compute_beta 运行时注入）；触发条件/分支键是公式语义，不随注册表。
        """
        eng = self._eng
        cfg: dict = {}
        d = eng.get("defaults")
        if isinstance(d, dict) and d:
            cfg["defaults"] = dict(d)
        sigs = eng.get("signals")
        if isinstance(sigs, list) and sigs:
            cfg["signal_specs"] = [dict(s) for s in sigs if isinstance(s, dict)]
        return cfg

    # ── 查询面（T-R4 delay_decay 过滤 / T-R7 增删改 CLI 复用）──
    def signal_specs(self) -> list[dict]:
        """信号定义清单（registry 优先，否则内置 factors._BETA_SIGNALS）。"""
        return self._config_from_registry().get("signal_specs") or [dict(s) for s in F._BETA_SIGNALS]

    def active_signal_ids(self) -> set[str]:
        """status == active 的信号 id 集合（delay_decay 扫描面过滤用）。"""
        return {s["id"] for s in self.signal_specs() if s.get("status", "active") == "active"}

    def run(self, secid: str, bars: list[dict], as_of: str | None = None,
            overrides: dict | None = None, **kw) -> dict:
        """委托 factors.compute_beta：registry defaults/signals 注入 + overrides 覆盖。

        优先级 overrides > 显式 kw > registry > factors 内置默认；
        overrides/kw 中的 ``signal_specs`` 覆盖 registry signals（调试注入用）。
        """
        cfg = self._config_from_registry()
        merged = {**cfg.get("defaults", {}), **kw}
        if overrides:
            merged.update(overrides)
        specs = merged.pop("signal_specs", cfg.get("signal_specs"))
        return F.compute_beta(secid, bars, as_of, signal_specs=specs, **merged)

    def run_hk_regime(self, series: list[dict], as_of: str | None = None,
                      chg_win: int | None = None, overrides: dict | None = None) -> dict:
        """委托 factors.compute_hk_regime（H-0006）：registry ``macro.regime`` 阈值注入。

        优先级 overrides > 显式 chg_win > registry macro.regime > factors 常量默认。
        """
        rg = dict((((self.registry or {}).get("engines") or {}).get("macro") or {}).get("regime") or {})
        if chg_win is not None:
            rg["chg_win"] = chg_win
        if overrides:
            rg.update(overrides)
        return F.compute_hk_regime(series, as_of=as_of, **rg)

    def signal_events(self, secid: str, bars: list[dict], signal_id: str = "all",
                      horizon: int = 20, start: str | None = None,
                      end: str | None = None, signal_specs: list[dict] | None = None,
                      **kw) -> list[dict]:
        """委托 factors.iter_signal_events（R4 延迟衰减用；signal_specs 透传）。"""
        return F.iter_signal_events(secid, bars, signal_id, horizon,
                                    start=start, end=end, signal_specs=signal_specs, **kw)
