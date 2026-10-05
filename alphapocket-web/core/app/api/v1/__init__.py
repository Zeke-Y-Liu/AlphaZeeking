"""API v1 路由聚合点（目录自动发现）。

**约定（所有 core 任务必须遵守）**：
- 在 ``app/api/v1/<name>.py`` 中定义模块级 ``router = APIRouter()``；
- **不要修改本文件**——新模块创建文件即自动生效，无需注册。

发现规则：按文件名字典序导入本目录下所有非下划线开头的 ``*.py``，
并 ``include_router`` 其模块级 ``router``。缺失模块（尚未交付）静默跳过，
因此并行开发期间互不阻塞；模块存在但依赖缺失或未定义 ``router`` 则立即报错（fail loud）。

（T0.3 协调方决策：替代原「各任务手工 include_router」方式，避免后续十余个
core 任务对同一聚合文件产生并行写冲突。）
"""
from importlib import import_module
from pathlib import Path

from fastapi import APIRouter

api_v1_router = APIRouter(prefix="/api/v1")

_PKG_DIR = Path(__file__).resolve().parent
_SKIP = {"__init__"}


def _discover_routers() -> None:
    for path in sorted(_PKG_DIR.glob("*.py")):
        stem = path.stem
        if stem in _SKIP or stem.startswith("_"):
            continue
        mod = import_module(f"{__name__}.{stem}")
        router = getattr(mod, "router", None)
        if router is None:
            raise RuntimeError(
                f"{__name__}.{stem} 未定义模块级 ``router``（路由聚合约定见本文件 docstring）"
            )
        api_v1_router.include_router(router)


_discover_routers()


@api_v1_router.get("/_ping", tags=["health"])
def ping() -> dict[str, str]:
    """前缀挂载自检端点（仅用于验证 /api/v1 挂载正确）。"""
    return {"ping": "pong", "service": "core"}
