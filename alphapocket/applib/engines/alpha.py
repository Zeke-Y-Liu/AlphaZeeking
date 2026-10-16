"""引擎A 包装器（T-R2/T-R5）：横截面选股/估值质量因子。

零公式：``run`` 一行委托 ``factors.compute_alpha``（公式真源）；T-R5 起 registry 作为
配置来源 —— ``dim_weights``（五维权重）/ ``anchors``（评分锚点）/ metrics 启停白名单
由注册表驱动，改注册表即可调权重/换锚点/启停因子，无需改代码；``overrides`` 优先于
registry（调试用：临时改参数不动盘上注册表）。
"""
from __future__ import annotations

from .. import factors as F
from .base import ALPHA_ANCHOR_KEYS, BaseEngine


class AlphaEngine(BaseEngine):
    id = "alpha"
    name = "引擎A·横截面因子"
    kind = "cross_section"

    def _config_from_registry(self) -> dict:
        """registry alpha 节 → compute_alpha 的配置注入（T-R5）。

        - ``weights``：dim_weights 节（键 = 五维，校验器已把关；缺 → factors 内置默认）；
        - ``anchors``：anchors 节（键 ⊆ ALPHA_ANCHOR_KEYS），与 factors._ALPHA_ANCHORS
          同构的 list 形状；compute_alpha 按键合并——只覆盖出现的组，s_val 无锚点表
          （公式 100−分位×100），注册表即便登记也不生效；
        - ``active_ids``：status == active 白名单（experimental 视同停用）。默认注册表
          19 因子全 active → 全覆盖集合不产生 flags 键，输出与直调逐位一致（锚点纪律）。
        """
        eng = self._eng
        cfg: dict = {}
        w = eng.get("dim_weights")
        if isinstance(w, dict) and w:
            cfg["weights"] = dict(w)
        a = eng.get("anchors")
        if isinstance(a, dict) and a:
            cfg["anchors"] = {k: [list(p) for p in v] for k, v in a.items()
                              if k in ALPHA_ANCHOR_KEYS and isinstance(v, list)}
        act = {m["id"] for m in eng.get("metrics", [])
               if isinstance(m, dict) and m.get("status", "active") == "active"}
        if act:
            cfg["active_ids"] = act
        return cfg

    # ── 查询面（T-R5 sensitivity 权重变体 / T-R7 增删改 CLI 复用）──
    def weights(self) -> dict:
        """生效五维权重（registry dim_weights 优先，否则内置 DEFAULT_DIM_WEIGHTS）。"""
        w = self._config_from_registry().get("weights")
        return w or dict(F.DEFAULT_DIM_WEIGHTS)

    def run(self, secid: str, bars: list[dict], reports: list[dict], dividends: list[dict],
            as_of: str | None = None, overrides: dict | None = None, **kw) -> dict:
        """委托 factors.compute_alpha：registry weights/anchors/active_ids 注入 + 覆盖。

        优先级 overrides > 显式 kw > registry > factors 内置默认；
        overrides/kw 中的 ``weights``（五维权重 dict）/ ``anchors``（锚点组 dict）/
        ``active_ids``（启停白名单 set）均可临时覆盖 registry（调试注入用）。
        """
        cfg = self._config_from_registry()
        merged = {**cfg, **kw}
        if overrides:
            merged.update(overrides)
        return F.compute_alpha(secid, bars, reports, dividends, as_of=as_of, **merged)
