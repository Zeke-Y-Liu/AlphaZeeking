"""数据状态与验证结果查询服务（T1.5，§6.2 / §9.2；V4-β 多市场扩展）。

职责
----
为 ``GET /data/status`` 与 ``GET /data/verifications`` 提供纯查询逻辑：

- ``load_last_bar_dates``：读 workspace 各标的最后 bar 日期（依次尝试工具实读位
  ``<ws>/sandbox/repo/market/daily_bars.json`` 与 ``fetch_all`` 写入位
  ``<ws>/remote/repo/market/daily_bars.json``）；文件缺失/损坏返回 ``{}``，绝不抛异常。
- ``build_status``：universe 汇总（计数/类型/市场）、逐标的最后 bar / 缺口 / 距今、
  最近六项验证汇总（复用 T1.6 ``latest_verify_summary``）与最近一次 ``data_sync`` job。
- ``build_verifications``：验证结果列表（含冻结 detail 结构）。

V4-β 多市场口径：三市场交易日历不同（A股 新浪日历；港股/美股 由本市场 K 线日期并集
推导，见 ``trade_calendar_hk/us.json``）→ ``as_of``/``missing``/``lag_days`` **按市场分组
计算**（A 股日期不计入美股缺口，反之亦然）；日历文件缺失时该市场回退全局日期并集
（保持旧行为）。securities 条目新增 ``market``/``currency``/``hk_connect``。

**只读**：本模块不写库、不改契约、不新增错误码。bars 缺失时 securities 各项降级为
``None``/``False``，但接口仍 200。
"""
from __future__ import annotations

import json
from datetime import date
from pathlib import Path
from typing import Any

from sqlalchemy import select

from app.models import Job, Security, VerifyResult
from app.services.verification import latest_verify_summary

_DAILY_BARS_REL = Path("market") / "daily_bars.json"
# 分市场日历（港股/美股由 K 线日期并集推导；见 applib.datasource.fetch_all ③）
_MARKET_CAL_REL = {"A": "trade_calendar.json", "HK": "trade_calendar_hk.json", "US": "trade_calendar_us.json"}


def _market_of(secid: str) -> str:
    """secid → 市场（A/HK/US）；无法识别按 A（与 applib 口径一致）。"""
    s = (secid or "").strip().upper()
    if s.endswith(".HK"):
        return "HK"
    if s.endswith(".US"):
        return "US"
    return "A"


def _bars_paths(ws_root: str | Path) -> tuple[Path, Path]:
    """候选 daily_bars.json 路径：工具实读位优先，``remote/repo`` 回退。"""
    root = Path(ws_root)
    return (root / "sandbox" / "repo" / _DAILY_BARS_REL, root / "remote" / "repo" / _DAILY_BARS_REL)


def _dates_of(bars: Any) -> set[str]:
    """从单标的 bars 列表取全部 ``dt``（字符串化，用于集合比较）。"""
    out: set[str] = set()
    if not isinstance(bars, list):
        return out
    for row in bars:
        if isinstance(row, dict):
            dt = row.get("dt")
            if dt:
                out.add(str(dt))
    return out


def _load_bars(ws_root: str | Path) -> dict[str, Any]:
    """定位并解析 daily_bars.json；文件缺失/损坏/无非空 dt 返回 ``{}``。"""
    for path in _bars_paths(ws_root):
        if not path.exists():
            continue
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if not isinstance(data, dict):
            continue
        if any(_dates_of(bars) for bars in data.values()):
            return data
    return {}


def _parse_iso_date(value: str | None) -> date | None:
    """尽力解析 ``YYYY-MM-DD``；失败返回 None（绝不抛异常）。"""
    if not value:
        return None
    try:
        return date.fromisoformat(str(value)[:10])
    except ValueError:
        return None


def load_last_bar_dates(ws_root: str | Path) -> dict[str, str]:
    """每个 secid 的最后 bar 日期（``max(dt)``）；缺失/损坏返回 ``{}``。"""
    try:
        data = _load_bars(ws_root)
    except Exception:  # noqa: BLE001 - 读盘异常一律降级为空
        return {}
    out: dict[str, str] = {}
    for secid, bars in data.items():
        dates = _dates_of(bars)
        if dates:
            out[secid] = max(dates)
    return out


def _latest_sync(db: Any) -> dict[str, Any] | None:
    """最近一次 ``type == "data_sync"`` 的 job（按 created_at 倒序第一条）。"""
    job = (
        db.execute(
            select(Job)
            .where(Job.type == "data_sync")
            .order_by(Job.created_at.desc())
            .limit(1)
        )
        .scalars()
        .first()
    )
    if job is None:
        return None
    return {
        "job_id": job.id,
        "status": job.status,
        "progress": job.progress,
        "finished_at": job.finished_at.isoformat() if job.finished_at is not None else None,
    }


