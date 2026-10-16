"""数据同步 job 核心逻辑（T1.4，§4.2 三 mode + §4.3 存储分治）。

关键路径语义（务必与 applib ``Ctx`` 对齐）
----------------------------------------
``Ctx(root=<ws_root>)`` 下：

- 工具**实际读取位** ``ctx.local_repo`` = ``<ws>/sandbox/repo``；
- ``ctx.repo_dir`` = ``<ws>/remote/repo`` 是 ``fetch_all`` 的**写入位**。

因此同步主链固定为：

1. ``fetch_all(ctx, ...)`` 写 ``<ws>/remote/repo``；
2. **clone**：``<ws>/remote/repo`` → ``<ws>/sandbox/repo``；
3. **materialize**：PG 权威数据物化到 ``<ws>/sandbox/repo``（``dim/security.json``
   与 ``portfolio/{pid}.json``），使 applib 工具真能读到；
4. **build DuckDB**：``<ws>/sandbox/repo`` JSON → ``<ws>/sandbox/data/ap.duckdb``
   并计算 A/B 引擎因子（对齐 α 任务链 ``data.sync`` → ``factor.compute_alpha``
   → ``factor.compute``），供六项验证直读。

**路径口径登记**：§5.2 / W4（T2.2）的组合物化写到 ``<ws>/repo/portfolio/``，与
applib 实读的 ``<ws>/sandbox/repo/portfolio/`` 不一致；本任务**以 sandbox/repo
为准**（让 applib 真能读到）。协调方如需兼容可在两处都写，见交付说明。

三 mode（§4.2）
--------------
- ``bootstrap``：universe=PG securities 全集，``start="2021-01-01"``，全量；
- ``incremental``：universe=PG securities 全集，``incremental_from={secid: 最后 bar 日期}``
  （缺则不带该键 → 全量 start）；
- ``refresh``：universe=[secid]，强制重拉最近 N(=5) 日（``start=today-5d``），
  经 ``incremental_from`` 走合并语义（替换最近 N 日，保留历史）。

限频熔断（§4.2）
--------------
Redis 记录上游连续失败计数，≥3 次 → 熔断 10 分钟；期间 ``POST /data/sync`` 返回
``503 UPSTREAM_RATE_LIMITED``。Redis 不可达 → fail-open（放行）并打 WARNING。

**universe_missing 限制（登记）**：``fetch_all`` 对不在 applib ``CN4+MULTI5`` 定义内的
已注册 secid 会返回 ``universe_missing``——本任务透明记录、不判失败。任意新标的需要
applib ``datasource`` 扩展，本阶段不支持。

六项验证（T1.6）
--------------
同步主链末尾调用可调用钩子 ``verify_hook``。调用方未显式传入且 ``job_id`` 非空时
默认使用 ``app.services.verification.verify_hook``，即生产路径（HTTP 触发）同步完成后
自动执行六项验证并写 ``verify_results``；钩子内部吸收异常，写库失败不使同步 job failed。
"""
from __future__ import annotations

import json
import logging
import shutil
import time
from collections.abc import Callable
from datetime import date, timedelta
from pathlib import Path
from typing import Any

from applib import datasource
from applib.ctx import Ctx, new_run_id
from sqlalchemy import select

from app.core.errors import AppError, ErrorCode
from app.db.redis import get_redis
from app.db.session import session_scope
from app.models import Portfolio, Run, Security
from app.services.portfolios import build_materialized, positions_of

logger = logging.getLogger("app.data_sync")

# ── mode 与同步窗口（§4.2）──────────────────────────────────────
BOOTSTRAP_START = "2021-01-01"
REFRESH_DAYS = 5
VALID_MODES = frozenset({"bootstrap", "incremental", "refresh"})

# ── 限频熔断（§4.2 / C7）────────────────────────────────────────
FAIL_KEY = "ap:sync:upstream_fail_count"
CIRCUIT_KEY = "ap:sync:upstream_circuit"
FAIL_THRESHOLD = 3
CIRCUIT_TTL_SECONDS = 600

