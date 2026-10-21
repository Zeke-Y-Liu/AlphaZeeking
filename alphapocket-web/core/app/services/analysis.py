"""单公司分析：档案 CRUD（含影子组合）+ 四模块分析 job + 消息读取。

设计要点
--------
1. **影子组合**：档案创建时自动建 ``shadow_{aid}``（单标的 100% 权重，``strategy``
   标记 ``kind="analysis_shadow"``）。回测/评估/调仓复用现有组合链路，本域不另建
   计算管道；删除档案 → 档案软删 + 影子组合级联归档。
2. **分析 job**（``type="analysis"``）：读 ``sandbox/repo`` 的四份数据面——
   ``factors/fundamental_values.json``（基本面）、``industry/industry_index.json``
   （行业趋势/行业现状）、``market/daily_bars.json`` + ``applib.technical``（短线超卖）、
   ``news/stock_news.json``（最新消息）→ 写 artifact + ``runs(type="analysis")``。
3. **降级**：任一模块数据缺失 → 该模块 ``available=false`` + ``note``，绝不使 job 失败
   （与 review 的 data_risk 提示同款思路）。
"""
from __future__ import annotations

import json
import logging
import re
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Any, Callable

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import Settings
from app.core.errors import AppError, ErrorCode
from app.db.session import session_scope
from app.models import AnalysisProfile, AuditLog, Portfolio, Position, Run, Security
from app.services import portfolios as pf_svc
from app.services.artifacts import put_run_artifacts
from applib.ctx import Ctx, new_run_id
from applib.industry import match_sw_l1
from applib.technical import oversold_score

logger = logging.getLogger("app.analysis")

AUDIT_ENTITY = "analysis_profile"
AUDIT_ACTOR = "system"
SHADOW_KIND = "analysis_shadow"
DEFAULT_MODULES: dict[str, bool] = {
    "fundamental": True, "industry": True, "technical": True, "news": True,
}
NEWS_LIMIT = 10

_AID_AUTO_RE = re.compile(r"^ca_(\d+)$")


# ── 通用 ────────────────────────────────────────────────────────


def _load_json(path: Path, default: Any) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return default


def shadow_pid(aid: str) -> str:
    """档案的影子组合 pid（单标的 100% 组合）。"""
    return f"shadow_{aid}"


def suggest_aid(db: Session) -> str:
    """自动生成 ``ca_<n>``（n = 现有最大编号 + 1）。"""
    existing = set(
        db.execute(select(AnalysisProfile.aid).where(AnalysisProfile.aid.like("ca_%"))).scalars()
    )
    nums = [int(m.group(1)) for aid in existing if (m := _AID_AUTO_RE.match(aid))]
    n = max(nums, default=0) + 1
    while f"ca_{n}" in existing:
        n += 1
    return f"ca_{n}"


def _audit(db: Session, aid: str, action: str, payload: dict[str, Any]) -> None:
    db.add(AuditLog(entity=AUDIT_ENTITY, entity_id=aid, action=action,
                    actor=AUDIT_ACTOR, payload=payload))


def get_or_404(db: Session, aid: str, *, include_archived: bool = True) -> AnalysisProfile:
    profile = db.execute(
        select(AnalysisProfile).where(AnalysisProfile.aid == aid)
    ).scalar_one_or_none()
    if profile is None or (not include_archived and profile.status != "active"):
        raise AppError(ErrorCode.NOT_FOUND, f"分析档案不存在：{aid}")
    return profile


def to_out(profile: AnalysisProfile, *, latest_run: dict[str, Any] | None = None) -> dict[str, Any]:
    return {
        "id": profile.id,
        "aid": profile.aid,
        "secid": profile.secid,
        "name": profile.name,
        "note": profile.note,
        "modules": profile.modules or {},
        "status": profile.status,
        "shadow_pid": shadow_pid(profile.aid),
        "created_at": profile.created_at,
        "updated_at": profile.updated_at,
        "latest_run": latest_run,
    }


def latest_run_of(db: Session, portfolio_id: int) -> dict[str, Any] | None:
    """影子组合最近一次 run（分析 job 落在此组合下）。"""
    return pf_svc.latest_run(db, portfolio_id)


def _module_flags(modules: dict[str, Any] | None) -> dict[str, bool]:
    merged = dict(DEFAULT_MODULES)
    for k, v in (modules or {}).items():
        if k in merged:
            merged[k] = bool(v)
    return merged


