"""engines 配置注入通道（组件化 T-R3）：JSON 配置 → 包装器运行时注入。

配置形态（文件或 dict，均 ``{engine_id: {params, enabled, registry_version}}``）::

    {
      "engine_b_beta": {"params": {"win": 10, "band": 0.03},
                        "enabled": true, "registry_version": "v1"},
      "engine_c_macro": {"enabled": false}
    }

注入顺序（T-R3 红线：注入不得绕过包装层契约）：显式 args > config.params >
compute_x 函数签名默认。版本不符 → ``_version_warning`` 显式登记不静默不阻断
（处置口径见 RUNBOOK §10）；引擎无硬编码参数——参数一律走注入通道或函数签名默认。
"""
from __future__ import annotations

import json
from pathlib import Path

from .base import ENGINE_REGISTRY_VERSION, EngineWrapper

__all__ = ["load_engine_config", "engine_config_for", "ENGINE_REGISTRY_VERSION"]


def load_engine_config(path: str | Path | None) -> dict:
    """读引擎配置 JSON → dict；缺文件/空 → {}（各包装器走默认并自带 _config_warnings）。"""
    if path is None:
        return {}
    p = Path(path)
    if not p.exists():
        return {}
    data = json.loads(p.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError(f"引擎配置须为 {{engine_id: ...}} 形态：{p}")
    return {k: (v if isinstance(v, dict) else {}) for k, v in data.items()}


def engine_config_for(cfg: dict, eng: EngineWrapper) -> dict:
    """取某引擎的配置条目；缺条目 → {}（走默认 + _config_warnings，不静默）。"""
    entry = cfg.get(eng.spec.engine_id)
    if entry is None:
        return {}
    if not isinstance(entry, dict):
        raise ValueError(f"引擎配置条目须为 dict：{eng.spec.engine_id}")
    return entry