_DAILY_BARS_REL = Path("market") / "daily_bars.json"

# DuckDB 物化所需输入（applib.data.data_sync 的读取面）；缺失即无法重建
_DUCKDB_INPUTS: tuple[Path, ...] = (
    Path("market") / "daily_bars.json",
    Path("market") / "daily_bars_raw.json",
    Path("market") / "cross_check_tx.json",
    Path("market") / "trade_calendar.json",
    Path("dim") / "security.json",
    Path("factors") / "fundamental_values.json",
)


# ── 熔断状态（Redis；不可达 fail-open）──────────────────────────


def circuit_open() -> bool:
    """熔断是否打开；Redis 不可达 → False（fail-open）并打 WARNING。"""
    try:
        return bool(get_redis().exists(CIRCUIT_KEY))
    except Exception as exc:  # noqa: BLE001 - Redis 不可达即降级放行
        logger.warning("Redis 不可达，同步熔断检查降级放行（fail-open）：%s", type(exc).__name__)
        return False


def record_upstream_failure() -> int:
    """上游失败计数 +1；达到阈值则置熔断标志（TTL 10 分钟）。返回当前计数。

    Redis 不可达 → 返回 0（fail-open，不阻断主流程）并打 WARNING。
    """
    try:
        client = get_redis()
        count = int(client.incr(FAIL_KEY))
        client.expire(FAIL_KEY, CIRCUIT_TTL_SECONDS)
        if count >= FAIL_THRESHOLD:
            client.set(CIRCUIT_KEY, "1", ex=CIRCUIT_TTL_SECONDS)
            logger.warning("同步上游连续失败 %d 次，熔断 %d 秒", count, CIRCUIT_TTL_SECONDS)
        return count
    except Exception as exc:  # noqa: BLE001
        logger.warning("Redis 不可达，同步失败计数未记录（fail-open）：%s", type(exc).__name__)
        return 0


def record_upstream_success() -> None:
    """上游成功：清零失败计数并解除熔断；Redis 不可达则告警（fail-open）。"""
    try:
        get_redis().delete(FAIL_KEY, CIRCUIT_KEY)
    except Exception as exc:  # noqa: BLE001
        logger.warning("Redis 不可达，同步失败计数清零失败（fail-open）：%s", type(exc).__name__)


# ── PG 读取 ─────────────────────────────────────────────────────


def _pg_universe() -> list[str]:
    """PG securities 全集 secid（按 secid 升序，确定性）。"""
    with session_scope() as db:
        return list(db.execute(select(Security.secid).order_by(Security.secid)).scalars().all())


def _last_bar_dates(ctx: Ctx) -> dict[str, str]:
    """读 workspace 各标的最后 bar 日期。

    依次尝试工具实读位 ``sandbox/repo`` 与 ``fetch_all`` 写入位 ``remote/repo``；
    取每个 secid 的 ``dt`` 最大值（不假设序列有序）。
    """
    for base in (ctx.local_repo, ctx.repo_dir):
        path = base / _DAILY_BARS_REL
        if not path.exists():
            continue
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        out: dict[str, str] = {}
        for secid, bars in data.items():
            dates = [r.get("dt") for r in bars if isinstance(r, dict) and r.get("dt")]
            if dates:
                out[secid] = max(dates)
        if out:
            return out
    return {}


# ── clone / materialize ─────────────────────────────────────────


