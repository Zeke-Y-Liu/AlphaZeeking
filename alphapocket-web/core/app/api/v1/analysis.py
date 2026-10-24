"""单公司分析端点（analysis_profiles CRUD + 四模块分析 + 消息 + 影子回测）。

路由
----
- ``GET    /analysis``                     列表（默认 active；``include_archived`` 可选）
- ``POST   /analysis``                     创建（标的存在性校验 + 自动影子组合）
- ``GET    /analysis/{aid}``               详情
- ``PUT    /analysis/{aid}``               更新（name/note/modules）
- ``DELETE /analysis/{aid}``               软删 + 影子组合级联归档
- ``POST   /analysis/{aid}/run``           触发分析 job（type="analysis"）
- ``GET    /analysis/{aid}/news``          最近消息（直读数据集）
- ``POST   /analysis/{aid}/news/refresh``  刷新该标的新闻（小 job）
- ``POST   /analysis/{aid}/backtest``      影子组合回测（复用现有回测链路）

本模块定义模块级 ``router``，由 ``app/api/v1/__init__.py`` 自动发现注册。
"""
from __future__ import annotations

import hashlib
import json
import logging
from datetime import datetime
from pathlib import Path
from typing import Annotated, Any

from applib.ctx import Ctx
from fastapi import APIRouter, Depends
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.api.v1.backtest import BacktestRequest, _validate_sync
from app.api.v1.data import RunnerDep
from app.core.config import Settings, get_settings
from app.core.errors import AppError, ErrorCode
from app.db.session import get_db
from app.models import Security
from app.services import analysis as svc
from app.services import backtest_svc, verification
from app.services.workspace import WorkspaceManager

logger = logging.getLogger("app.api.analysis")

router = APIRouter(prefix="/analysis", tags=["analysis"])

DbDep = Annotated[Session, Depends(get_db)]
SettingsDep = Annotated[Settings, Depends(get_settings)]
BT_ACCEPTED = 202


# ── 模型 ────────────────────────────────────────────────────────


class AnalysisCreate(BaseModel):
    """创建分析档案；``secid`` 必填，其余可选。"""

    secid: str = Field(min_length=1)
    aid: str | None = None
    name: str | None = None
    note: str | None = None
    modules: dict[str, Any] | None = None


class AnalysisUpdate(BaseModel):
    """更新分析档案（至少一个字段）。"""

    name: str | None = None
    note: str | None = None
    modules: dict[str, Any] | None = None


def _out(profile, db: Session) -> dict[str, Any]:
    portfolio = svc.shadow_portfolio(db, profile.aid)
    return svc.to_out(profile, latest_run=svc.latest_run_of(db, portfolio.id))


# ── CRUD ────────────────────────────────────────────────────────


@router.get("")
def list_analyses(db: DbDep, include_archived: bool = False) -> list[dict[str, Any]]:
    """分析档案列表（默认仅 active）；附最近一次 run 摘要。"""
    profiles = svc.list_profiles(db, include_archived=include_archived)
    out = []
    for p in profiles:
        try:
            out.append(_out(p, db))
        except AppError:                 # 影子组合缺失（历史脏数据）→ 仍返回档案
            out.append(svc.to_out(p, latest_run=None))
    return out


@router.post("", status_code=201)
def create_analysis(
    payload: AnalysisCreate, db: DbDep, settings: SettingsDep
) -> dict[str, Any]:
    """创建档案 + 影子组合（单标的 100%）。"""
    profile = svc.create_profile(
        db, secid=payload.secid, name=payload.name, note=payload.note,
        modules=payload.modules, aid=payload.aid, settings=settings,
    )
    return _out(profile, db)


@router.get("/{aid}")
def get_analysis(aid: str, db: DbDep) -> dict[str, Any]:
    return _out(svc.get_or_404(db, aid), db)


@router.put("/{aid}")
def update_analysis(aid: str, payload: AnalysisUpdate, db: DbDep) -> dict[str, Any]:
    profile = svc.get_or_404(db, aid)
    profile = svc.update_profile(
        db, profile, name=payload.name, note=payload.note, modules=payload.modules
    )
    return _out(profile, db)


@router.delete("/{aid}")
def delete_analysis(aid: str, db: DbDep) -> dict[str, Any]:
    """软删档案 + 影子组合级联归档（组合记录保留以追溯历史 run）。"""
    profile = svc.archive_profile(db, svc.get_or_404(db, aid))
    return svc.to_out(profile, latest_run=None)


# ── 分析 job ────────────────────────────────────────────────────


@router.post("/{aid}/run")
def run_analysis(aid: str, db: DbDep, runner: RunnerDep) -> JSONResponse:
    """触发分析 job（202 + job_id + poll）。"""
    profile = svc.get_or_404(db, aid)
    portfolio = svc.shadow_portfolio(db, aid)
    params = {"aid": aid}
    job_id = runner.submit(
        type="analysis",
        params=params,
        portfolio_id=portfolio.id,
        fn=lambda ctx: svc.run_analysis_job(
            ctx.ws_root, aid, params, ctx.job_id, ctx.report
        ),
        lock="compute",
        ws_name="default",
    )
    return JSONResponse(
        status_code=BT_ACCEPTED,
        content={"job_id": job_id, "poll": f"/api/v1/jobs/{job_id}",
                 "aid": profile.aid, "shadow_pid": portfolio.pid},
    )


