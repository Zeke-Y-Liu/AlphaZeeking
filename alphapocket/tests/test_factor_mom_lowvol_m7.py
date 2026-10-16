"""M7 T-F5：动量+低波红利族 6 因子测试——golden 手工对账 / 行业 PIT / β 合成 / streak 构造。

三层（与 test_factor_gro_m7 同模式）：
- 合成 fixture（golden repo 注入 2019+ 行情 / 行业 PIT / 分红序列 → 重 prepare → 物化 →
  query 对账）；期望值全部独立手工推演，不依赖被测代码：
  · mom_12_1 / mom_6_1：恒定价 + 特设两点（P(t−21)/P(t−n) − 1 精确算术）
  · mom_industry_adj：组内三票动量 {0.5, 0.25, 0.75} → 中位数 0.5 → adj {0, −0.25, 0.25}；
    行业 <3 回退全行恒等于 mom_12_1；行业切换（2020-06-01）PIT 前后组中位数不同锁死 asof
  · low_vol_252d：日收益 ±1% 交替 → 252 行窗口恰 126/126 → mean=0 →
    年化 = 0.01 × 252/√251（手工闭式）
  · low_beta：ri ≡ 1.5 × rm 线性构造 → β ≈ 1.5（容差 1e-6）
  · div_payout_streak：连 3 年断 1 年再连 2 年 → streak=2；announce 后移 → 值回退；
    announce NULL 不计入（不编造）；无分红史 → 0
- 包内 defs 静态契约：6 因子 hypothesis 绑定 ↔ sandbox/repo registry + direction ↔
  eval asc/desc 映射互锁（low ↔ asc / high ↔ desc）；
- 真实库留证（T-F5 物化/检验/门禁产物，文件缺失 → skip）：物化量级 + 2021-01 预热锚 +
  eval/gate 在档 + 行业回退行 == 同日 mom_12_1（披露红线）。

运行：``cd alphapocket && /workspace/alphapocket-web/core/.venv/bin/python -m pytest tests/test_factor_mom_lowvol_m7.py -q``
"""
from __future__ import annotations

import json
from datetime import date, timedelta
from pathlib import Path

import duckdb
import pytest

from factorlib.materialize import materialize, query
from factorlib.registry import load_factor

ROOT = Path(__file__).resolve().parents[1]
REAL_REPO = ROOT / "remote" / "repo"
REAL_DB = ROOT / "sandbox" / "data" / "fl.duckdb"
SB_REGISTRY = ROOT / "sandbox" / "repo" / "hypothesis" / "registry.json"

SIX = ("mom_12_1", "mom_6_1", "mom_industry_adj",
       "low_vol_252d", "low_beta", "div_payout_streak")
_HYPS = {"mom_12_1": "H-0020", "mom_6_1": "H-0021", "mom_industry_adj": "H-0022",
         "low_vol_252d": "H-0023", "low_beta": "H-0024", "div_payout_streak": "H-0025"}
# eval asc/desc 口径：asc=原始值小好（与 DSL/hypothesis 一致）；low ↔ asc / high ↔ desc
_EVAL_DIR = {"mom_12_1": "desc", "mom_6_1": "desc", "mom_industry_adj": "desc",
             "low_vol_252d": "asc", "low_beta": "asc", "div_payout_streak": "desc"}


# ── 注入 helpers（golden repo 可变副本上操作）─────────────────────────
def _wdays(lo: str, hi: str) -> list[str]:
    d, out = date.fromisoformat(lo), []
    while d <= date.fromisoformat(hi):
        if d.weekday() < 5:
            out.append(d.isoformat())
        d += timedelta(days=1)
    return out


def _edit_bars(fl_repo: Path, secid: str, days: list[str], close_of) -> None:
    """注入/覆盖单标的日行情（close = close_of(d, i, prev)），同步日历并集。"""
    fp = fl_repo / "market" / "daily_bars.json"
    bars = json.loads(fp.read_text(encoding="utf-8"))
    arr, prev = [], None
    for i, d in enumerate(days):
        c = float(close_of(d, i, prev))
        arr.append({"dt": d, "open": c, "high": c, "low": c, "close": c,
                    "volume": 10000, "amount": round(c * 10000, 2), "pct": 0.0})
        prev = c
    bars[secid] = arr
    fp.write_text(json.dumps(bars, ensure_ascii=False), encoding="utf-8")
    cal_fp = fl_repo / "market" / "trade_calendar.json"
    cal = set(json.loads(cal_fp.read_text(encoding="utf-8")))
    cal |= {r["dt"] for a in bars.values() for r in a}
    cal_fp.write_text(json.dumps(sorted(cal)), encoding="utf-8")