def list_profiles(db: Session, *, include_archived: bool = False) -> list[AnalysisProfile]:
    """档案列表（默认仅 active，按 id 升序）。"""
    stmt = select(AnalysisProfile).order_by(AnalysisProfile.id)
    if not include_archived:
        stmt = stmt.where(AnalysisProfile.status == "active")
    return list(db.execute(stmt).scalars().all())


# ── CRUD ────────────────────────────────────────────────────────

def create_profile(
    db: Session,
    *,
    secid: str,
    name: str | None,
    note: str | None,
    modules: dict[str, Any] | None,
    aid: str | None,
    settings: Settings,
) -> AnalysisProfile:
    """建档案 + 影子组合（单标的 100%）；标的必须已注册。"""
    security = db.execute(select(Security).where(Security.secid == secid)).scalar_one_or_none()
    if security is None:
        raise AppError(ErrorCode.SECURITY_NOT_FOUND, "标的不存在或未注册",
                       detail={"missing_secids": [secid]})

    aid = aid or suggest_aid(db)
    if db.execute(select(AnalysisProfile.id).where(AnalysisProfile.aid == aid)).scalar_one_or_none():
        raise AppError(ErrorCode.VALIDATION_ERROR, f"aid 已存在：{aid}",
                       detail={"field": "aid", "conflict": aid})
    pid = shadow_pid(aid)
    pf_svc.ensure_pid_available(db, pid)

    profile = AnalysisProfile(
        aid=aid,
        secid=secid,
        name=name or f"{security.name} · 单公司分析",
        note=note,
        modules=_module_flags(modules),
        status="active",
    )
    db.add(profile)
    db.flush()

    portfolio = Portfolio(
        pid=pid,
        name=f"{profile.name}（影子组合）",
        note=f"单公司分析 {aid} 的影子组合：{secid} 100% 权重，供回测/评估复用",
        base_ccy="CNY",
        strategy={"kind": SHADOW_KIND, "aid": aid},
        status="active",
    )
    db.add(portfolio)
    db.flush()
    db.add(Position(portfolio_id=portfolio.id, secid=secid, weight=100))
    _audit(db, aid, "create", {"aid": aid, "secid": secid, "name": profile.name,
                               "modules": profile.modules, "shadow_pid": pid})
    db.commit()
    db.refresh(profile)

    secmap = pf_svc.validate_securities(db, [secid])
    pf_svc.materialize(portfolio, [(secid, Decimal("100"))], secmap, settings)
    return profile


def update_profile(
    db: Session,
    profile: AnalysisProfile,
    *,
    name: str | None,
    note: str | None,
    modules: dict[str, Any] | None,
) -> AnalysisProfile:
    if name is None and note is None and modules is None:
        raise AppError(ErrorCode.VALIDATION_ERROR, "无更新字段")
    changed: dict[str, Any] = {}
    if name is not None:
        profile.name = name
        changed["name"] = name
    if note is not None:
        profile.note = note
        changed["note"] = note
    if modules is not None:
        profile.modules = _module_flags(modules)
        changed["modules"] = profile.modules
    _audit(db, profile.aid, "update", changed)
    db.commit()
    db.refresh(profile)
    return profile


def archive_profile(db: Session, profile: AnalysisProfile) -> AnalysisProfile:
    """软删档案 + 级联归档影子组合（组合记录保留，回测产物仍可追溯）。"""
    profile.status = "archived"
    pf = db.execute(
        select(Portfolio).where(Portfolio.pid == shadow_pid(profile.aid))
    ).scalar_one_or_none()
    if pf is not None:
        pf.status = "archived"
    _audit(db, profile.aid, "delete", {"aid": profile.aid, "shadow_pid": shadow_pid(profile.aid)})
    db.commit()
    db.refresh(profile)
    return profile


def shadow_portfolio(db: Session, aid: str) -> Portfolio:
    pid = shadow_pid(aid)
    pf = db.execute(select(Portfolio).where(Portfolio.pid == pid)).scalar_one_or_none()
    if pf is None:
        raise AppError(ErrorCode.NOT_FOUND, f"影子组合不存在：{pid}")
    return pf


# ── 四模块分析 ──────────────────────────────────────────────────

def _ret(closes: list[float], n: int) -> float | None:
    if len(closes) <= n or not closes[-1 - n]:
        return None
    return round(closes[-1] / closes[-1 - n] - 1, 4)


