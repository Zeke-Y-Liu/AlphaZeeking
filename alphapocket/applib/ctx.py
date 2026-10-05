"""运行时上下文与路径约定（V4-α：目录级 sandbox 模拟，生产为 Docker quant-std 镜像）

工作区目录约定（T0.3 applib 服务化）
------------------------------------
每个工作区（workspace）是一个自包含根目录，Web core 为多 workspace 隔离使用：

    var/ws/<name>/remote      # 远端仓库 + 内容寻址对象库（repo/、objects/、refs.json）
    var/ws/<name>/sandbox     # 本地运行时工作区（repo/、data/ap.duckdb、artifacts/、
                              #   logs/、.ap/、checkpoints.json）

`Ctx(root=...)` 注入工作区根时按此结构映射：

    Ctx(root="var/ws/alpha")  →  remote_dir  = var/ws/alpha/remote
                                 sandbox_dir = var/ws/alpha/sandbox

`Ctx()`（root=None，默认）沿用 applib 同级的 `alphapocket/remote`、`alphapocket/sandbox`，
与历史行为逐字节一致（CLI 与 ap-mcp 不受影响）。

生产（Docker）下由环境变量 `AP_WORKSPACE_ROOT`（见 alphapocket-web/.env.example，
默认 `var/ws/default`）提供根路径，core 读取后传入 `Ctx(root=...)`。
"""
from __future__ import annotations
import os, json, hashlib, time
from pathlib import Path
from dataclasses import dataclass, field

# 项目根 = applib/..（本进程内置的默认工作区；root 注入时为显式指定的工作区根）
PROJECT_ROOT = Path(__file__).resolve().parent.parent
REMOTE_DIR = PROJECT_ROOT / "remote"          # 云端替身：远端仓库 + 对象库（生产为云端 OSS/版本服务）
SANDBOX_DIR = PROJECT_ROOT / "sandbox"        # 本地运行时工作区（quant-std 模拟）
CODE_VERSION = "v4-alpha-real+r2a4e91c"   # 模拟 git sha（生产取 git rev-parse HEAD）


@dataclass
class Ctx:
    """工具执行上下文：所有 tool 只经由 Ctx 访问文件系统，禁止逃逸 sandbox"""
    project_root: Path = PROJECT_ROOT
    remote_dir: Path = REMOTE_DIR
    sandbox_dir: Path = SANDBOX_DIR
    code_version: str = CODE_VERSION
    root: Path | None = None    # T0.3：外部（Web core）注入的工作区根；None=沿用 applib 同级（行为不变）

    def __post_init__(self):
        # 仅在显式注入 root 时重算工作区路径；root=None 时不触碰默认，保证向后兼容
        if self.root is not None:
            r = Path(self.root)
            self.remote_dir = r / "remote"
            self.sandbox_dir = r / "sandbox"

    @property
    def repo_dir(self) -> Path:      # 远端 main 内容（云端替身）
        return self.remote_dir / "repo"

    @property
    def objects_dir(self) -> Path:   # 内容寻址对象库
        return self.remote_dir / "objects"

    @property
    def refs_file(self) -> Path:
        return self.remote_dir / "refs.json"

    @property
    def crs_file(self) -> Path:      # Change Request 队列
        return self.remote_dir / "crs.json"

    @property
    def local_repo(self) -> Path:    # clone 到 sandbox 的本地工作副本
        return self.sandbox_dir / "repo"

    @property
    def db_path(self) -> Path:
        return self.sandbox_dir / "data" / "ap.duckdb"

    @property
    def artifacts_dir(self) -> Path:
        return self.sandbox_dir / "artifacts"

    @property
    def checkpoints_file(self) -> Path:
        return self.sandbox_dir / "checkpoints.json"

    @property
    def credentials(self) -> Path:
        return self.sandbox_dir / ".ap" / "credentials.json"

    @property
    def profile_file(self) -> Path:
        return self.sandbox_dir / ".ap" / "profile.json"

    def ensure_sandbox(self):
        """quant-std profile：一份运行时定义（生产挂 Docker 镜像，α 用目录隔离模拟）"""
        for p in (self.local_repo, self.db_path.parent, self.artifacts_dir,
                  self.sandbox_dir / ".ap", self.sandbox_dir / "logs"):
            p.mkdir(parents=True, exist_ok=True)
        if not self.profile_file.exists():
            profile = {
                "profile": "quant-std",
                "image": "quant-std:0.1 (dir-sandbox 模拟; 生产=Docker)",
                "network": "offline (α: 数据经 remote/ 本地替身同步)",
                "limits": {"cpu": 4, "mem_gb": 8},
                "isolation": "cwd 限定 sandbox/；工具仅经 Ctx 访问路径",
            }
            self.profile_file.write_text(json.dumps(profile, ensure_ascii=False, indent=2), encoding="utf-8")


def sha256_bytes(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()


def sha256_file(p: Path) -> str:
    return sha256_bytes(p.read_bytes())


def sha256_obj(obj) -> str:
    return sha256_bytes(json.dumps(obj, ensure_ascii=False, sort_keys=True).encode("utf-8"))


def new_run_id(prefix="run") -> str:
    return f"{prefix}_{time.strftime('%Y%m%d_%H%M%S')}_{sha256_bytes(str(time.time_ns()).encode())[:6]}"