def _edit_fund(fl_repo: Path, fn) -> None:
    fp = fl_repo / "factors" / "fundamental_values.json"
    fund = json.loads(fp.read_text(encoding="utf-8"))
    fn(fund)
    fp.write_text(json.dumps(fund, ensure_ascii=False), encoding="utf-8")


def _edit_industry(fl_repo: Path, records: list[dict]) -> None:
    fp = fl_repo / "dim" / "industry_sw.json"
    fp.write_text(json.dumps({"records": records}, ensure_ascii=False), encoding="utf-8")


def _set_div(fl_repo: Path, secid: str, rows: list[dict]) -> None:
    def fn(fund):
        fund[secid]["dividends"] = rows
    _edit_fund(fl_repo, fn)


def _prep(fl_repo: Path, tmp_path: Path) -> Path:
    from factorlib.prepare import prepare
    db = tmp_path / "fl_mom.duckdb"
    prepare(fl_repo, db)
    return db


def _mat(db: Path, fl_repo: Path, *fids: str) -> Path:
    for fid in fids:
        meta = load_factor(fl_repo, fid)
        materialize(meta, db, mode="full")
    return db


def _val(db: Path, fid: str, dt: str, secid: str):
    hits = query(db, fid, dt=dt, secid=secid)
    return hits[0]["value"] if hits else None


# ── 一、动量 golden 手工对账（DoD a）──────────────────────────────────
# 2019-01-01..2021-06-30 工作日 ≈ 625 个；t=末日，t−21 / t−126 / t−252 手工定点
_DAYS = _wdays("2019-01-01", "2021-06-30")
_T = _DAYS[-1]
_D21, _D126, _D252 = _DAYS[-1 - 21], _DAYS[-1 - 126], _DAYS[-1 - 252]


def test_mom_12_1_golden_value(fl_repo, tmp_path):
    """mom_12_1 = P(t−21)/P(t−252) − 1——JT 近端/远端手工对账（DoD a）。

    600001.SH 恒价 10，特设 P(t−252)=8、P(t−21)=12 → 12/8 − 1 = 0.5（浮点精确）；
    交叉区分：同日 mom_6_1 = P(t−21)/P(t−126) − 1 = 12/10 − 1 = 0.2（t−126 未特设）。
    量价豁免财务 PIT → fl_factor_pit_meta 无行。
    """
    special = {_D252: 8.0, _D21: 12.0}
    _edit_bars(fl_repo, "600001.SH", _DAYS, lambda d, i, p: special.get(d, 10.0))
    db = _mat(_prep(fl_repo, tmp_path), fl_repo, "mom_12_1", "mom_6_1")
    assert _val(db, "mom_12_1", _T, "600001.SH") == pytest.approx(0.5, rel=1e-12)
    assert _val(db, "mom_6_1", _T, "600001.SH") == pytest.approx(0.2, rel=1e-12)
    # 窗口前端（序列第 100 日，lag252 缺席）→ 无行（None = 缺席语义）
    assert _val(db, "mom_12_1", _DAYS[100], "600001.SH") is None
    # 量价 PIT 豁免：used_period/used_announce 恒 NULL → pit 元数据表无行
    con = duckdb.connect(str(db), read_only=True)
    try:
        n = con.execute("select count(*) from fl_factor_pit_meta "
                        "where factor_id = 'mom_12_1'").fetchone()[0]
    finally:
        con.close()
    assert n == 0


def test_mom_6_1_golden_value(fl_repo, tmp_path):
    """mom_6_1 = P(t−21)/P(t−126) − 1——126 窗口手工对账（DoD a）。

    特设 P(t−126)=8、P(t−21)=12 → 0.5；交叉区分：同日 mom_12_1 = 12/10 − 1 = 0.2。
    """
    special = {_D126: 8.0, _D21: 12.0}
    _edit_bars(fl_repo, "600001.SH", _DAYS, lambda d, i, p: special.get(d, 10.0))
    db = _mat(_prep(fl_repo, tmp_path), fl_repo, "mom_6_1", "mom_12_1")
    assert _val(db, "mom_6_1", _T, "600001.SH") == pytest.approx(0.5, rel=1e-12)
    assert _val(db, "mom_12_1", _T, "600001.SH") == pytest.approx(0.2, rel=1e-12)


