"""M3 单因子检验流水线测试（实施计划 §6 DoD）：IC 正确性 + NW t + 分层/换手/公告周 + 入库。

合成库 E（12 标的 × 18 个月末 2021-01..2022-06，基准 510300.SH 恒 100 → 超额=绝对收益）：
- 月收益 r_k = 0.01·k（B01→1% … B12→12%），月末复利 close；
- F+：value = k（跨期恒定）→ 每月 Spearman(value, fwd) = +1.0（完美单调）；
- F−：value = −k → IC = −1.0；direction=−1 → 多头组 = 低值层 = 高收益组；
- 公告周：B03 2021-03-28、B05 2021-06-29 公告（距月末 ≤7 日 → 对应截面 near）。

DoD-2 手工锚（3 组已知相关）：
  完美正 IC=1.0；完美负 IC=−1.0；混合 xs=[1,2,3,4] ys=[0.1,0.3,0.2,0.4] → ry=[1,3,2,4]，
  cov=4.0，vx=vy=5 → IC=0.8。
"""
import calendar
import json
import math
from datetime import date

import duckdb
import pytest

from factorlib import evaluate as EV
from factorlib.evaluate import (avg_rank, spearman, newey_west_t, assign_layers,
                                fwd_returns, turnover, evaluate_factor, render_md,
                                save_eval, ensure_eval_tables, month_ends)

SECIDS = [f"B{k:02d}" for k in range(1, 13)]
R = {s: 0.01 * k for k, s in enumerate(SECIDS, start=1)}          # 月收益
MES = [f"{date(y, m, calendar.monthrange(y, m)[1]).isoformat()}"
       for y, m in [(2021, m) for m in range(1, 13)] + [(2022, m) for m in range(1, 7)]]
BENCH = "510300.SH"
ANN = [("B03", "2021-03-28", "2021-03-31"), ("B05", "2021-06-29", "2021-06-30")]


def closes_series(s: str) -> list[float]:
    """月末复利 close：100 · Π(1+r)。"""
    out, c = [], 100.0
    for _ in MES:
        c *= 1 + R[s]
        out.append(c)
    return out


@pytest.fixture()
def eval_con():
    con = duckdb.connect(":memory:")
    for ddl in EV._EVAL_DDLS:
        con.execute(ddl)
    con.execute("""CREATE TABLE factor_value_daily
                   (dt DATE, secid TEXT, factor_id TEXT, value DOUBLE)""")
    con.execute("""CREATE TABLE factor_pit_meta
                   (dt DATE, secid TEXT, factor_id TEXT, used_period DATE,
                    used_announce DATE, data_staleness INTEGER)""")
    for s in SECIDS:
        con.executemany("INSERT INTO dwd_daily_bar VALUES (?,?,?,?,?,?,?,?,?)",
                        [(s, dt, c, c, c, c, 1000, 1000 * c, 0.0)
                         for dt, c in zip(MES, closes_series(s))])
    con.executemany("INSERT INTO dwd_daily_bar VALUES (?,?,?,?,?,?,?,?,?)",
                    [(BENCH, dt, 100.0, 100.0, 100.0, 100.0, 1000, 100000.0, 0.0)
                     for dt in MES])
    con.executemany("INSERT INTO dwd_financial_report (secid, period, announce_date)"
                    " VALUES (?,?,?)", [(s, p, a) for s, p, a in ANN])
    ensure_eval_tables(con)
    # F+/F− 因子值直接注入长表（检验层测试聚焦，物化层已有独立测试）
    rows = []
    for dt in MES:
        for k, s in enumerate(SECIDS, start=1):
            rows.append((dt, s, "F_pos", float(k)))
            rows.append((dt, s, "F_neg", float(-k)))
    con.executemany("INSERT INTO factor_value_daily (dt, secid, factor_id, value) VALUES (?,?,?,?)",
                    rows)
    return con


class _Meta:
    """合成 FactorMeta（duck-类型：evaluate_factor 只读 id/name/type/version/family/direction/engine）。"""

    def __init__(self, fid, direction=1, ftype="alpha"):
        self.id, self.direction, self.type = fid, direction, ftype
        self.name, self.version, self.family, self.engine = fid, "1.0.0", "test", "sql"