def _clone_to_sandbox(ctx: Ctx) -> int:
    """``<ws>/remote/repo`` → ``<ws>/sandbox/repo``（rmtree+copytree，返回文件数）。

    等价 ``applib.repo.repo_clone`` 的文件级镜像，但不触发其内部 ``seed_remote``
    （provenance 缺失时会注入模拟种子）与版本面（refs/objects）写入，保持同步链
    确定性；语义差异登记见模块 docstring。

    因子注册表写保护（T-R6/T-R12 语义，对齐 RUNBOOK）：clone 会整体替换 sandbox/repo，
    而用户经引擎 API 的注册表改动只落在 sandbox——故 clone 前**先快照 sandbox 注册表**，
    clone 后按 ``merge_registry(sandbox快照, remote注册表)`` 回写：用户改动（含门禁
    新增因子）在每次同步后完整保留；sandbox 无注册表（首次）→ 保持 remote 原样。
    """
    from applib.engines.base import export_registry_v2, load_registry_raw, merge_registry

    ctx.ensure_sandbox()
    pre_local_registry = load_registry_raw(ctx.local_repo)
    if ctx.local_repo.exists():
        shutil.rmtree(ctx.local_repo, ignore_errors=True)
    shutil.copytree(ctx.repo_dir, ctx.local_repo)
    if isinstance(pre_local_registry, dict) and pre_local_registry:
        reg_path = ctx.local_repo / "factors" / "registry.json"
        defaults = load_registry_raw(ctx.repo_dir)
        merged = merge_registry(
            pre_local_registry, defaults if isinstance(defaults, dict) and defaults
            else export_registry_v2())
        reg_path.parent.mkdir(parents=True, exist_ok=True)
        reg_path.write_text(json.dumps(merged, ensure_ascii=False, indent=2),
                            encoding="utf-8")
    return sum(1 for p in ctx.local_repo.rglob("*") if p.is_file())


def materialize_from_pg(ctx: Ctx) -> dict[str, Any]:
    """把 PG 权威数据物化到 ``<ws>/sandbox/repo``（工具实读位）。

    - ``dim/security.json``：PG securities 全集（字段对齐 applib ``fetch_all`` 形态）；
    - ``portfolio/{pid}.json``：PG 全部 active 组合（复用 W4 ``build_materialized``
      构造，applib 兼容形态见 §5.2）。

    portfolio 目录先清空再写，保证与 PG active 集合一致（不被 ``fetch_all`` 的
    硬编码 ``cn4/multi5`` 残留污染）。
    """
    with session_scope() as db:
        secs = db.execute(select(Security).order_by(Security.secid)).scalars().all()
        pfs = (
            db.execute(
                select(Portfolio).where(Portfolio.status == "active").order_by(Portfolio.id)
            )
            .scalars()
            .all()
        )
        secmap = {s.secid: s for s in secs}
        dim_rows = [
            {
                "secid": s.secid,
                "name": s.name,
                "market": s.market,
                "asset_type": s.asset_type,
                "industry": s.industry,
                "currency": s.currency,
                "hk_connect": s.hk_connect,
                "source": s.source,
            }
            for s in secs
        ]
        portfolio_payloads = [
            (pf.pid, build_materialized(pf, positions_of(db, pf.id), secmap)) for pf in pfs
        ]

    dim_path = ctx.local_repo / "dim" / "security.json"
    dim_path.parent.mkdir(parents=True, exist_ok=True)
    dim_path.write_text(json.dumps(dim_rows, ensure_ascii=False, indent=2), encoding="utf-8")

    pdir = ctx.local_repo / "portfolio"
    pdir.mkdir(parents=True, exist_ok=True)
    for stale in pdir.glob("*.json"):
        stale.unlink()
    for pid, payload in portfolio_payloads:
        (pdir / f"{pid}.json").write_text(
            json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
        )

    return {
        "securities": len(dim_rows),
        "portfolios": [pid for pid, _ in portfolio_payloads],
    }