def test_price_window_insufficient_no_rows(fl_repo, tmp_path):
    """边界（DoD e）：golden 125 日（2021-01..06）< 126/252 → 四因子全无行。

    mom_12_1（lag252）/ low_vol_252d（count(ret)≥252）自然缺席；
    mom_6_1 需 126 行 > 125 差 1 行即无行（边界）；low_beta 无基准 510300.SH
    → inner join 空 → 无行（基准缺失与窗口不足同为 None 语义）。
    """
    db = _mat(_prep(fl_repo, tmp_path), fl_repo, "mom_12_1", "mom_6_1",
              "low_vol_252d", "low_beta")
    for fid in ("mom_12_1", "mom_6_1", "low_vol_252d", "low_beta"):
        assert query(db, fid, dt="2021-04-30") == [], f"{fid} 窗口不足应有行缺席"


# ── 二、行业调整动量：中位数对账 / 回退披露 / PIT 切换（DoD b）─────────
_INJ_DAYS = _DAYS
_T2, _T1 = _DAYS[-1], "2020-05-29"          # _T2=2021-06-30；_T1=行业切换日前
_I252_1, _I21_1 = _DAYS[_DAYS.index(_T1) - 252], _DAYS[_DAYS.index(_T1) - 21]
_I252_2, _I21_2 = _D252, _D21


def _industry_bars(fl_repo: Path, moms: dict[str, float]) -> None:
    """各票恒价 10 + t1/t2 两个时点的 (P(t−252)=8, P(t−21)=8×(1+m)) 特设 →
    m@t1 == m@t2（两时点动量同值，行业归属不同）。"""
    for secid, m in moms.items():
        special = {}
        for d252, d21 in ((_I252_1, _I21_1), (_I252_2, _I21_2)):
            special[d252], special[d21] = 8.0, 8.0 * (1 + m)
        _edit_bars(fl_repo, secid, _INJ_DAYS, lambda d, i, p, s=special: s.get(d, 10.0))


def test_mom_industry_adj_median_golden(fl_repo, tmp_path):
    """行业中位数手工对账（DoD b）：同行业 3 只 m={0.5,0.25,0.75} → med=0.5 →
    adj = {0, −0.25, 0.25}（中位数抗离群非均值：均值 0.5 巧合相同，另案测 median 语义）。"""
    _industry_bars(fl_repo, {"600001.SH": 0.5, "600002.SH": 0.25, "600003.SH": 0.75})
    _edit_industry(fl_repo, [{"code": "600001", "start_date": "2019-01-02", "l1_code": "110000"},
                             {"code": "600002", "start_date": "2019-01-02", "l1_code": "110000"},
                             {"code": "600003", "start_date": "2019-01-02", "l1_code": "110000"}])
    db = _mat(_prep(fl_repo, tmp_path), fl_repo, "mom_industry_adj", "mom_12_1")
    assert _val(db, "mom_industry_adj", _T2, "600001.SH") == pytest.approx(0.0, abs=1e-12)
    assert _val(db, "mom_industry_adj", _T2, "600002.SH") == pytest.approx(-0.25, rel=1e-12)
    assert _val(db, "mom_industry_adj", _T2, "600003.SH") == pytest.approx(0.25, rel=1e-12)
    # 一致性：adj = 同日 mom_12_1 − 行业中位数
    assert _val(db, "mom_12_1", _T2, "600001.SH") == pytest.approx(0.5, rel=1e-12)


def test_mom_industry_adj_fallback_lt3(fl_repo, tmp_path):
    """回退路径（DoD b + 红线）：行业样本 <3（仅 600001 登记）→ 全部标的回退
    mom_12_1 原值——全行比对两因子值恒等（回退 ≠ 缺行，披露由计数承载）。"""
    _industry_bars(fl_repo, {"600001.SH": 0.5, "600002.SH": 0.25, "600003.SH": 0.75})
    _edit_industry(fl_repo, [{"code": "600001", "start_date": "2019-01-02", "l1_code": "999999"}])
    db = _mat(_prep(fl_repo, tmp_path), fl_repo, "mom_industry_adj", "mom_12_1")
    con = duckdb.connect(str(db), read_only=True)
    try:
        n, diff = con.execute("""
            select count(*), sum(abs(a.value - b.value) > 1e-12)
            from fl_factor_value_daily a join fl_factor_value_daily b
              on a.factor_id='mom_industry_adj' and b.factor_id='mom_12_1'
             and a.dt=b.dt and a.secid=b.secid
        """).fetchone()
    finally:
        con.close()
    assert n > 0 and diff == 0, f"回退行应恒等于 mom_12_1：{n} 行中 {diff} 行不等"
    assert _val(db, "mom_industry_adj", _T2, "600001.SH") == pytest.approx(0.5, rel=1e-12)