# ── 统计原语 ──
def test_avg_rank_ties():
    assert avg_rank([10.0, 20.0, 20.0, 30.0]) == [1.0, 2.5, 2.5, 4.0]


def test_spearman_known_three_cases():
    """DoD-2：3 组已知相关合成数据，IC 与手工计算一致（前两组逐位，混合组逐位）。"""
    assert spearman([1, 2, 3, 4, 5], [2, 4, 6, 8, 10]) == 1.0        # 完美正
    assert spearman([1, 2, 3, 4, 5], [10, 8, 6, 4, 2]) == -1.0       # 完美负
    assert spearman([1, 2, 3, 4], [0.1, 0.3, 0.2, 0.4]) == 0.8       # ry=[1,3,2,4] → 4/5
    assert spearman([1, 1, 1], [1, 2, 3]) is None                    # 常数列
    assert spearman([1, 2], [1, 2]) is None                          # n < 3


def test_newey_west_hand():
    """NW t 手工核对：lag=0 退化为经典 t；lag=1 含一阶自相关修正；自动 lag 规则。"""
    ics = [0.1, 0.2, 0.3, 0.4, 0.5, 0.6]
    T, m = 6, 0.35
    e = [x - m for x in ics]
    g0 = sum(x * x for x in e) / T
    # lag=0：SE = sqrt(γ0/T)
    r0 = newey_west_t(ics, lag=0)
    assert r0["lag"] == 0
    assert r0["t"] == pytest.approx(m / math.sqrt(g0 / T), abs=1e-12)
    # lag=1：w1=1−1/2，γ1=(1/T)Σ_{t≥1} e_t·e_{t−1}
    g1 = sum(e[t] * e[t - 1] for t in range(1, T)) / T
    r1 = newey_west_t(ics, lag=1)
    assert r1["t"] == pytest.approx(m / math.sqrt((g0 + 2 * 0.5 * g1) / T), abs=1e-12)
    # 自动 lag = floor(4·(T/100)^(2/9))，T=6 → 2
    assert newey_west_t(ics)["lag"] == 2
    assert newey_west_t([0.1]) is None


# ── 数据提取与分层原语 ──
def test_month_ends_and_fwd_returns(eval_con):
    assert month_ends(eval_con, "2021-01-01", "2022-12-31") == MES
    fr = fwd_returns({s: dict(zip(MES, closes_series(s))) for s in SECIDS},
                     MES[0], MES[1], SECIDS)
    for s in SECIDS:
        assert fr[s] == pytest.approx(R[s])                        # 复利收益 = 月收益


def test_assign_layers_contiguous_nonempty():
    vals = {f"S{i:02d}": float(i) for i in range(1, 13)}
    layers = assign_layers(vals, 10)
    assert len(layers) == 10
    flat = [s for g in layers for s in g]
    assert sorted(flat) == sorted(vals)                            # 全覆盖不重不漏
    assert [len(g) for g in layers] == [2, 1, 1, 1, 1, 2, 1, 1, 1, 1]
    assert layers[0] == ["S01", "S02"] and layers[-1] == ["S12"]
    assert layers[5] == ["S07", "S08"]                             # i*L//n 公式：额外元素落组 0/组 5
    assert assign_layers({f"S{i}": float(i) for i in range(4)}, 10)[0] == ["S0"]  # 自适应退化


def test_turnover():
    assert turnover(["A", "B"], ["A", "B"]) == 0.0
    assert turnover(["A", "B"], ["C", "D"]) == 1.0
    assert turnover(["A"], ["A", "B"]) == 0.0
    assert turnover([], ["A"]) is None


