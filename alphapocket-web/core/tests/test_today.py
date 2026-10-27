"""今日结论测试（2026-10-26 快速结论页：today 聚合 + 收盘自动评估调度决策）。

覆盖三层：
1. **纯函数对拍**（无 DB）：``fmt_pct`` / ``build_highlights`` / ``build_level``——
   highlight 分支（收益/信号三态/宏观/指数/数据风险/缺字段降级）逐一对拍；
2. **调度决策**（无 DB）：``parse_fire_time`` / ``is_trading_day`` / ``pending_pids``
   / ``phase`` + ``_tick_once`` 三路径（早退 / 先同步 / 后评估，模块函数打桩）；
3. **API 集成**（真实 PG，不可达整体 skip）：``GET /api/v1/today``——有种入
   review run 的组合出 highlights/level/fresh；无 run 的组合 ``stale`` 降级。

不修改 ``conftest.py``，本文件自带 fixture（同 ``test_review.py`` 模式）；
所有落库行用 ``test_td_`` 唯一前缀并在用例结束后按 FK 顺序清理。
"""
from __future__ import annotations

import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import date, datetime
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import delete, select, text

from app.core.config import Settings, get_settings
from app.db.session import session_scope
from app.models import Job, Portfolio, Run
from app.services import scheduler as sched_mod
from app.services.scheduler import (
    CloseReviewScheduler,
    is_trading_day,
    parse_fire_time,
    pending_pids,
    phase,
)
from app.services.today import build_highlights, build_level, build_today, fmt_pct

# ── 测试数据：冻结 summary（字段与 review._build_summary 对齐） ─────────


def _full_summary(
    *,
    day_ret: float = 0.012,
    above: int = 2,
    below: int = 1,
    neutral: int = 0,
    state: str = "easy_froth",
    data_risk=None,
) -> dict:
    return {
        "as_of": "2026-10-26",
        "portfolio_metrics": {
            "day_ret": day_ret,
            "ret_20d": 0.05,
            "ret_ytd": 0.10,
            "ret_1y": 0.20,
            "max_dd_1y": -0.08,
            "nav": 1.2,
            "method": "weighted",
        },
        "position_count": 3,
        "signal_count": 3,
        "signal_states": {"above": above, "below": below, "neutral": neutral},
        "macro_liquidity": {
            "markets": {"A": 1},
            "global": 1,
            "global_score": 0.6,
            "state": state,
            "as_of": "2026-10-26",
        },
        "index_trend": {
            "indexes": {
                "HS300": {
                    "position": "mid_zone",
                    "pos_q": 55.0,
                    "trend": "up",
                    "ret_20d": 0.03,
                    "close": 4000.0,
                    "name": "沪深300",
                    "market": "A",
                }
            },
            "as_of": "2026-10-26",
        },
        "data_risk": data_risk,
    }


# ── 1) 纯函数：fmt_pct / build_highlights / build_level ───────────────


@pytest.mark.parametrize(
    ("value", "expected"),
    [(None, "—"), ("abc", "—"), (0.012, "+1.20%"), (-0.008, "-0.80%"), (0, "+0.00%")],
)
def test_fmt_pct(value, expected):
    assert fmt_pct(value) == expected


def test_highlights_empty_summary():
    """无 summary → 单条「暂无评估」提示；level=stale。"""
    hs = build_highlights(None)
    assert len(hs) == 1
    assert "暂无评估" in hs[0]
    assert build_level(None) == "stale"


def test_highlights_bullish_full():
    """偏多 + 宽松偏热 + 指数中位上行：收益/信号/宏观/指数四类短句齐备。"""
    hs = build_highlights(_full_summary())
    joined = " | ".join(hs)
    assert "今日 +1.20%" in joined
    assert "近20日 +5.00%" in joined
    assert "信号偏多（2 上 / 1 下 / 0 中性），持仓可继续持有" in hs
    assert "宏观流动性：宽松偏热" in hs
    assert "指数：沪深300 中位↑上行" in hs
    assert build_level(_full_summary()) == "good"