def _load_market_calendars(ws_root: str | Path) -> dict[str, set[str] | None]:
    """分市场交易日历：``{market: 日期集合 or None(文件缺失)}``。

    文件缺失（如该市场尚未同步）返回 None，调用方回退全局日期并集（保持旧行为）。
    """
    root = Path(ws_root)
    out: dict[str, set[str] | None] = {}
    for mkt, rel in _MARKET_CAL_REL.items():
        found: set[str] | None = None
        for base in (root / "sandbox" / "repo", root / "remote" / "repo"):
            path = base / "market" / rel
            if not path.exists():
                continue
            try:
                cal = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            if isinstance(cal, list) and cal:
                found = {str(d)[:10] for d in cal if d}
                break
        out[mkt] = found
    return out


def build_status(db: Any, ws_root: str | Path) -> dict[str, Any]:
    """聚合数据状态（§6.2 ``GET /data/status``；V4-β 按市场分组）。

    - ``as_of``：universe 最新 bar 日期（全局最大；分市场口径见 ``as_of_by_market``）；
    - ``universe``：``{"count", "types": {STOCK, ETF, LOF}, "markets": {A, HK, US}}``；
    - ``securities``：逐标的 market / currency / hk_connect / last_bar / lag_days /
      missing / has_bars（缺口按本市场日历计）；
    - ``verification``：``latest_verify_summary(db)`` 原样；
    - ``latest_sync``：最近 ``data_sync`` job 摘要或 None。
    """
    data = _load_bars(ws_root)
    per_secid: dict[str, set[str]] = {secid: _dates_of(bars) for secid, bars in data.items()}
    all_dates: set[str] = set()
    for dates in per_secid.values():
        all_dates |= dates
    as_of = max(all_dates) if all_dates else None
    mkt_cals = _load_market_calendars(ws_root)

    # 分市场日期集合：日历文件在 → 用日历（**过滤到今天**：日历是含未来交易日的计划表，
    # as_of/缺口口径须以已发生的交易日为界）；缺失 → 该市场 bars 日期并集（再回退全局并集）
    today_str = date.today().isoformat()
    dates_by_mkt: dict[str, set[str]] = {}
    for mkt in ("A", "HK", "US"):
        if mkt_cals.get(mkt):
            dates_by_mkt[mkt] = {d for d in mkt_cals[mkt] if d <= today_str}   # type: ignore[index]
        else:
            union = {dt for s, dts in per_secid.items() if _market_of(s) == mkt for dt in dts}
            dates_by_mkt[mkt] = union or all_dates
    as_of_by_market = {m: (max(d) if d else None) for m, d in dates_by_mkt.items()}

    securities = db.execute(select(Security).order_by(Security.secid)).scalars().all()

    types = {"STOCK": 0, "ETF": 0, "LOF": 0}
    markets = {"A": 0, "HK": 0, "US": 0}
    items: list[dict[str, Any]] = []
    for sec in securities:
        if sec.asset_type in types:
            types[sec.asset_type] += 1
        mkt = sec.market if sec.market in markets else "A"
        markets[mkt] += 1
        own = per_secid.get(sec.secid, set())
        last_bar = max(own) if own else None
        mkt_dates = dates_by_mkt.get(mkt, all_dates)
        mkt_as_of = as_of_by_market.get(mkt) or as_of
        lag_days: int | None = None
        missing: int | None = None
        if last_bar is not None:
            mkt_as_of_date = _parse_iso_date(mkt_as_of)
            last_bar_date = _parse_iso_date(last_bar)
            if mkt_as_of_date is not None and last_bar_date is not None:
                lag_days = (mkt_as_of_date - last_bar_date).days
            missing = sum(
                1
                for dt in mkt_dates
                if dt > last_bar and (mkt_as_of is None or dt <= mkt_as_of) and dt not in own
            )
        items.append(
            {
                "secid": sec.secid,
                "code": sec.code,
                "name": sec.name,
                "market": mkt,
                "asset_type": sec.asset_type,
                "currency": sec.currency,
                "hk_connect": sec.hk_connect,
                "last_bar": last_bar,
                "lag_days": lag_days,
                "missing": missing,
                "has_bars": last_bar is not None,
            }
        )

    return {
        "as_of": as_of,
        "as_of_by_market": as_of_by_market,
        "universe": {"count": len(securities), "types": types, "markets": markets},
        "securities": items,
        "verification": latest_verify_summary(db),
        "latest_sync": _latest_sync(db),
    }


def build_verifications(db: Any, limit: int) -> list[dict[str, Any]]:
    """验证结果列表（§6.2 ``GET /data/verifications?limit=``）。

    按 ``created_at`` desc、``id`` desc 排序取 ``limit`` 条；``detail`` 为冻结结构。
    """
    rows = (
        db.execute(
            select(VerifyResult)
            .order_by(VerifyResult.created_at.desc(), VerifyResult.id.desc())
            .limit(limit)
        )
        .scalars()
        .all()
    )
    return [
        {
            "job_id": row.job_id,
            "check": row.check,
            "status": row.status,
            "detail": row.detail,
            "created_at": row.created_at.isoformat() if row.created_at is not None else None,
        }
        for row in rows
    ]
