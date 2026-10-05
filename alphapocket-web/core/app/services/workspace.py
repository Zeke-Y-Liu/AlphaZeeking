"""workspace 解析与进程级互斥锁（T1.1 薄封装 + T1.2 ``WorkspaceManager``）。

职责
----
1. ``resolve_workspace(settings)``：T1.1 保留的路径解析接缝（勿删）。
2. ``WorkspaceManager``：workspace 目录幂等初始化（复用 α ``applib.ctx.Ctx`` +
   ``ensure_sandbox()``），以及进程级互斥锁——
   - ``compute_lock(ws_name)``：同一 workspace 同时最多 1 个计算 job；
   - ``sync_lock()``：sync 全局唯一。

锁语义（§6.1）
--------------
同一 workspace 同时最多 1 个计算 job（互斥），sync 全局唯一；冲突返回
``409 JOB_LOCKED``（前端转排队提示）。本实现按 §6.1 语义：**提交时若锁被占用
即抛/判 JOB_LOCKED（不排队）**，由调用方（HTTP 层）转 409；前端据 ``poll`` 轮询提示。
判定点位于服务层（``WorkspaceManager.try_acquire_*`` → ``JobRunner.submit``），
HTTP 层只负责把 ``AppError(JOB_LOCKED)`` 映射为 409（见 ``app/core/errors.py``）。

跨线程约定
----------
锁在 ``JobRunner.submit``（调用方线程）非阻塞获取、在 job 线程结束时释放。因此
底层使用 ``threading.Lock``（非 RLock：RLock 要求同线程释放，跨线程释放会报错）。

α 坑（关键不变量）
------------------
DuckDB 是**单文件单写**：同一 workspace 的 ``sandbox/data/ap.duckdb`` 在同一时刻
只能有一个写者。因此**所有 applib 调用**（review/backtest/rebalance_plan/同步等，
后续任务实现）**必须在 ``compute_lock(ws_name)`` 内串行执行**；sync 任务（全局
唯一）亦受同一约束。任何时候都不得绕开 ``JobRunner``/本管理器直接并发调用 applib。
"""
from __future__ import annotations

import threading
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from applib.ctx import Ctx

from app.core.config import Settings

DEFAULT_WS = "default"


def resolve_workspace(settings: Settings) -> Path:
    """返回当前配置的工作区根目录（绝对路径）。"""
    return settings.workspace_path


class WorkspaceManager:
    """workspace 目录初始化 + 进程级互斥锁。

    多 workspace 映射：默认 workspace(``default``) 即 ``settings.workspace_path``
    （如 ``var/ws/default``）；其余 ``<name>`` 取 ``settings.workspace_path`` 的同级
    兄弟目录（如 ``var/ws/<name>``），与 α 文档 ``var/ws/<name>/{remote,sandbox}`` 对齐。
    """

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._compute_locks: dict[str, threading.Lock] = {}
        self._locks_guard = threading.Lock()
        self._sync_lock = threading.Lock()

    # ── 目录 ────────────────────────────────────────────────────
    def _ws_root(self, ws_name: str) -> Path:
        base = self._settings.workspace_path
        if ws_name == DEFAULT_WS:
            return base
        return base.parent / ws_name

    def ensure(self, ws_name: str = DEFAULT_WS) -> Path:
        """幂等初始化 workspace 目录，返回工作区根。

        - sandbox 侧交由 α ``Ctx.ensure_sandbox()``（repo/data/artifacts/logs/.ap +
          profile.json），不另写一套目录结构；
        - remote 侧仅保证根目录存在（``repo/objects/refs.json`` 由后续 sync/clone 落盘）。
        """
        root = self._ws_root(ws_name)
        root.mkdir(parents=True, exist_ok=True)
        ctx = Ctx(root=root)
        ctx.ensure_sandbox()
        ctx.remote_dir.mkdir(parents=True, exist_ok=True)
        return root

    # ── 锁：compute（按 ws_name 分桶） ──────────────────────────
    def _lock_for(self, ws_name: str) -> threading.Lock:
        with self._locks_guard:
            lock = self._compute_locks.get(ws_name)
            if lock is None:
                lock = threading.Lock()
                self._compute_locks[ws_name] = lock
            return lock

    def try_acquire_compute(self, ws_name: str = DEFAULT_WS) -> bool:
        """非阻塞获取计算锁；成功返回 True 并**持锁**，失败返回 False（勿再释放）。"""
        return self._lock_for(ws_name).acquire(blocking=False)

    def release_compute(self, ws_name: str = DEFAULT_WS) -> None:
        self._lock_for(ws_name).release()

    @contextmanager
    def compute_lock(self, ws_name: str = DEFAULT_WS) -> Iterator[None]:
        """阻塞式上下文管理器：获取该 workspace 计算锁，退出释放。"""
        lock = self._lock_for(ws_name)
        lock.acquire()
        try:
            yield
        finally:
            lock.release()

    # ── 锁：sync（全局唯一） ────────────────────────────────────
    def try_acquire_sync(self) -> bool:
        """非阻塞获取全局 sync 锁；成功返回 True 并**持锁**，失败返回 False。"""
        return self._sync_lock.acquire(blocking=False)

    def release_sync(self) -> None:
        self._sync_lock.release()

    @contextmanager
    def sync_lock(self) -> Iterator[None]:
        """阻塞式上下文管理器：获取全局 sync 锁，退出释放。"""
        self._sync_lock.acquire()
        try:
            yield
        finally:
            self._sync_lock.release()