def test_highlights_bearish():
    """偏空：信号建议减仓/对冲；level=bad。"""
    hs = build_highlights(_full_summary(day_ret=-0.01, above=0, below=3))
    assert any("信号偏空" in h and "关注减仓" in h for h in hs)
    assert build_level(_full_summary(day_ret=-0.01, above=0, below=3)) == "bad"


def test_highlights_balanced():
    """均衡：维持现有仓位；level=flat。"""
    hs = build_highlights(_full_summary(day_ret=0.0, above=1, below=1))
    assert any("信号均衡" in h and "维持现有仓位" in h for h in hs)
    assert build_level(_full_summary(day_ret=0.0, above=1, below=1)) == "flat"


def test_highlights_no_signals():
    """信号全零：提示补数据而非误判倾向。"""
    hs = build_highlights(_full_summary(above=0, below=0, neutral=0))
    assert any("暂无有效信号" in h for h in hs)


def test_highlights_data_risk_first():
    """数据风险排第一且 level=warn（黄牌优先于收益倾向）。"""
    hs = build_highlights(_full_summary(data_risk="golden_test: fail (diff>0.5%)"))
    assert hs[0].startswith("数据验证未全绿")
    assert build_level(_full_summary(data_risk="x")) == "warn"


def test_highlights_degraded_blocks():
    """宏观/指数 unavailable、metrics 缺失：对应短句跳过，不抛错。"""
    summary = _full_summary()
    summary["macro_liquidity"] = {"status": "unavailable", "error": "no data"}
    summary["index_trend"] = {"status": "unavailable", "error": "no data"}
    summary["portfolio_metrics"] = {}
    hs = build_highlights(summary)
    joined = " | ".join(hs)
    assert "宏观流动性" not in joined
    assert "指数：" not in joined
    assert "今日 —" in joined                      # metrics 缺失降级为占位
    assert build_level(summary) == "flat"


def test_highlights_index_degraded():
    """引擎D 无 active 指数（unavailable / 空 indexes）→ 无指数短句。"""
    for it in ({"status": "unavailable", "error": "x"}, {"indexes": {}}):
        summary = _full_summary()
        summary["index_trend"] = it
        assert all(not h.startswith("指数：") for h in build_highlights(summary))


# ── 2) 调度决策纯函数 ────────────────────────────────────────────────


def test_parse_fire_time():
    assert parse_fire_time("16:30") == (16, 30)
    assert parse_fire_time(" 09:05 ") == (9, 5)
    assert parse_fire_time("25:99") == (23, 59)     # 非法 → 23:59 兜底（当日不触发）
    assert parse_fire_time("abc") == (23, 59)
    assert parse_fire_time("") == (23, 59)


def test_is_trading_day(tmp_path: Path):
    ws = tmp_path / "ws"
    ws.mkdir()
    today = date(2026, 10, 26)
    # 文件缺失 → 退化 True
    assert is_trading_day(ws, today) is True
    cal = ws / "remote" / "repo" / "market" / "trade_calendar.json"
    cal.parent.mkdir(parents=True)
    # dict 形态：当日有记录
    cal.write_text('{"2026-10-26": 1, "2026-10-27": 0}', encoding="utf-8")
    assert is_trading_day(ws, today) is True
    assert is_trading_day(ws, date(2026, 10, 27)) is False
    # 日历未覆盖 → 退化 True（不阻断）
    assert is_trading_day(ws, date(2030, 1, 1)) is True
    # list 形态
    cal.write_text('["2026-10-26"]', encoding="utf-8")
    assert is_trading_day(ws, today) is True
    assert is_trading_day(ws, date(2026, 10, 27)) is False
    # 损坏 JSON → 退化 True
    cal.write_text("not-json", encoding="utf-8")
    assert is_trading_day(ws, today) is True