def test_mom_industry_adj_pit_switch(fl_repo, tmp_path):
    """PIT 行业切换（DoD b）：600001 于 2020-06-01 由 110000 → 480000。

    _T1=2020-05-29（切换前）→ 110000 组 {0.5, 0.1, 0.1} med=0.1 → adj=0.4；
    _T2=2021-06-30（切换后）→ 480000 组 {0.5, 0, 0, 0} med=0 → adj=0.5。
    若误用最新行业（PIT 违规）_T1 会得 0.5 ≠ 0.4 → 锁死 asof 口径；
    600002 在 _T2 所在 110000 组仅剩 2 只 <3 → 回退原值 0.1（组员流失触发回退）。
    """
    _industry_bars(fl_repo, {"600001.SH": 0.5, "600002.SH": 0.1, "600003.SH": 0.1,
                             "600007.SH": 0.0, "600008.SH": 0.0, "600009.SH": 0.0})
    _edit_industry(fl_repo, [
        {"code": "600001", "start_date": "2019-01-02", "l1_code": "110000"},
        {"code": "600001", "start_date": "2020-06-01", "l1_code": "480000"},
        {"code": "600002", "start_date": "2019-01-02", "l1_code": "110000"},
        {"code": "600003", "start_date": "2019-01-02", "l1_code": "110000"},
        {"code": "600007", "start_date": "2019-01-02", "l1_code": "480000"},
        {"code": "600008", "start_date": "2019-01-02", "l1_code": "480000"},
        {"code": "600009", "start_date": "2019-01-02", "l1_code": "480000"}])
    db = _mat(_prep(fl_repo, tmp_path), fl_repo, "mom_industry_adj", "mom_12_1")
    assert _val(db, "mom_industry_adj", _T1, "600001.SH") == pytest.approx(0.4, rel=1e-12)
    assert _val(db, "mom_industry_adj", _T2, "600001.SH") == pytest.approx(0.5, rel=1e-12)
    assert _val(db, "mom_industry_adj", _T2, "600002.SH") == pytest.approx(0.1, rel=1e-12)
    assert _val(db, "mom_12_1", _T2, "600002.SH") == pytest.approx(0.1, rel=1e-12)


# ── 三、低波/低β：闭式对账 + 合成 β + 守卫差异（DoD c/e）──────────────
_BDAYS = _wdays("2019-01-01", "2021-06-30")
_BT = _BDAYS[-1]
_RMP = 0.008       # 基准日收益交替 ±0.8%


def test_low_vol_252d_golden(fl_repo, tmp_path):
    """low_vol_252d 闭式对账：日收益 ±1% 交替 → 252 行窗口恰 126 正/126 负 →
    mean=0 → std_samp = sqrt(252×0.0001/251) → 年化 = 0.01 × 252/√251（手工闭式）。"""
    def close_of(d, i, prev):
        if prev is None:
            return 100.0
        return prev * (1 + (0.01 if i % 2 else -0.01))
    _edit_bars(fl_repo, "600001.SH", _BDAYS, close_of)
    db = _mat(_prep(fl_repo, tmp_path), fl_repo, "low_vol_252d")
    want = 0.01 * 252 / 251 ** 0.5
    hits = query(db, "low_vol_252d", dt=_BT, secid="600001.SH")
    assert len(hits) == 1
    assert hits[0]["value"] == pytest.approx(want, rel=1e-9)


