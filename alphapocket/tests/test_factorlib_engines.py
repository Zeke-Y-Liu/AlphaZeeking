"""M1 三引擎测试：SQL（PIT 模板）、qlib_expr（解析/翻译/窗口历史）、python_udf（纯函数守卫）。

运行：``cd alphapocket && python -m pytest tests/test_factorlib_engines.py -q``
"""
from __future__ import annotations

from datetime import date

import pytest

from factorlib.engine import EngineError, get_engine
from factorlib.engine.python_udf import PurityViolation, check_purity
from factorlib.engine.qlib_expr import _Parser, _render
from factorlib.registry import load_factor


def _rows_by_dt(rows):
    out = {}
    for r in rows:
        out.setdefault(str(r["dt"]), {})[r["secid"]] = r
    return out


# ── SQL 引擎：val_pe_ttm（PIT 模板）─────────────────────
def test_sql_engine_golden_spot(fl_db, golden):
    db, _ = fl_db
    meta = load_factor(_repo_of(db), "val_pe_ttm")
    rows = get_engine(meta).run(db, date(2021, 1, 4), date(2021, 6, 30))
    by_dt = _rows_by_dt(rows)
    for case in golden["cases"]:
        r = by_dt[case["dt"]][case["secid"]]
        assert r["value"] == pytest.approx(case["expected"], rel=1e-9)
        assert r["value"] == pytest.approx(case["close"] / case["ttm_eps"], rel=1e-9)  # 独立算术
        assert str(r["used_period"]) == case["used_period"]
        assert str(r["used_announce"]) == case["used_announce"]
        assert r["used_announce"] <= r["dt"]               # PIT 红线：公告日不晚于取值日


def _repo_of(db):
    return db.parent / "repo"


def test_sql_engine_loss_making_absent(fl_db):
    """S07 自 2021-05-28（Q1'21 公告）起 ttm_eps = -0.15 → PE 缺席。"""
    db, _ = fl_db
    meta = load_factor(_repo_of(db), "val_pe_ttm")
    rows = get_engine(meta).run(db, date(2021, 1, 4), date(2021, 6, 30))
    s07 = {str(r["dt"]) for r in rows if r["secid"] == "600007.SH" and r["value"] is not None}
    assert "2021-05-27" in s07 and not any(d >= "2021-05-28" for d in s07)


def test_sql_engine_requires_output_columns(fl_db):
    db, _ = fl_db
    meta = load_factor(_repo_of(db), "val_pe_ttm")
    bad = type(meta)(**{**meta.to_dict(),
                        "sql": "SELECT dt, secid FROM fl_daily_bar "
                               "WHERE dt BETWEEN $start AND $end",
                        "source_path": None})
    with pytest.raises(EngineError, match="缺少列"):
        get_engine(bad).run(db, date(2021, 4, 29), date(2021, 4, 29))


# ── qlib_expr 引擎 ────────────────────────────────────
def test_qlib_parser_and_render():
    node = _Parser("$close/Ref($close,20)-1").parse()
    sql = _render(node)
    assert "lag(close, 20) OVER (PARTITION BY secid ORDER BY dt)" in sql


def test_qlib_parser_rejects_bad_input():
    for bad in ("$close/", "$oops", "Mean($close,2)+", "($close", "Ref($close,20"):
        with pytest.raises(EngineError):
            _Parser(bad).parse()


def test_qlib_render_rejects_bad_semantics():
    for bad in ("Foo($close,1)", "Mean(Ref($close,1),2)", "Mean($close,0)", "Corr($close,$volume)"):
        with pytest.raises(EngineError):
            _render(_Parser(bad).parse())


def test_qlib_rejects_window_nesting():
    with pytest.raises(EngineError, match="窗口嵌套"):
        _render(_Parser("Mean(Ref($close,1),2)").parse())


def test_qlib_unknown_function():
    with pytest.raises(EngineError, match="未知函数"):
        _render(_Parser("Corr($close,$volume)").parse())


def test_qlib_mom_first_value_and_value(fl_db):
    """Ref 20：前 20 个交易日无值；首值日 2021-02-01。
    CP1（01-29）已跳档 → 02-01 的 Ref 落在 01-04（上一平台）：值 = (9.5+i)/(9+i) - 1。"""
    db, _ = fl_db
    meta = load_factor(_repo_of(db), "mom_20d_fl")
    rows = get_engine(meta).run(db, date(2021, 1, 4), date(2021, 6, 30))
    dts = {str(r["dt"]) for r in rows if r["value"] is not None}
    assert not any(d < "2021-02-01" for d in dts)
    first = {r["secid"]: r["value"] for r in rows
             if str(r["dt"]) == "2021-02-01" and r["value"] is not None}
    assert len(first) == 10
    for i in range(1, 11):
        assert first[f"6000{i:02d}.SH"] == pytest.approx((9.5 + i) / (9 + i) - 1)
    # 02-26 是 CP2 跳档日：close = 10+i，Ref20 落在 01-29（CP1 平台 9.5+i）→ 跳档算术
    jump = {r["secid"]: r["value"] for r in rows
            if str(r["dt"]) == "2021-02-26" and r["value"] is not None}
    assert len(jump) == 10
    for i in range(1, 11):
        assert jump[f"6000{i:02d}.SH"] == pytest.approx((10 + i) / (9.5 + i) - 1)
    # 06-30 深处平台 4（Ref20 = 06-02，同为平台 4）→ 恰为 0.0
    flat = [r["value"] for r in rows
            if str(r["dt"]) == "2021-06-30" and r["value"] is not None]
    assert len(flat) == 10 and all(v == pytest.approx(0.0, abs=1e-12) for v in flat)