def test_pending_pids():
    """未到时点 / 非交易日 / 全部已跑 → 空；部分已跑 → 差集。"""
    pfs = [(1, "cn4"), (2, "hk1")]
    before = datetime(2026, 10, 26, 10, 0)
    fire = (16, 30)
    assert pending_pids(before, fire, True, pfs, set()) == []
    after = datetime(2026, 10, 26, 17, 0)
    assert pending_pids(after, fire, False, pfs, set()) == []
    assert pending_pids(after, fire, True, pfs, {1}) == ["hk1"]
    assert pending_pids(after, fire, True, pfs, {1, 2}) == []
    assert pending_pids(after, fire, True, [], {1}) == []


def test_phase():
    """两阶段：None→sync；queued/running→wait；done/failed→review。"""
    assert phase(None) == "sync"
    assert phase("queued") == "wait"
    assert phase("running") == "wait"
    assert phase("done") == "review"
    assert phase("failed") == "review"


# ── 3) _tick_once 三路径（模块函数打桩，无真实 DB/runner） ───────────────


class _FakeResult:
    def __init__(self, rows):
        self._rows = rows

    def all(self):
        return list(self._rows)


class _FakeDB:
    """仅服务 ``_tick_once`` 的 portfolios 查询（``.execute().all()``）。"""

    def __init__(self, portfolios):
        self._portfolios = portfolios

    def execute(self, stmt):  # noqa: ARG002 - stub
        return _FakeResult(self._portfolios)


def _make_sched(tmp_path: Path, fire: str) -> CloseReviewScheduler:
    settings = Settings(auto_review_time=fire, workspace_root=str(tmp_path))
    return CloseReviewScheduler(settings, runner=object(), ws_root=tmp_path)


def _patch_tick(
    monkeypatch,
    *,
    now: datetime,
    sync_status: str | None,
    portfolios: list,
    done_ids: set,
) -> dict:
    """打桩 ``scheduler`` 模块的时钟/DB/同步状态/提交，返回调用记录。"""
    calls = {"sync": 0, "review": [], "queries": 0}

    class _FixedClock:
        """占住模块名 ``datetime``：``datetime.now()`` 返回打桩时间。"""

        @staticmethod
        def now():
            return now

    monkeypatch.setattr(sched_mod, "datetime", _FixedClock)
    monkeypatch.setattr(
        sched_mod, "is_trading_day", lambda ws_root, today: True
    )

    @contextmanager
    def fake_session():
        calls["queries"] += 1
        yield _FakeDB(portfolios)

    monkeypatch.setattr(sched_mod, "session_scope", fake_session)
    monkeypatch.setattr(
        sched_mod, "close_auto_sync_status", lambda db, today: sync_status
    )
    monkeypatch.setattr(
        sched_mod, "closed_auto_job_ids", lambda db, today: set(done_ids)
    )

    def fake_submit_sync(runner, today):
        calls["sync"] += 1
        return "job_sync"

    def fake_submit_one(runner, portfolio_id, pid, today):
        calls["review"].append((portfolio_id, pid))
        return f"job_{pid}"

    monkeypatch.setattr(sched_mod, "submit_sync", fake_submit_sync)
    monkeypatch.setattr(sched_mod, "submit_one", fake_submit_one)
    return calls


def test_tick_before_fire_time(tmp_path, monkeypatch):
    """未到触发时点：不查库、不提交（两阶段都不启动）。"""
    sched = _make_sched(tmp_path, fire="23:58")
    calls = _patch_tick(
        monkeypatch,
        now=datetime(2026, 10, 26, 23, 0),
        sync_status=None,
        portfolios=[(1, "cn4")],
        done_ids=set(),
    )
    sched._tick_once()
    assert calls == {"sync": 0, "review": [], "queries": 0}


def test_tick_non_trading_day(tmp_path, monkeypatch):
    """非交易日：直接早退。"""
    sched = _make_sched(tmp_path, fire="00:00")
    calls = _patch_tick(
        monkeypatch,
        now=datetime(2026, 10, 26, 17, 0),
        sync_status=None,
        portfolios=[(1, "cn4")],
        done_ids=set(),
    )
    monkeypatch.setattr(sched_mod, "is_trading_day", lambda ws_root, today: False)
    sched._tick_once()
    assert calls == {"sync": 0, "review": [], "queries": 0}