def test_low_vol_low_beta_guard_200d(fl_repo, tmp_path):
    """守卫差异（DoD e）：≈190 日行情 → count(ret)≈189。
    low_vol（需 ≥252）无行；low_beta（需 ≥126）有行——两阈值各自显式生效，
    禁止用不足窗口的 std 冒充（low_vol 红线）。"""
    days = _wdays("2020-10-01", "2021-06-30")
    n_ret = len(days) - 1
    assert 126 <= n_ret < 252, f"fixture 设计前提不成立：n_ret={n_ret}"
    _edit_bars(fl_repo, "510300.SH", days,
               lambda d, i, p: 100.0 if p is None else p * (1 + (_RMP if i % 2 else -_RMP)))
    _edit_bars(fl_repo, "600001.SH", days,
               lambda d, i, p: 50.0 if p is None else p * (1 + (_RMP if i % 2 else -_RMP)))
    db = _mat(_prep(fl_repo, tmp_path), fl_repo, "low_vol_252d", "low_beta")
    assert _val(db, "low_vol_252d", days[-1], "600001.SH") is None
    got = _val(db, "low_beta", days[-1], "600001.SH")
    assert got is not None and got == pytest.approx(1.0, rel=1e-6)   # ri ≡ rm → β=1


def test_low_beta_synthetic_1p5(fl_repo, tmp_path):
    """合成 β=1.5（DoD c）：ri ≡ 1.5 × rm 线性构造（rm=±0.8% 交替）→
    窗口 OLS 斜率 = cov/var = 1.5（任何 var(rm)>0 窗口恒成立），容差 1e-6。"""
    _edit_bars(fl_repo, "510300.SH", _BDAYS,
               lambda d, i, p: 100.0 if p is None else p * (1 + (_RMP if i % 2 else -_RMP)))
    _edit_bars(fl_repo, "600001.SH", _BDAYS,
               lambda d, i, p: 50.0 if p is None else p * (1 + 1.5 * (_RMP if i % 2 else -_RMP)))
    db = _mat(_prep(fl_repo, tmp_path), fl_repo, "low_beta")
    hits = query(db, "low_beta", dt=_BT, secid="600001.SH")
    assert len(hits) == 1
    assert hits[0]["value"] == pytest.approx(1.5, abs=1e-6)


# ── 四、div_payout_streak：gaps-islands 构造 + PIT（DoD d）────────────
def test_div_payout_streak_golden_islands(fl_repo, tmp_path):
    """streak 构造（DoD d）：连 3 年(2015-17)断 1 年(2018)再连 2 年(2019-20) →
    2021-04-30（2020 分红已确认）→ 最近岛 streak=2；
    2021-03-29（2020 ann=03-30 前一日）→ 仅 2019 确认 → streak=1（PIT + 持有口径）；
    600002 无分红史 → 0（真实信息非 NULL）；600003 连续 3 年 → 3（组内区分）。"""
    _set_div(fl_repo, "600001.SH", [
        {"ex_date": "2015-06-30", "announce": "2016-03-29", "dps": 0.3},
        {"ex_date": "2016-06-30", "announce": "2017-03-30", "dps": 0.3},
        {"ex_date": "2017-06-30", "announce": "2018-03-29", "dps": 0.3},
        {"ex_date": "2019-06-30", "announce": "2020-03-30", "dps": 0.4},
        {"ex_date": "2020-06-30", "announce": "2021-03-30", "dps": 0.4}])
    _set_div(fl_repo, "600003.SH", [
        {"ex_date": "2018-06-30", "announce": "2019-03-29", "dps": 0.2},
        {"ex_date": "2019-06-30", "announce": "2020-03-30", "dps": 0.2},
        {"ex_date": "2020-06-30", "announce": "2021-03-30", "dps": 0.2}])
    db = _mat(_prep(fl_repo, tmp_path), fl_repo, "div_payout_streak")
    assert _val(db, "div_payout_streak", "2021-04-30", "600001.SH") == 2.0
    assert _val(db, "div_payout_streak", "2021-03-29", "600001.SH") == 1.0
    assert _val(db, "div_payout_streak", "2021-01-29", "600001.SH") == 1.0
    assert _val(db, "div_payout_streak", "2021-04-30", "600002.SH") == 0.0
    assert _val(db, "div_payout_streak", "2021-04-30", "600003.SH") == 3.0