# ── 新闻 ────────────────────────────────────────────────────────


@router.get("/{aid}/news")
def get_news(aid: str, db: DbDep, settings: SettingsDep, limit: int = 10) -> dict[str, Any]:
    profile = svc.get_or_404(db, aid)
    repo = _sandbox_repo(settings)
    return svc.read_news(repo, profile.secid, limit=max(1, min(limit, 30)))


@router.post("/{aid}/news/refresh")
def refresh_news(aid: str, db: DbDep, settings: SettingsDep, runner: RunnerDep) -> JSONResponse:
    """刷新该标的新闻（小 job；写 remote+sandbox 并同步 provenance 哈希）。"""
    profile = svc.get_or_404(db, aid)
    params = {"aid": aid, "secid": profile.secid}
    job_id = runner.submit(
        type="news_refresh",
        params=params,
        portfolio_id=None,
        fn=lambda ctx: _refresh_news_job(ctx.ws_root, profile.secid),
        lock="compute",
        ws_name="default",
    )
    return JSONResponse(status_code=BT_ACCEPTED,
                        content={"job_id": job_id, "poll": f"/api/v1/jobs/{job_id}"})


def _sandbox_repo(settings: Settings) -> Path:
    ws_root = WorkspaceManager(settings).ensure("default")
    return Ctx(root=ws_root).local_repo


def _refresh_news_job(ws_root: Path, secid: str) -> dict[str, Any]:
    """单标新闻刷新：写 remote 与 sandbox 两份，并更新 provenance 的 sha256/bytes。

    只动 news/stock_news.json 一条 provenance 记录；其余数据集纸面不动，
    保证 provenance_check 仍全绿（否则哈希不符会红）。
    """
    import akshare as ak
    from applib.datasource import _fetch_stock_news
    from sqlalchemy import select

    from app.db.session import session_scope

    with session_scope() as db:
        sec = db.execute(select(Security).where(Security.secid == secid)).scalar_one_or_none()
        if sec is None:
            raise AppError(ErrorCode.SECURITY_NOT_FOUND, "标的不存在或未注册")
        code, market = sec.code, sec.market

    if market != "A":
        return {"secid": secid, "updated": 0, "note": "该市场暂无公开新闻源"}

    fetched = _fetch_stock_news(ak, [{"code": code, "secid": secid, "name": secid}])
    items = fetched.get(secid) or []
    if not items:
        return {"secid": secid, "updated": 0, "note": "上游返回空"}

    ctx = Ctx(root=Path(ws_root))
    rel = "news/stock_news.json"
    for repo in (ctx.repo_dir, ctx.local_repo):
        target = repo / rel
        data = {}
        if target.exists():
            try:
                data = json.loads(target.read_text(encoding="utf-8"))
            except json.JSONDecodeError:
                data = {}
        data[secid] = items
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")

    # provenance 哈希同步（仅 news 那条；缺失则跳过，不影响刷新结果）
    prov_path = ctx.repo_dir / "market" / "provenance.json"
    if prov_path.exists():
        prov = json.loads(prov_path.read_text(encoding="utf-8"))
        target = ctx.repo_dir / rel
        for src in prov.get("sources", []):
            if src.get("file") == rel:
                src["sha256"] = hashlib.sha256(target.read_bytes()).hexdigest()
                src["bytes"] = target.stat().st_size
                src["params"] = {**(src.get("params") or {}),
                                 "refreshed_at": datetime.now().isoformat(timespec="seconds")}
        prov_path.write_text(json.dumps(prov, ensure_ascii=False, indent=2), encoding="utf-8")

    return {"secid": secid, "updated": len(items), "latest": items[0].get("dt")}


# ── 影子组合回测 ────────────────────────────────────────────────


@router.post("/{aid}/backtest")
def analysis_backtest(
    aid: str,
    db: DbDep,
    runner: RunnerDep,
    settings: SettingsDep,
    payload: BacktestRequest | None = None,
) -> JSONResponse:
    """影子组合回测：复用 ``/portfolios/{pid}/backtest`` 同链路（门禁一致）。"""
    svc.get_or_404(db, aid)
    portfolio = svc.shadow_portfolio(db, aid)
    verification.gate_or_raise(db)
    body = payload or BacktestRequest()
    _validate_sync(body, settings)
    params = body.model_dump()
    job_id = runner.submit(
        type="backtest",
        params=params,
        portfolio_id=portfolio.id,
        fn=lambda ctx: backtest_svc.run_backtest_job(
            ctx.ws_root, portfolio.pid, params, ctx.job_id, ctx.report
        ),
        lock="compute",
        ws_name="default",
    )
    return JSONResponse(
        status_code=BT_ACCEPTED,
        content={"job_id": job_id, "poll": f"/api/v1/jobs/{job_id}", "pid": portfolio.pid},
    )