def build_duckdb(ctx: Ctx) -> dict[str, Any]:
    """``sandbox/repo`` JSON → ``sandbox/data/ap.duckdb`` 并计算 A/B 引擎因子。

    对齐 α 任务链（``data.sync`` → ``factor.compute_alpha`` → ``factor.compute``）。
    六项验证中的 ``pit_audit`` / ``golden_test`` 直读 ``ctx.db_path``（只读），
    若不先物化 DuckDB，二者恒以 ``Cannot open database ... does not exist`` 判 fail。

    输入文件缺失或行情快照为空时跳过并告警（轻量测试夹具只写子集；增量窗口无新行情
    时 α ``fetch_all`` 会整体重建出空快照）；生产 bootstrap/全量路径数据集齐全，正常物化。
    """
    missing = [str(rel) for rel in _DUCKDB_INPUTS if not (ctx.local_repo / rel).exists()]
    if missing:
        logger.warning("跳过 DuckDB 物化：sandbox/repo 缺少输入 %s", missing)
        return {"built": False, "reason": "missing_inputs", "missing": missing}

    bars_path = ctx.local_repo / _DAILY_BARS_REL
    try:
        bars = json.loads(bars_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        bars = {}
    rows = sum(len(v) for v in bars.values() if isinstance(v, list)) if isinstance(bars, dict) else 0
    if rows == 0:
        # α ``fetch_all`` 整体重建 remote/repo；增量窗口无新行情时快照为空。
        # 空快照无法重建因子，物化只会掩盖问题 → 跳过，交由六项验证如实报红。
        logger.warning(
            "跳过 DuckDB 物化：sandbox/repo/market/daily_bars.json 为空快照（增量窗口无新行情）；"
            "如需恢复请执行 bootstrap 全量同步"
        )
        return {"built": False, "reason": "empty_bars", "rows": 0}

    from applib import data as applib_data
    from applib import tools as applib_tools

    synced = applib_data.data_sync({}, ctx)  # 基础表（dwd_* / dim_* / cross_check 等）
    engine_a = applib_tools.factor_compute_alpha({}, ctx)  # factor_value_daily + factor_pit_meta
    engine_b = applib_tools.factor_compute({}, ctx)  # 20 日信号因子
    logger.info(
        "DuckDB 物化完成 as_of=%s tables=%s engine_a=%s engine_b=%s",
        synced.get("as_of"),
        len(synced.get("synced", {})),
        engine_a.get("as_of"),
        engine_b.get("as_of"),
    )
    return {
        "built": True,
        "as_of": synced.get("as_of"),
        "synced": synced.get("synced", {}),
        "engine_a_as_of": engine_a.get("as_of"),
        "engine_b_as_of": engine_b.get("as_of"),
    }


# ── 辅助 ────────────────────────────────────────────────────────


def _parse_date(value: Any) -> date | None:
    if not value:
        return None
    try:
        return date.fromisoformat(str(value)[:10])
    except ValueError:
        return None


def _report(progress: Callable[[int, str | None], None] | None, pct: int, note: str) -> None:
    if progress is not None:
        progress(pct, note)


# ── 主链 ────────────────────────────────────────────────────────


def sync_workspace(
    ws_root: str | Path,
    mode: str,
    secid: str | None = None,
    progress: Callable[[int, str | None], None] | None = None,
    *,
    job_id: str | None = None,
    verify_hook: Callable[..., Any] | None = None,
) -> dict[str, Any]:
    """按 §4.2 三 mode 执行一次 workspace 同步，返回结构化结果。

    ``job_id`` 非空时落 ``runs`` 记录（type=data_sync），并在结果中返回 ``run_uid``。
    ``verify_hook`` 为 T1.6 六项验证可调用钩子（默认 None → 不执行）。
    """
    if mode not in VALID_MODES:
        raise AppError(
            ErrorCode.VALIDATION_ERROR,
            f"未知 mode：{mode!r}",
            detail={"mode": mode, "allowed": sorted(VALID_MODES)},
        )
    if mode == "refresh" and not secid:
        raise AppError(
            ErrorCode.VALIDATION_ERROR, "refresh 模式必须提供 secid", detail={"field": "secid"}
        )

    ws_root = Path(ws_root)
    ctx = Ctx(root=ws_root)
    ctx.ensure_sandbox()
    started = time.monotonic()

    _report(progress, 5, "读取 PG universe")
    universe_pg = _pg_universe()
    today = date.today()

    if mode == "bootstrap":
        universe = universe_pg
        start = BOOTSTRAP_START
        incremental_from: dict[str, str] | None = None
    elif mode == "incremental":
        last = _last_bar_dates(ctx)
        universe = universe_pg
        start = BOOTSTRAP_START  # 未列入 incremental_from 的 secid 仍按全量起点
        incremental_from = {s: last[s] for s in universe if last.get(s)}
    else:  # refresh
        universe = [secid]  # type: ignore[list-item]
        refresh_start = today - timedelta(days=REFRESH_DAYS)
        start = refresh_start.isoformat()
        # T-fix：refresh 也走增量合并（incremental_from=窗口前一日 → 自窗口首日重拉并
        # 按日合并），替换最近 N 日而非整体覆盖——旧行为 rmtree 后仅写 5 日窗口会丢历史
        incremental_from = {secid: (refresh_start - timedelta(days=1)).isoformat()}

    _report(progress, 15, f"fetch_all（{mode}）")
    try:
        fetched = datasource.fetch_all(
            ctx, start=start, universe=universe, incremental_from=incremental_from,
            # bootstrap = 以 PG securities 全集重建（权威全集，含 applib 内置清单外的
            # HK/US 标的）→ 显式授权放行破坏性重写守卫（详见 applib.fetch_all docstring）。
            allow_rebuild=(mode == "bootstrap"),
        )
    except Exception:
        record_upstream_failure()
        raise
    record_upstream_success()

    _report(progress, 70, "clone remote → sandbox")
    cloned_files = _clone_to_sandbox(ctx)

    _report(progress, 85, "materialize PG → sandbox/repo")
    materialized = materialize_from_pg(ctx)

    _report(progress, 88, "物化 DuckDB 与因子（对齐 α 任务链）")
    duckdb_stats = build_duckdb(ctx)

    # applib 返回体兼容（mock 可能缺字段，一律防御取值）
    fetched = fetched or {}
    per_secid = fetched.get("per_secid") or {}
    bars_fetched = sum(int(v.get("bars") or 0) for v in per_secid.values())
    as_of = _parse_date(fetched.get("as_of_bars"))
    duration = round(time.monotonic() - started, 3)

    result: dict[str, Any] = {
        "mode": mode,
        "start": start,
        "universe": list(universe),
        "universe_fetched": fetched.get("universe_fetched") or [],
        "universe_missing": fetched.get("universe_missing") or [],
        "universe_failed": fetched.get("universe_failed") or [],
        "incremental_from": incremental_from or {},
        "bars_fetched": bars_fetched,
        "as_of": as_of.isoformat() if as_of else None,
        "securities": materialized["securities"],
        "portfolios": materialized["portfolios"],
        "cloned_files": cloned_files,
        "duckdb": duckdb_stats,
        "duration_s": duration,
    }

    # T1.6 钩子：调用方未显式传入且 job_id 非空时，默认执行六项验证并写 verify_results
    # （延迟导入规避循环导入；verify_hook 内部吸收异常，写库失败绝不让同步 job failed）
    if verify_hook is None and job_id is not None:
        from app.services import verification

        verify_hook = verification.verify_hook
    if verify_hook is not None:
        _report(progress, 95, "六项验证（T1.6）")
        verify_hook(ctx=ctx, job_id=job_id, result=result)

    if job_id is not None:
        run_uid = new_run_id("run")
        summary = {
            "mode": mode,
            "start": start,
            "as_of": result["as_of"],
            "universe_count": len(universe),
            "universe_fetched": result["universe_fetched"],
            "universe_missing": result["universe_missing"],
            "universe_failed": result["universe_failed"],
            "bars_fetched": bars_fetched,
            "incremental_from": result["incremental_from"],
            "securities": materialized["securities"],
            "portfolios": materialized["portfolios"],
            "duration_s": duration,
        }
        with session_scope() as db:
            db.add(
                Run(
                    run_uid=run_uid,
                    job_id=job_id,
                    portfolio_id=None,
                    type="data_sync",
                    as_of=as_of,
                    summary=summary,
                    artifact_keys=[],
                )
            )
        result["run_uid"] = run_uid

    _report(progress, 100, "完成")
    return result