def test_div_payout_streak_pit_tamper(fl_repo, tmp_path):
    """PIT 红线（篡改对象）：2020 年分红 announce 2021-03-30 → 2021-06-01：
    2021-04-30 截面 streak 回退 2 → 1（2020 年未确认，岛仍 {2019}）；
    2021-06-30（公告日后）恢复 2（确认日切换生效）。"""
    _set_div(fl_repo, "600001.SH", [
        {"ex_date": "2015-06-30", "announce": "2016-03-29", "dps": 0.3},
        {"ex_date": "2016-06-30", "announce": "2017-03-30", "dps": 0.3},
        {"ex_date": "2017-06-30", "announce": "2018-03-29", "dps": 0.3},
        {"ex_date": "2019-06-30", "announce": "2020-03-30", "dps": 0.4},
        {"ex_date": "2020-06-30", "announce": "2021-06-01", "dps": 0.4}])
    db = _mat(_prep(fl_repo, tmp_path), fl_repo, "div_payout_streak")
    assert _val(db, "div_payout_streak", "2021-04-30", "600001.SH") == 1.0
    assert _val(db, "div_payout_streak", "2021-06-30", "600001.SH") == 2.0


def test_div_payout_streak_announce_null_excluded(fl_repo, tmp_path):
    """announce NULL（无法判定可见时点）→ 不计入，不编造：
    600004 三年分红但 2020 年 announce 缺 → streak=2（2018-19 岛）≠3；
    600005 三年全缺 announce → streak=0（全部不可见 ≠ NULL——无已确认分红史）。"""
    _set_div(fl_repo, "600004.SH", [
        {"ex_date": "2018-06-30", "announce": "2019-03-29", "dps": 0.2},
        {"ex_date": "2019-06-30", "announce": "2020-03-30", "dps": 0.2},
        {"ex_date": "2020-06-30", "announce": None, "dps": 0.2}])
    _set_div(fl_repo, "600005.SH", [
        {"ex_date": "2018-06-30", "announce": None, "dps": 0.2},
        {"ex_date": "2019-06-30", "announce": None, "dps": 0.2},
        {"ex_date": "2020-06-30", "announce": None, "dps": 0.2}])
    db = _mat(_prep(fl_repo, tmp_path), fl_repo, "div_payout_streak")
    assert _val(db, "div_payout_streak", "2021-04-30", "600004.SH") == 2.0
    assert _val(db, "div_payout_streak", "2021-04-30", "600005.SH") == 0.0


# ── 五、定义静态契约：假设绑定 + direction 映射互锁 ───────────────────
def test_defs_hypothesis_binding_and_direction():
    """6 因子 yaml 绑定 H-0020..H-0025；registry 反向含因子 id 且方向语义一致
    （因子 low ↔ 假设 negative / high ↔ positive）；eval asc/desc 映射锁定。"""
    import yaml
    reg = json.loads(SB_REGISTRY.read_text(encoding="utf-8"))
    by_id = {h["id"]: h for h in reg.get("hypotheses", [])}
    for fid in SIX:
        p = ROOT / "factorlib" / "defs" / fid.split("_")[0] / f"{fid}.yaml"
        if not p.exists():                       # mom_industry_adj 在 mom/ 目录
            p = ROOT / "factorlib" / "defs" / "mom" / f"{fid}.yaml"
        meta = yaml.safe_load(p.read_text(encoding="utf-8"))
        assert meta["hypothesis_id"] == _HYPS[fid], f"{fid} 假设绑定漂移"
        h = by_id.get(_HYPS[fid])
        assert h is not None and fid in h["linked_factors"]
        want = "negative" if meta["direction"] == "low" else "positive"
        assert h["expected_direction"] == want, f"{fid} 假设方向与 defs 漂移"
        assert (meta["direction"] == "low") == (_EVAL_DIR[fid] == "asc")
        assert h["statement"] and h["source"]["ref"]         # 可检验命题 + 来源可追溯
    # 引擎与输入契约：量价 4 走 fl_daily_bar（前 5 无财务依赖）；streak 依赖 fl_dividend
    engines = {"mom_12_1": "qlib_expr", "mom_6_1": "qlib_expr",
               "mom_industry_adj": "sql", "low_vol_252d": "sql",
               "low_beta": "sql", "div_payout_streak": "sql"}
    for fid in SIX:
        meta = load_factor(ROOT, fid)
        assert meta.engine == engines[fid], f"{fid} 引擎漂移"
        if fid != "div_payout_streak":
            assert "fl_fin_report" not in meta.inputs
        else:
            assert "fl_dividend" in meta.inputs
    # 行业 PIT 红线静态锁：mom_industry_adj 必含 ASOF JOIN fl_industry_sw + n>=3 回退阈值
    adj_sql = load_factor(ROOT, "mom_industry_adj").sql
    assert "ASOF" in adj_sql and "fl_industry_sw" in adj_sql and "n >= 3" in adj_sql