def _ma(values: list[float], n: int) -> float | None:
    if len(values) < n:
        return None
    return sum(values[-n:]) / n


def _fundamental(repo: Path, secid: str, as_of: str) -> dict[str, Any]:
    fund = _load_json(repo / "factors" / "fundamental_values.json", {})
    reps = (fund.get(secid) or {}).get("reports") or []
    avail = [r for r in reps if (r.get("announce_date") or "") <= as_of]
    avail.sort(key=lambda r: r.get("period") or "")
    if not avail:
        return {"available": False, "note": "无可用财报（未拉取或标的无财报数据）"}

    def pick(r: dict) -> dict[str, Any]:
        return {
            "period": r.get("period"),
            "announce_date": r.get("announce_date"),
            "revenue": r.get("revenue"),
            "net_profit": r.get("net_profit"),
            "roe": r.get("roe"),
            "rev_growth": r.get("rev_g"),
            "np_growth": r.get("np_g"),
            "margin": r.get("margin"),
            "eps_ytd": r.get("eps_ytd"),
            "eps_ttm": r.get("eps_ttm"),
            "bps": r.get("bps"),
        }

    recent = [pick(r) for r in avail[-4:]]
    latest = recent[-1]
    return {
        "available": True,
        "as_of": as_of,
        "reports": recent,
        "latest": latest,
        "trend": {
            "roe": latest.get("roe"),
            "rev_growth": latest.get("rev_growth"),
            "np_growth": latest.get("np_growth"),
            "margin": latest.get("margin"),
            "roe_prev": recent[-2].get("roe") if len(recent) > 1 else None,
        },
        "periods_n": len(avail),
    }


def _industry(repo: Path, security: Security, bars: list[dict]) -> dict[str, Any]:
    index = _load_json(repo / "industry" / "industry_index.json", {})
    if not index:
        return {"available": False, "note": "行业指数数据未就绪（请先完成数据同步）"}

    label = security.industry or ""
    board = match_sw_l1(label)
    if board not in index:
        board = next((k for k in index if k and (k in label or (label and label in k))), None)
    if board is None:
        return {"available": False, "note": f"行业未归一（标注：{label or '无'}）",
                "available_boards": sorted(index)}

    rows = index[board]
    closes = [r["close"] for r in rows if r.get("close") is not None]
    amounts = [r["amount"] for r in rows if r.get("amount") is not None]
    stock_closes = [b["close"] for b in bars if b.get("close") is not None]
    stock_ret20 = _ret(stock_closes, 20)
    board_ret20 = _ret(closes, 20)
    ma5, ma60 = _ma(amounts, 5), _ma(amounts, 60)
    return {
        "available": True,
        "board": board,
        "label": label,
        "as_of": rows[-1]["dt"] if rows else None,
        "ret_20d": board_ret20,
        "ret_60d": _ret(closes, 60),
        "close": closes[-1] if closes else None,
        "amount_ma5": round(ma5, 2) if ma5 else None,
        "amount_ma60": round(ma60, 2) if ma60 else None,
        "amount_ratio_5_60": round(ma5 / ma60, 3) if (ma5 and ma60) else None,
        "stock_ret_20d": stock_ret20,
        "relative_strength_20d": (round(stock_ret20 - board_ret20, 4)
                                  if (stock_ret20 is not None and board_ret20 is not None) else None),
        "bars_n": len(rows),
    }


def _technical(bars: list[dict]) -> dict[str, Any]:
    if not bars:
        return {"available": False, "note": "无行情数据"}
    tech = oversold_score(bars[-250:])
    tech.update({"available": True, "as_of": bars[-1]["dt"], "close": bars[-1].get("close")})
    return tech


def _news(repo: Path, security: Security, limit: int = NEWS_LIMIT) -> dict[str, Any]:
    news = _load_json(repo / "news" / "stock_news.json", {})
    items = news.get(security.secid) or []
    if not items:
        note = ("该市场暂无公开新闻源（当前仅 A 股接入东财个股新闻）"
                if security.market != "A" else "暂无新闻（可尝试刷新）")
        return {"available": False, "note": note, "items": []}
    return {"available": True, "items": items[:limit], "count": len(items)}


