"""三引擎单测：SQL（契约+PIT 语义）、python_udf（纯函数约束）、qlib_expr（子集算例）。

合成库 PIT 手工推演（见 conftest）：
- S1：01-06 同日公告 FY2020(2.0)+2021Q1(0.6) → 取最新 period → TTM=0.6−0.5+2.0=2.1；
- S2：01-06..01-08 TTM=4.0；04-05 起 TTM=1.0−0.8+4.0=4.2；
- S3：EPS=-1.0 → 置空无行。
"""
import pytest

from conftest import CLOSES, DTs, make_meta, sql_from_yaml
from factorlib.engine import EngineError, run_factor
from factorlib.engine import udf_engine, qlib_expr
from factorlib.engine.udf_engine import UdfViolation

# 手工期望：PE = close / TTM_EPS（None = 无行）
EXPECT = {
    "2021-01-04": {"S1": None, "S2": None},
    "2021-01-05": {"S1": None, "S2": None},
    "2021-01-06": {"S1": 12.0 / 2.1, "S2": 22.0 / 4.0},
    "2021-01-07": {"S1": 13.0 / 2.1, "S2": 23.0 / 4.0},
    "2021-01-08": {"S1": 14.0 / 2.1, "S2": 24.0 / 4.0},
}


# ── SQL 引擎 ────────────────────────────────────────────
def test_sql_engine_pit_semantics(con):
    meta = make_meta(body=sql_from_yaml())
    rows = {(r[0], r[1]): r[2] for r in run_factor(con, meta)}
    for dt, per in EXPECT.items():
        for s, want in per.items():
            if want is None or (dt, s) not in rows:
                continue
            assert rows[(dt, s)] == want                     # 逐位一致（同运算顺序）
    # 01-04/05 无公告 → 无因子行（P0 缺失整行不产出）
    assert ("2021-01-04", "S1") not in rows
    assert ("2021-01-04", "S3") not in rows


def test_sql_engine_pit_meta_output(con):
    meta = make_meta(body=sql_from_yaml())
    rows = run_factor(con, meta)
    used = {(r[0], r[1]): (r[3], r[4]) for r in rows}
    # S1 01-06：同日双公告取最新 period=2021-03-31，announce=2021-01-06
    assert used[("2021-01-06", "S1")] == ("2021-03-31", "2021-01-06")
    # S2 01-06..01-08：FY2020
    assert used[("2021-01-07", "S2")] == ("2020-12-31", "2021-01-06")


def test_sql_engine_window_params(con):
    meta = make_meta(body=sql_from_yaml())
    rows = run_factor(con, meta, start_dt="2021-01-07")
    assert {r[0] for r in rows} == {"2021-01-07", "2021-01-08"}


def test_sql_engine_missing_table(con):
    con.execute("DROP TABLE dwd_financial_report")
    meta = make_meta(body="SELECT dt, secid, close AS value, NULL, NULL FROM dwd_daily_bar")
    with pytest.raises((EngineError, ValueError)):
        run_factor(con, meta)


def test_sql_engine_output_contract(con):
    meta = make_meta(body="SELECT dt, secid FROM dwd_daily_bar")   # 缺 value/used_* 列
    with pytest.raises(EngineError, match="缺列"):
        run_factor(con, meta)


# ── python_udf 引擎 ─────────────────────────────────────
def good_pe_udf(bars, reports):
    """UDF 版 PE-TTM（与 SQL 同口径的第二实现，交叉验证）。"""
    out = []
    for s, reps in reports.items():
        by_period = {r["period"]: r for r in reps}
        for b in bars.get(s, []):
            dt = b["dt"]
            ann = [r for r in reps if r["announce_date"] <= dt]
            if not ann:
                continue
            p0 = max(ann, key=lambda r: (r["announce_date"], r["period"]))
            if p0["period"][5:7] == "12":
                ttm = p0["eps_ytd"]
            else:
                py = str(int(p0["period"][:4]) - 1)
                same = by_period.get(py + p0["period"][4:])
                fy = by_period.get(py + "-12-31")
                if same is None or fy is None:
                    continue
                ttm = p0["eps_ytd"] - same["eps_ytd"] + fy["eps_ytd"]
            if ttm is None or ttm <= 0:
                continue
            out.append((dt, s, b["close"] / ttm, p0["period"], p0["announce_date"]))
    return out


