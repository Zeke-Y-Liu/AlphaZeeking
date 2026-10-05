"""应用配置（pydantic-settings，env 前缀 ``AP_``）。

键名与仓库根 ``.env.example`` 的 ``AP_*`` 段对齐；``.env`` 不存在时使用默认值，不报错。
"""
from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

# 仓库根 = alphapocket-web/（本文件位于 core/app/core/config.py）
REPO_ROOT = Path(__file__).resolve().parents[3]


class Settings(BaseSettings):
    """core 服务配置。"""

    model_config = SettingsConfigDict(
        env_prefix="AP_",
        env_file=str(REPO_ROOT / ".env"),
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    # 基础设施（键名对齐 .env.example）
    database_url: str = "postgresql+psycopg://ap:ap@localhost:5432/ap"
    redis_url: str = "redis://localhost:6379/0"
    minio_endpoint: str = "localhost:9000"
    minio_access_key: str = "minioadmin"
    minio_secret_key: str = "minioadmin"
    minio_bucket: str = "ap-artifacts"

    # 认证（T5.x 使用）
    jwt_secret: str = "dev-only-change-me"
    jwt_access_ttl: str = "15m"
    jwt_refresh_ttl: str = "7d"

    # 工作区（core 的 Ctx root 注入点，见 T0.3）
    workspace_root: str = "var/ws/default"

    # 评估 run 缺口自愈开关（§9.3 T2.3；env: AP_REVIEW_AUTO_INCREMENTAL）
    review_auto_incremental: bool = True

    # 运行时
    log_level: str = "INFO"
    core_port: int = 8082
    app_env: str = "dev"

    @property
    def workspace_path(self) -> Path:
        """工作区根目录的绝对路径（相对路径基于仓库根解析）。"""
        path = Path(self.workspace_root)
        if not path.is_absolute():
            path = REPO_ROOT / path
        return path


@lru_cache
def get_settings() -> Settings:
    """进程级单例配置。测试可通过 ``get_settings.cache_clear()`` 覆盖。"""
    return Settings()