def run_analysis_job(
    ws_root: Path,
    aid: str,
    params: dict[str, Any],
    job_id: str | None,
    progress: Callable[[int, str], None],
) -> dict[str, Any]:
    """分析 job：四模块 → artifact + runs(type="analysis")。"""
    with session_scope() as db:
        profile = get_or_404(db, aid)
        portfolio = shadow_portfolio(db, aid)
        security = db.execute(
            select(Security).where(Security.secid == profile.secid)
        ).scalar_one_or_none()
        if security is None:
            raise AppError(ErrorCode.SECURITY_NOT_FOUND, "标的不存在或未注册")
        portfolio_id = portfolio.id
        modules = _module_flags(profile.modules)
        secid, name = profile.secid, profile.name
        sec_payload = {
            "secid": security.secid, "name": security.name, "market": security.market,
            "asset_type": security.asset_type, "industry": security.industry,
            "currency": security.currency,
        }

    ctx = Ctx(root=Path(ws_root))
    repo = ctx.local_repo
    bars = (_load_json(repo / "market" / "daily_bars.json", {}) or {}).get(secid) or []
    as_of = bars[-1]["dt"] if bars else date.today().isoformat()

    out: dict[str, Any] = {"aid": aid, "secid": secid, "name": name, "as_of": as_of,
                           "security": sec_payload, "modules": {}}
    steps = [("fundamental", "基本面", lambda: _fundamental(repo, secid, as_of)),
             ("industry", "行业趋势", lambda: _industry(repo, security_stub(sec_payload), bars)),
             ("technical", "短线超卖", lambda: _technical(bars)),
             ("news", "最新消息", lambda: _news(repo, security_stub(sec_payload)))]
    total = max(1, sum(1 for k, _, _ in steps if modules.get(k)))
    done = 0
    for key, label, fn in steps:
        if not modules.get(key):
            out["modules"][key] = {"available": False, "note": "模块已关闭"}
            continue
        progress(int(10 + 70 * done / total), f"计算{label}")
        try:
            out["modules"][key] = fn()
        except Exception as exc:  # noqa: BLE001 - 单模块失败降级，不使 job 失败
            logger.exception("分析模块 %s 失败（降级）", key)
            out["modules"][key] = {"available": False, "note": f"模块计算失败：{exc}"}
        done += 1

    # artifact + runs
    progress(85, "写入产物")
    run_uid = new_run_id("run")
    art_dir = ctx.artifacts_dir / run_uid
    art_dir.mkdir(parents=True, exist_ok=True)
    (art_dir / "analysis_report.json").write_text(
        json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")
    files = {p.name: p for p in sorted(art_dir.glob("*")) if p.is_file()}
    keys = put_run_artifacts(run_uid, files)

    tech = out["modules"].get("technical") or {}
    ind = out["modules"].get("industry") or {}
    summary = {
        "aid": aid, "secid": secid, "name": name, "as_of": as_of,
        "oversold": tech.get("oversold"),
        "technical_score": tech.get("score"),
        "rsi": tech.get("rsi"),
        "industry_board": ind.get("board"),
        "industry_ret_20d": ind.get("ret_20d"),
        "news_count": (out["modules"].get("news") or {}).get("count", 0),
        "available_modules": [k for k, v in out["modules"].items() if (v or {}).get("available")],
    }
    progress(95, "写入运行记录")
    with session_scope() as db:
        db.add(Run(
            run_uid=run_uid, job_id=job_id, portfolio_id=portfolio_id,
            type="analysis", as_of=date.fromisoformat(as_of[:10]) if as_of else None,
            summary=summary, artifact_keys=keys,
        ))
    return {"run_uid": run_uid, "artifact_keys": keys, "summary": summary}


class security_stub:
    """轻量标的视图（避免跨事务持有 ORM 对象）。"""

    def __init__(self, payload: dict[str, Any]) -> None:
        self.secid = payload["secid"]
        self.name = payload.get("name")
        self.market = payload.get("market")
        self.asset_type = payload.get("asset_type")
        self.industry = payload.get("industry")
        self.currency = payload.get("currency")


def read_news(repo: Path, secid: str, limit: int = NEWS_LIMIT) -> dict[str, Any]:
    """直读新闻数据集（不跑 job）；无数据 → available=false + 原因。"""
    news = _load_json(repo / "news" / "stock_news.json", {})
    items = news.get(secid) or []
    if not items:
        return {"secid": secid, "available": False, "items": [],
                "note": "暂无新闻数据（可调用刷新接口或先完成数据同步）"}
    return {"secid": secid, "available": True, "items": items[:limit], "count": len(items)}