def udf_with_random(bars, reports):
    import random
    return [(b["dt"], s, random.random(), None, None)
            for s, arr in bars.items() for b in arr]


def udf_with_socket(bars, reports):
    import socket
    socket.gethostname()
    return [(b["dt"], s, 1.0, None, None) for s, arr in bars.items() for b in arr]


def udf_with_open(bars, reports):
    with open("/tmp/x", "w") as f:          # noqa: S108（守卫应拦截）
        f.write("x")
    return [(b["dt"], s, 1.0, None, None) for s, arr in bars.items() for b in arr]


def udf_with_eval(bars, reports):
    return eval("1")                        # noqa: S307（守卫应拦截）


def udf_with_global(bars, reports):
    global _COUNTER
    _COUNTER = 0
    return [(b["dt"], s, 1.0, None, None) for s, arr in bars.items() for b in arr]


def test_udf_engine_matches_sql(con):
    ref = f"{__name__}.good_pe_udf"
    rows = {(r[0], r[1]): r[2] for r in udf_engine.run_udf(con, ref)}
    meta = make_meta(body=sql_from_yaml())
    sql_rows = {(r[0], r[1]): r[2] for r in run_factor(con, meta)}
    assert rows.keys() == sql_rows.keys()
    for k in rows:
        assert rows[k] == sql_rows[k]       # 双实现逐位一致


def test_udf_guard_rejects_forbidden():
    for fn in (udf_with_random, udf_with_socket, udf_with_open, udf_with_eval, udf_with_global):
        with pytest.raises(UdfViolation):
            udf_engine.resolve_udf(f"{__name__}.{fn.__name__}")


def test_udf_guard_accepts_pure():
    fn = udf_engine.resolve_udf(f"{__name__}.good_pe_udf")
    assert callable(fn)


def test_udf_engine_missing_fn():
    with pytest.raises(UdfViolation):
        udf_engine.resolve_udf("no.such_module.fn_x")


# ── qlib_expr 引擎 ──────────────────────────────────────
def test_qlib_ref(con):
    rows = {(r[0], r[1]): r[2] for r in qlib_expr.run_expr(con, "Ref($close, 1)")}
    assert rows[("2021-01-05", "S1")] == CLOSES["S1"][0]      # 前一日 close
    assert ("2021-01-04", "S1") not in rows                   # 首-1 期 → NaN 丢弃


def test_qlib_mean_std(con):
    import statistics
    mean_rows = {(r[0], r[1]): r[2] for r in qlib_expr.run_expr(con, "Mean($close, 3)")}
    std_rows = {(r[0], r[1]): r[2] for r in qlib_expr.run_expr(con, "Std($close, 3)")}
    win = CLOSES["S1"][:3]
    assert mean_rows[("2021-01-06", "S1")] == sum(win) / 3
    assert std_rows[("2021-01-06", "S1")] == statistics.stdev(win)   # ddof=1


def test_qlib_combo(con):
    # ($close - Ref($close,1)) / Ref($close,1) = pct 变化（手算 S2 第 2 日）
    rows = {(r[0], r[1]): r[2] for r in qlib_expr.run_expr(con, "($close - Ref($close,1))/Ref($close,1)")}
    assert rows[("2021-01-05", "S2")] == (CLOSES["S2"][1] - CLOSES["S2"][0]) / CLOSES["S2"][0]


def test_qlib_delta_and_abs(con):
    rows = {(r[0], r[1]): r[2] for r in qlib_expr.run_expr(con, "Delta($close, 2)")}
    assert rows[("2021-01-07", "S1")] == CLOSES["S1"][3] - CLOSES["S1"][1]
    rows2 = {(r[0], r[1]): r[2] for r in qlib_expr.run_expr(con, "Abs($close - 31.5)")}
    assert rows2[("2021-01-05", "S3")] == abs(CLOSES["S3"][1] - 31.5)


def test_qlib_syntax_errors(con):
    with pytest.raises(EngineError):
        qlib_expr.parse("Mean($close, 3")            # 括号未闭合
    with pytest.raises(EngineError):
        qlib_expr.parse("Foo($close, 3)")            # 未知函数
    with pytest.raises(EngineError):
        qlib_expr.parse("Mean($, 3)")                # 字段名缺失


def test_engine_dispatch_unknown(con):
    with pytest.raises(EngineError, match="未注册引擎"):
        run_factor(con, make_meta(engine="spark", body="x"))