def test_tick_sync_phase(tmp_path, monkeypatch):
    """今日尚无 close_auto 同步：提交同步 job，本轮不提交评估。"""
    sched = _make_sched(tmp_path, fire="16:30")
    calls = _patch_tick(
        monkeypatch,
        now=datetime(2026, 10, 26, 17, 0),
        sync_status=None,                    # phase → "sync"
        portfolios=[(1, "cn4"), (2, "hk1")],
        done_ids=set(),
    )
    sched._tick_once()
    assert calls["sync"] == 1
    assert calls["review"] == []


def test_tick_wait_sync_running(tmp_path, monkeypatch):
    """同步 running 中：只轮询，不重复提交、不进入评估。"""
    sched = _make_sched(tmp_path, fire="16:30")
    calls = _patch_tick(
        monkeypatch,
        now=datetime(2026, 10, 26, 17, 0),
        sync_status="running",               # phase → "wait"
        portfolios=[(1, "cn4")],
        done_ids=set(),
    )
    sched._tick_once()
    assert calls["sync"] == 0
    assert calls["review"] == []


def test_tick_review_phase_dedup(tmp_path, monkeypatch):
    """同步 done 后进入评估：只提交「active − 今日已跑」差集组合。"""
    sched = _make_sched(tmp_path, fire="16:30")
    calls = _patch_tick(
        monkeypatch,
        now=datetime(2026, 10, 26, 17, 0),
        sync_status="done",                 # phase → "review"
        portfolios=[(1, "cn4"), (2, "hk1"), (3, "us1")],
        done_ids={1, 3},
    )
    sched._tick_once()
    assert calls["sync"] == 0
    assert calls["review"] == [(2, "hk1")]


def test_tick_sync_failed_still_reviews(tmp_path, monkeypatch):
    """同步 failed 不阻断评估（宁可按既有数据出结论）。"""
    sched = _make_sched(tmp_path, fire="16:30")
    calls = _patch_tick(
        monkeypatch,
        now=datetime(2026, 10, 26, 17, 0),
        sync_status="failed",               # phase → "review"
        portfolios=[(1, "cn4")],
        done_ids=set(),
    )
    sched._tick_once()
    assert calls["review"] == [(1, "cn4")]


# ── 4) API 集成：GET /api/v1/today（真实 PG，不可达整体 skip） ───────────


@pytest.fixture(scope="session")
def pg_ready():
    from app.db.session import engine

    try:
        with engine.connect() as conn:
            conn.execute(text("select 1"))
    except Exception as exc:  # noqa: BLE001 - 探测失败即跳过
        pytest.skip(f"PostgreSQL 不可达：{type(exc).__name__}: {exc}")
    yield engine


@pytest.fixture
def ws_root(tmp_path, monkeypatch) -> Iterator[Path]:
    root = tmp_path / "ws"
    monkeypatch.setenv("AP_WORKSPACE_ROOT", str(root))
    get_settings.cache_clear()
    yield root
    get_settings.cache_clear()


@pytest.fixture
def api_client(ws_root, pg_ready) -> Iterator[TestClient]:
    from app.main import create_app

    with TestClient(create_app()) as client:
        yield client
    get_settings.cache_clear()


@pytest.fixture
def cleanup(pg_ready) -> Iterator[dict]:
    """收集本用例落库的 run/job/pid，结束后按 FK 顺序清理。"""
    state: dict = {"run_uids": [], "job_ids": [], "pids": []}
    yield state
    with session_scope() as db:
        if state["run_uids"]:
            db.execute(delete(Run).where(Run.run_uid.in_(state["run_uids"])))
        if state["job_ids"]:
            db.execute(delete(Job).where(Job.id.in_(state["job_ids"])))
        if state["pids"]:
            ids = [
                row
                for (row,) in db.execute(
                    select(Portfolio.id).where(Portfolio.pid.in_(state["pids"]))
                ).all()
            ]
            if ids:
                db.execute(delete(Portfolio).where(Portfolio.id.in_(ids)))