# ── 六、真实库留证（T-F5 物化/检验/门禁产物；文件缺失 → skip）─────────
def _requires_real() -> bool:
    return not (REAL_REPO / "market" / "daily_bars.json").exists() or not REAL_DB.exists()


@pytest.mark.skipif(_requires_real(), reason="真实库/数据面缺失（CI 只跑合成层）")
def test_real_six_factors_materialized():
    """真实库：6 因子物化量级 >0 且 2021-01 截面全部覆盖 >0（契约预热锚——
    检验窗 2021-01 起全因子覆盖，无 M6 mom 族截断问题）。"""
    con = duckdb.connect(str(REAL_DB), read_only=True)
    try:
        rows = dict(con.execute(
            "select factor_id, count(*) from fl_factor_value_daily "
            f"where factor_id in ({','.join('?' * len(SIX))}) group by 1", list(SIX)).fetchall())
        warm = dict(con.execute(
            "select factor_id, count(distinct secid) from fl_factor_value_daily "
            "where dt between '2021-01-01' and '2021-01-31' "
            f"and factor_id in ({','.join('?' * len(SIX))}) group by 1", list(SIX)).fetchall())
    finally:
        con.close()
    for fid in SIX:
        assert rows.get(fid, 0) > 0, f"{fid} 未物化"
        assert warm.get(fid, 0) > 0, f"{fid} 2021-01 截面零覆盖（预热锚失守）"


@pytest.mark.skipif(_requires_real(), reason="真实库/数据面缺失")
def test_real_eval_gate_registered():
    """检验 + 门禁产物在档（DoD f）：fl_factor_eval 六因子逐 horizon 在档；
    fl_factor_gate 六因子逐关卡在档（结论如实——pass 与拒因 detail 均为合法落档）。"""
    con = duckdb.connect(str(REAL_DB), read_only=True)
    try:
        ev = dict(con.execute(
            "select factor_id, count(*) from fl_factor_eval "
            f"where factor_id in ({','.join('?' * len(SIX))}) group by 1", list(SIX)).fetchall())
        gates = con.execute(
            "select factor_id, gate, pass, detail_json from fl_factor_gate "
            f"where factor_id in ({','.join('?' * len(SIX))})", list(SIX)).fetchall()
    finally:
        con.close()
    for fid in SIX:
        assert ev.get(fid, 0) > 0, f"{fid} eval 报告未落档"
        g = [x for x in gates if x[0] == fid]
        assert g, f"{fid} 门禁报告未落档"
        for _, gate, _, detail in g:
            assert gate and detail, f"{fid}/{gate} 门禁行残缺"


@pytest.mark.skipif(_requires_real(), reason="真实库/数据面缺失")
def test_real_fallback_rows_equal_mom_12_1():
    """披露红线（真实库）：行业样本 <3 或无行业记录的标的（回退路径触发）
    mom_industry_adj 值恒等于同日 mom_12_1 原值；且回退集合非空（l1 分布中
    n<3 组真实存在，禁止静默当原值因子用）。"""
    con = duckdb.connect(str(REAL_DB), read_only=True)
    try:
        n_fallback, n_diff = con.execute("""
            with grp as (
                select m.dt, m.secid, count(*) over (partition by m.dt, i.l1_code) as n,
                       i.l1_code
                from (select distinct dt, secid from fl_factor_value_daily
                      where factor_id = 'mom_industry_adj') m
                asof left join fl_industry_sw i on i.secid = m.secid and i.start_date <= m.dt
            )
            select count(*), sum(abs(a.value - b.value) > 1e-9)
            from fl_factor_value_daily a
            join fl_factor_value_daily b
              on b.factor_id = 'mom_12_1' and b.dt = a.dt and b.secid = a.secid
            join grp g on g.dt = a.dt and g.secid = a.secid
            where a.factor_id = 'mom_industry_adj'
              and (g.l1_code is null or g.n < 3)
        """).fetchone()
    finally:
        con.close()
    assert n_fallback > 0, "回退集合为空：n<3 组/无行业标的的回退路径未真实触发"
    assert n_diff == 0, f"回退行与 mom_12_1 不等（披露红线违约）：{n_diff} 行"