def test_qlib_window_sees_pre_start_history(fl_db):
    """历史截断修复：区间仅 5 日（2021-02-01..05），Ref(20) 仍取到 1 月 K 线 → 无 NULL 截断。"""
    db, _ = fl_db
    meta = load_factor(_repo_of(db), "mom_20d_fl")
    rows = get_engine(meta).run(db, date(2021, 2, 1), date(2021, 2, 5))
    assert len(rows) == 50                                  # 10 股 × 5 日，全部有值
    for r in rows:
        i = int(r["secid"][4:6])
        assert r["value"] == pytest.approx((9.5 + i) / (9 + i) - 1)


def test_qlib_signal_rank_range(fl_db):
    db, _ = fl_db
    meta = load_factor(_repo_of(db), "sig_mom20_rank_fl")
    rows = get_engine(meta).run(db, date(2021, 2, 1), date(2021, 6, 30))
    assert rows and all(0.0 <= r["value"] <= 1.0 for r in rows)


# ── python_udf 引擎 ───────────────────────────────────
def test_udf_purity_guard():
    check_purity("def compute(a, b, c, d):\n    return None\n")
    for bad in ("import socket\n", "from urllib.request import urlopen\n",
                "import random\n", "import duckdb\n",
                "def compute(a, b, c, d):\n    return open('x')\n",
                "def compute(a, b, c, d):\n    return eval('1')\n",
                "def compute(a, b, c, d):\n    return exec('1')\n",
                "def compute(a, b, c, d):\n    return __import__('os')\n"):
        with pytest.raises(PurityViolation):
            check_purity(bad)


def test_udf_engine_rejects_missing_compute():
    from factorlib.registry import FactorMeta
    m = FactorMeta(id="x_udf", name="x", category="F2_fundamental", factor_type="alpha",
                   engine="python_udf", direction="high", universe=["CN"], inputs=["t"],
                   lookback_days=0, pit=True, version="1", status="draft", description="d",
                   udf="def other():\n    return 1\n")
    with pytest.raises(EngineError, match="compute"):
        get_engine(m)


def test_udf_eps_revise_golden_spot(fl_db, golden):
    """PIT 在 UDF 内自持：晚公告组 2021-04-30 仍用年报（修正值 0.0），早公告组已切 Q1（0.10）。"""
    db, _ = fl_db
    meta = load_factor(_repo_of(db), "eps_revise_fl")
    rows = get_engine(meta).run(db, date(2021, 1, 4), date(2021, 6, 30))
    by_dt = _rows_by_dt(rows)
    r = by_dt["2021-04-30"]["600001.SH"]
    assert r["value"] == pytest.approx(0.35 - 0.25)         # Q1'21 vs Q1'20 eps_ytd
    assert str(r["used_period"]) == "2021-03-31"
    assert str(r["used_announce"]) == "2021-04-30"
    r6 = by_dt["2021-04-30"]["600006.SH"]
    assert r6["value"] == pytest.approx(0.0)                # 年报 vs 去年年报
    assert str(r6["used_period"]) == "2020-12-31"
    # CP1：全部为 0.00（2020Q3 vs 2019Q3 同值）
    assert all(by_dt["2021-01-29"][s]["value"] == pytest.approx(0.0)
               for s in by_dt["2021-01-29"])


def test_udf_pit_self_enforced(fl_db):
    """UDF 输出的 used_announce 全部 <= dt（审计主检查在数据面成立的先决条件）。"""
    db, _ = fl_db
    meta = load_factor(_repo_of(db), "eps_revise_fl")
    rows = get_engine(meta).run(db, date(2021, 1, 4), date(2021, 6, 30))
    assert all(r["used_announce"] <= r["dt"] for r in rows)


# ── 引擎路由 ──────────────────────────────────────────
def test_get_engine_routes_by_meta(fl_repo):
    m = load_factor(fl_repo, "val_pe_ttm")
    assert type(get_engine(m)).__name__ == "SqlEngine"
    m = load_factor(fl_repo, "mom_20d_fl")
    assert type(get_engine(m)).__name__ == "QlibExprEngine"
    m = load_factor(fl_repo, "eps_revise_fl")
    assert type(get_engine(m)).__name__ == "UdfEngine"
    m.engine = "excel"
    with pytest.raises(EngineError, match="未知引擎"):
        get_engine(m)
