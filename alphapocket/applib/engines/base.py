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
    """

    def __init__(self, spec: EngineSpec, compute_fn: Callable):
        self.spec = spec
        self._fn = compute_fn          # 委托目标（factors.compute_x，原位不动）

    def __call__(self, args: dict) -> dict:
        t0 = time.monotonic()
        err = validate_input(self.spec, dict(args))
        if err:
            return {"status": "invalid_input", "error": err,
                    "_engine": self._prov(t0, status="invalid_input")}
        out = self._fn(**dict(args))   # 委托（契约 1：公式体只存在于 factors.py）
        out = dict(out) if isinstance(out, dict) else {"value": out}
        warns = check_output_range(self.spec, out)
        if warns:
            out["_range_warnings"] = warns     # 显式告警不静默截断（契约 3）
        out["_engine"] = self._prov(t0, status="ok")
        return out

    def _prov(self, t0: float, status: str) -> dict:
        return {"engine_id": self.spec.engine_id, "version": self.spec.version,
                "status": status, "elapsed_ms": round((time.monotonic() - t0) * 1000, 2)}