def _seed_portfolio_with_run(cleanup: dict, summary: dict | None, as_of: date | None):
    """种入 Portfolio（active）+ 可选 review Job/Run，返回 pid。"""
    pid = f"test_td_{uuid.uuid4().hex[:8]}"
    with session_scope() as db:
        pf = Portfolio(pid=pid, name=f"今日结论测试-{pid[-4:]}")
        db.add(pf)
        db.flush()
        cleanup["pids"].append(pid)
        if summary is not None:
            job_id = f"job_td_{uuid.uuid4().hex[:8]}"
            db.add(
                Job(id=job_id, type="review", portfolio_id=pf.id,
                    status="done", params={"pid": pid}, progress=100)
            )
            run_uid = f"run_td_{uuid.uuid4().hex[:8]}"
            db.add(
                Run(run_uid=run_uid, job_id=job_id, portfolio_id=pf.id,
                    type="review", as_of=as_of, summary=summary, artifact_keys=[])
            )
            cleanup["job_ids"].append(job_id)
            cleanup["run_uids"].append(run_uid)
    return pid


def test_today_api(api_client, cleanup):
    """GET /today：server_time + fresh 组合 highlights + stale 组合降级。"""
    today = date.today()
    with_run = _seed_portfolio_with_run(cleanup, _full_summary(), today)
    without_run = _seed_portfolio_with_run(cleanup, None, None)

    resp = api_client.get("/api/v1/today")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert set(body) >= {"server_time", "date", "generated_at", "auto_review", "portfolios"}
    assert body["date"] == today.isoformat()
    assert body["auto_review"] == {"enabled": False}     # 测试环境默认关闭

    by_pid = {p["pid"]: p for p in body["portfolios"]}
    assert set(by_pid) >= {with_run, without_run}

    item = by_pid[with_run]
    assert item["fresh"] is True
    assert item["as_of"] == today.isoformat()
    assert item["level"] == "good"
    assert item["run_uid"].startswith("run_td_")
    assert any("持仓可继续持有" in h for h in item["highlights"])
    assert item["metrics"]["day_ret"] == 0.012
    assert item["signal_states"] == {"above": 2, "below": 1, "neutral": 0}

    stale = by_pid[without_run]
    assert stale["fresh"] is False
    assert stale["level"] == "stale"
    assert any("暂无评估" in h for h in stale["highlights"])


def test_today_api_not_fresh(api_client, cleanup):
    """结论日期早于今天：fresh=False（收盘自动评估尚未跑当日）。"""
    pid = _seed_portfolio_with_run(cleanup, _full_summary(), date(2026, 1, 1))
    resp = api_client.get("/api/v1/today")
    assert resp.status_code == 200, resp.text
    item = next(p for p in resp.json()["portfolios"] if p["pid"] == pid)
    assert item["fresh"] is False
    assert item["as_of"] == "2026-01-01"


def test_build_today_direct(cleanup, pg_ready):
    """build_today 直调：服务器时间格式 + 数据风险 warn 短句透传。"""
    today = date.today()
    pid = _seed_portfolio_with_run(
        cleanup, _full_summary(data_risk="golden_test: fail"), today
    )
    with session_scope() as db:
        payload = build_today(db, get_settings(), datetime.now())
    item = next(p for p in payload["portfolios"] if p["pid"] == pid)
    assert item["level"] == "warn"
    assert item["highlights"][0].startswith("数据验证未全绿")
    assert payload["server_time"].startswith(today.isoformat())


def test_settings_auto_review_defaults(monkeypatch):
    """默认关闭（测试/离线零副作用）；显式开/关可覆盖。"""
    monkeypatch.delenv("AP_AUTO_REVIEW", raising=False)
    monkeypatch.delenv("AP_AUTO_REVIEW_TIME", raising=False)
    s = Settings()
    assert s.auto_review_enabled is False
    assert s.auto_review_time == "16:30"
    on = Settings(auto_review_enabled=True, auto_review_time="17:05")
    assert on.auto_review_enabled is True
    assert on.auto_review_time == "17:05"