# ── 检验端到端（合成完美/反向因子） ──
def test_evaluate_perfect_factor(eval_con):
    rep = evaluate_factor(eval_con, _Meta("F_pos", direction=1),
                          "2021-01-01", "2022-12-31")
    assert rep["window"]["n_months_total"] == 18
    assert rep["benchmark"]["secid"] == BENCH
    h1 = rep["horizons"][1]
    assert h1["n_months"] == 17                                    # 18 月末 − 1 个前瞻窗
    assert all(r["ic"] == 1.0 for r in h1["ic_series"])            # 每月 IC = +1.0
    assert h1["ic_mean"] == 1.0
    assert h1["ic_ir"] is None and h1["ic_t_nw"] is None           # IC 恒定 → 波动 0，诚实置 None
    # 分层：10 层完美单调 → 单调性 1.0；L1={B01,B02}→1.5%、L9={B10,B11}→10.5%、L10={B12}→12%
    assert h1["monotonicity"] == 1.0
    assert h1["layer_mean_excess"][0] == pytest.approx(0.015)
    assert h1["layer_mean_excess"][-1] == pytest.approx(0.12)
    assert h1["long_excess_mean"] == pytest.approx(0.12)           # direction=+1 → 高值层
    assert h1["long_short_mean"] == pytest.approx(0.105)
    assert h1["turnover"] == 0.0                                   # 因子恒定 → 组成员稳定
    # 公告周：2 个截面各剔除 1 样本（B03@2021-03-31、B05@2021-06-30）
    assert h1["ic_ex_announce"]["n_dropped_total"] == 2
    assert h1["ic_ex_announce"]["ic_mean"] == 1.0                  # 剔除后单调性不变
    # 覆盖度：因子值 12 标的 / 库内全集 13（含基准 ETF）→ 12/13（诚实口径）
    assert rep["universe"]["n_all"] == 13
    assert rep["coverage"]["avg"] == pytest.approx(12 / 13)


def test_evaluate_inverse_factor_direction_minus(eval_con):
    rep = evaluate_factor(eval_con, _Meta("F_neg", direction=-1),
                          "2021-01-01", "2022-12-31", horizons=(1,))
    h1 = rep["horizons"][1]
    assert all(r["ic"] == -1.0 for r in h1["ic_series"])
    assert h1["long_excess_mean"] == pytest.approx(0.115)          # 低值层 = 高收益组
    assert h1["layer_mean_excess"][0] == pytest.approx(0.115)      # 层 1（低值）恰为最高收益


def test_evaluate_turnover_flip(eval_con):
    """因子值跨期交替 → 多头组全换 → 换手 1.0（手工构造）。"""
    con = eval_con
    con.execute("DELETE FROM factor_value_daily WHERE factor_id = 'F_flip'")
    rows = []
    for i, dt in enumerate(MES):
        for k, s in enumerate(SECIDS, start=1):
            v = float(k) if i % 2 == 0 else float(13 - k)          # 偶月正序 / 奇月反序
            rows.append((dt, s, "F_flip", v))
    con.executemany("INSERT INTO factor_value_daily (dt, secid, factor_id, value) VALUES (?,?,?,?)",
                    rows)
    rep = evaluate_factor(con, _Meta("F_flip", direction=1),
                          "2021-01-01", "2022-12-31", horizons=(1,))
    assert rep["horizons"][1]["turnover"] == 1.0


def test_evaluate_filter_factor_rejected(eval_con):
    with pytest.raises(ValueError, match="非打分因子"):
        evaluate_factor(eval_con, _Meta("F_x", ftype="filter"), "2021-01-01", "2022-12-31")


def test_evaluate_window_too_short(eval_con):
    with pytest.raises(ValueError, match="月份数不足"):
        evaluate_factor(eval_con, _Meta("F_pos"), "2021-01-01", "2021-02-28")


# ── 报告与入库（任务 6.2/6.4） ──
def test_save_eval_roundtrip(eval_con):
    rep = evaluate_factor(eval_con, _Meta("F_pos"), "2021-01-01", "2022-12-31",
                          horizons=(1, 3))
    n = save_eval(eval_con, rep)
    assert n == 2                                                  # 每 horizon 一行
    rows = eval_con.execute("SELECT factor_id, horizon_months, ic_mean, ic_tstat_nw,"
                            " report_json FROM meta_factor_eval ORDER BY horizon_months"
                            ).fetchall()
    assert [r[1] for r in rows] == [1, 3]
    assert rows[0][2] == 1.0
    payload = json.loads(rows[0][4])
    assert payload["ic_series"][0]["ic"] == 1.0
    assert payload["ic_series"][0]["dt"] == MES[0]


def test_render_md_sections(eval_con):
    rep = evaluate_factor(eval_con, _Meta("F_pos"), "2021-01-01", "2022-12-31")
    md = render_md(rep)
    for section in ("## IC", "## 分层", "## 分层收益衰减", "## 口径与局限",
                    rep["git_sha"], "IC 均值", "单调性"):
        assert section in md
