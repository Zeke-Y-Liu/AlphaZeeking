"""engines/ 包装层基类（组件化 T-R1 骨架 · 契约冻结）。

架构决策（为什么不搬家，全文见 任务拆分-组件化重构/T-R1-现状盘点与架构决策.md）：
factors.compute_x 纯函数已被单测精确数值断言 + seed 快照双重锚定，是可复现资产；
搬家 = 回归面爆炸。包装层只做契约，公式原位。

契约五条（冻结，修订须登记）：
1. **委托**：包装器不得复制公式体，只委托 factors.compute_x（grep 红线 T-R2）
2. **入参校验**：required 键 + 类型校验，失败在包装层拦截（不进计算函数）
3. **输出位范围**：range_fields 声明的数值字段越界 → ``_range_warnings`` 收集
   （显式告警不静默截断、不阻断产出）
4. **无状态**：包装器实例零可变状态（同实例串行调用互不影响，可独立单测）
5. **provenance**：每次调用登记 engine_id / version / ts / 耗时
"""
from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any, Callable

# 引擎注册表版本（T-R3 版本绑定锚）：引擎集/契约变更时 bump（M1 注册中心接管后由 registry 提供）
ENGINE_REGISTRY_VERSION = "v1"


@dataclass(frozen=True)
class EngineSpec:
    """引擎包装契约（冻结）：id / 版本 / 入参 schema / 输出位范围。

    ``enum_fields``（T-R2 契约扩展：新增可选字段，不推翻五条）——字符串枚举位域
    （如引擎B ``state ∈ {above,below,neutral}``），与数值 ``output_range`` 并列。
    """

    engine_id: str
    version: str
    # 入参 schema：{形参名: 类型或类型元组}；None 表示不限类型（仅 required 检查）
    input_schema: dict[str, Any]
    required: tuple[str, ...] = ()
    # 输出位范围：仅约束 range_fields 列出的数值字段（如引擎C 五档 -2..+2）
    output_range: tuple[float, float] | None = None
    range_fields: tuple[str, ...] = ()
    # 字符串枚举位域：{字段: 允许取值}（T-R2 扩展）
    enum_fields: dict[str, tuple[str, ...]] | None = None
    note: str = ""


def validate_input(spec: EngineSpec, args: dict) -> str | None:
    """入参校验（契约 2）：缺参 / 未声明形参 / 类型不符 → 返回错误文本；通过返回 None。"""
    missing = [k for k in spec.required if args.get(k) is None]
    if missing:
        return f"{spec.engine_id}: 缺少必填入参 {missing}"
    unknown = [k for k in args if k not in spec.input_schema]
    if unknown:
        return f"{spec.engine_id}: 未声明入参 {unknown}（schema={sorted(spec.input_schema)}）"
    for k, want in spec.input_schema.items():
        if want is None or args.get(k) is None:
            continue
        if not isinstance(args[k], want):
            return f"{spec.engine_id}: 入参 {k} 类型须为 {want}，实得 {type(args[k]).__name__}"
    return None


def check_output_range(spec: EngineSpec, out: Any) -> list[str]:
    """输出位范围检查（契约 3）：数值越界 / 枚举出域 → 收集告警文本；不截断、不抛异常。"""
    if not isinstance(out, dict):
        return []
    warns = []
    if spec.output_range is not None:
        low, high = spec.output_range
        for f in spec.range_fields:
            v = out.get(f)
            if isinstance(v, (int, float)) and not isinstance(v, bool):
                if not (low <= v <= high):
                    warns.append(f"{spec.engine_id}.{f}={v} 越界 [{low},{high}]（告警不截断）")
    for f, allowed in (spec.enum_fields or {}).items():
        v = out.get(f)
        if isinstance(v, str) and v not in allowed:
            warns.append(f"{spec.engine_id}.{f}={v!r} 出域（枚举 {allowed}）（告警不截断）")
    return warns


class EngineWrapper:
    """委托-校验-位约束包装器（契约 1/2/3/4/5）。

    用法::

        alpha_engine = EngineWrapper(EngineSpec(...), F.compute_alpha)
        out = alpha_engine({"secid": ..., "bars": [...], ...})
        out = alpha_engine({"secid": ...}, config={"params": {"win": 10}})   # T-R3 注入

    配置注入（T-R3，经包装层不绕过契约）：``config = {params, enabled,
    registry_version}``——params 作为入参默认值层（显式 args 优先）；enabled=False
    → ``status=disabled`` 不进公式；registry_version 与 ``ENGINE_REGISTRY_VERSION``
    核对，不符 → ``_version_warning`` 显式登记（不静默、不阻断产出）。
    """

    def __init__(self, spec: EngineSpec, compute_fn: Callable):
        self.spec = spec
        self._fn = compute_fn          # 委托目标（factors.compute_x，原位不动）

    def __call__(self, args: dict, config: dict | None = None) -> dict:
        t0 = time.monotonic()
        cfg = config or {}
        if cfg.get("enabled") is False:
            return {"status": "disabled",
                    "reason": f"{self.spec.engine_id} 已由引擎配置停用（enabled=false）",
                    "_engine": self._prov(t0, status="disabled")}
        merged = {**(cfg.get("params") or {}), **dict(args)}   # 显式入参 > 配置默认
        warn_cfg = (None if cfg.get("params")
                    else [f"{self.spec.engine_id}: 未显式配置参数，走函数签名默认（T-R3 注入通道）"])
        err = validate_input(self.spec, merged)
        if err:
            return {"status": "invalid_input", "error": err,
                    "_engine": self._prov(t0, status="invalid_input")}
        out = self._fn(**merged)       # 委托（契约 1：公式体只存在于 factors.py）
        out = dict(out) if isinstance(out, dict) else {"value": out}
        warns = check_output_range(self.spec, out)
        if warns:
            out["_range_warnings"] = warns     # 显式告警不静默截断（契约 3）
        if cfg.get("registry_version") and cfg["registry_version"] != ENGINE_REGISTRY_VERSION:
            out["_version_warning"] = (        # 版本不符：显式登记不静默、不阻断（T-R3）
                f"{self.spec.engine_id}: 配置 registry_version={cfg['registry_version']!r} "
                f"≠ 注册表 {ENGINE_REGISTRY_VERSION!r}（结果照常产出；处置见 RUNBOOK §10）")
        if warn_cfg:
            out["_config_warnings"] = warn_cfg
        out["_engine"] = self._prov(t0, status="ok")
        return out

    def _prov(self, t0: float, status: str) -> dict:
        return {"engine_id": self.spec.engine_id, "version": self.spec.version,
                "status": status, "elapsed_ms": round((time.monotonic() - t0) * 1000, 2)}
