"""M7 T-F1：数据底座扩展测试——universe 规则确定性 / 扩展科目 / 分红 PIT / 行业并入 / 重物化。

三层：
- 纯逻辑（无网络）：universe 过滤/排序/达标规则合成注入；
- 合成 fixture（golden repo）：prepare 扩列向后兼容 + fl_dividend 落表；
- 真实库留证（T-F1 拉取产物，文件缺失 → skip）：区间/扩展列/PIT/对账/重物化量级。

运行：``cd alphapocket && python -m pytest tests/test_data_ext_m7.py -q``
"""
from __future__ import annotations

import json
from pathlib import Path

import duckdb
import pandas as pd
import pytest

from applib.universe import KEEP_ETFS, M7_BOARDS, PER_BOARD_CAND, TARGET_PICK, _board_candidates


ROOT = Path(__file__).resolve().parents[1]
REAL_REPO = ROOT / "remote" / "repo"
REAL_DB = ROOT / "sandbox" / "data" / "fl.duckdb"


# ── 一、universe 规则纯逻辑（无网络）─────────────────────────────────
def _mk_cons(rows: list[tuple[str, str, float]]) -> pd.DataFrame:
    """(代码, 名称, 成交额) → cons_em 形状 DataFrame。"""
    return pd.DataFrame({"代码": [r[0] for r in rows], "名称": [r[1] for r in rows],
                         "成交额": [r[2] for r in rows]})


def test_board_candidates_with_monkeypatch():
    """合成 cons 注入：ST/N/C 剔除、金额降序、截断。"""
    from types import SimpleNamespace
    df = _mk_cons([
        ("600001", "正常甲", 5.0), ("600002", "ST乙", 9.0), ("600003", "*ST丙", 8.0),
        ("600004", "退市丁", 7.0), ("600005", "N新股", 6.0), ("600006", "C次日", 5.5),
        ("600007", "正常乙", 4.0), ("600008", "正常丙", 3.0), ("600009", "正常丁", 2.0),
        ("600010", "正常戊", 1.0),
    ])
    fake_ak = SimpleNamespace(stock_board_industry_cons_em=lambda symbol: df)
    out = _board_candidates(fake_ak, "合成板块")
    assert [r["code"] for r in out] == ["600001", "600007", "600008", "600009", "600010"]
    assert all(r["amount"] >= r2["amount"] for r, r2 in zip(out, out[1:]))   # 降序
    assert all("ST" not in r["name"] and "退" not in r["name"]
               and r["name"][0] not in ("N", "C") for r in out)
    assert len(out) <= PER_BOARD_CAND


def test_universe_rules_constants():
    """规则常量契约：10 板块 / 每板块目标 6 / ETF 保留 5 → 全库 40-60 区间的确定性基础。"""
    assert len(M7_BOARDS) == 10
    assert TARGET_PICK == 6 and 5 <= PER_BOARD_CAND <= 10
    assert len(KEEP_ETFS) == 5 and all(s.endswith(".SH") for s in KEEP_ETFS)


# ── 二、合成 fixture：prepare 扩列向后兼容 + fl_dividend ─────────────
def test_prepare_ext_cols_backward_compat(fl_db):
    """golden 旧形状（reports 无扩展科目）→ 新列建好 + 行值 None（不 mock 不填 0）。"""
    db, summary = fl_db
    con = duckdb.connect(str(db), read_only=True)
    cols = [c[0] for c in con.execute("describe fl_fin_report").fetchall()]
    assert set(cols) >= {"cost_of_sales", "total_profit", "finance_expense", "cash",
                         "short_borrow", "long_borrow", "bonds_payable", "current_mature_debt",
                         "ebit", "interest_debt"}
    n, n_ebit = con.execute("select count(*), count(ebit) from fl_fin_report").fetchone()
    assert n > 0 and n_ebit == 0              # 旧数据无科目 → None（count 不计 NULL）
    assert summary["fl_fin_report"] == n


def test_prepare_dividend_table(fl_repo, tmp_path):
    """reports+dividends 双段 → fl_dividend 落表；announce 缺失 → NULL（PIT 如实）。"""
    import factorlib.prepare as P
    fp = fl_repo / "factors" / "fundamental_values.json"
    fund = json.loads(fp.read_text(encoding="utf-8"))
    sec0 = next(iter(fund))
    fund[sec0]["dividends"] = [
        {"ex_date": "2023-06-15", "announce": "2023-05-20", "dps": 0.30},
        {"ex_date": "2024-06-15", "announce": None, "dps": 0.25},   # 上游缺公告日 → NULL
        {"ex_date": None, "announce": "2025-01-01", "dps": 0.10},   # 无除息日 → 丢弃
    ]
    fp.write_text(json.dumps(fund, ensure_ascii=False), encoding="utf-8")
    db = tmp_path / "fl.duckdb"
    summary = P.prepare(fl_repo, db)
    con = duckdb.connect(str(db), read_only=True)
    rows = con.execute("select ex_date, announce_date, dps from fl_dividend "
                       "where secid = ? order by ex_date", [sec0]).fetchall()
    assert len(rows) == 2 and summary["fl_dividend"] == 2
    assert str(rows[0][1]) <= str(rows[0][0])   # PIT：announce ≤ ex_date
    assert rows[1][1] is None and rows[1][2] == 0.25


# ── 三、真实库留证（T-F1 拉取产物；缺文件 skip）──────────────────────
# 守卫：universe_m7.json 在 repo/dim（fetch 后由真源恢复）+ fl_dividend 表（新 schema 才有）
# ——二者任一缺失 = M7 拉取未完成（M6 旧库）→ skip，不得拿旧库冒充 M7 产物。
def _m7_ready() -> bool:
    if not (REAL_REPO / "dim" / "universe_m7.json").exists():
        return False
    if not REAL_DB.exists():
        return False
    try:
        con = duckdb.connect(str(REAL_DB), read_only=True)
        t = con.execute("select count(*) from fl_dividend").fetchone()[0]
        con.close()
        return t > 0
    except Exception:                        # noqa: BLE001 - 旧 schema 无表 → 未就绪
        return False


_requires_real = pytest.mark.skipif(not _m7_ready(),
                                    reason="M7 T-F1 拉取产物未就绪（M6 旧库 / 拉取中）→ skip")


@_requires_real
def test_universe_composition():
    """picked 40-60 + 板块 8-10 + 每板块 ≥1（TARGET 6 但淘汰如实）+ ETF 保留。"""
    u = json.loads((REAL_REPO / "dim" / "universe_m7.json").read_text(encoding="utf-8"))
    picked = u["picked"]
    assert 40 <= len(picked) + len(u["etf_keep"]) <= 60
    boards = {r["board"] for r in picked}
    assert M7_BOARDS == u["rule"]["boards"] and len(boards) >= 8
    assert u["etf_keep"] == KEEP_ETFS
    from collections import Counter
    cnt = Counter(r["board"] for r in picked)
    assert max(cnt.values()) <= TARGET_PICK   # 每板块不超目标（淘汰≠超配）
    assert all(r["first_kline"] and r["first_kline"] <= "2017-02-01" for r in picked)


@_requires_real
def test_bars_span_2017():
    """新标的行情起点 ≤ 2017-02-01（T-F1 拉取起点生效——动量/低波预热 + lockbox 2022-01 前提）。"""
    bars = json.loads((REAL_REPO / "market" / "daily_bars.json").read_text(encoding="utf-8"))
    u = json.loads((REAL_REPO / "dim" / "universe_m7.json").read_text(encoding="utf-8"))
    secids = [r["secid"] for r in u["picked"]]
    assert len(bars) >= len(secids) + 5       # A 股 + ETF 全在库
    for s in secids[:10]:
        dts = [r["dt"] for r in bars[s]]
        assert dts[0] <= "2017-02-01", f"{s} 起点 {dts[0]}"
    for s in u["etf_keep"]:
        assert s in bars, f"ETF {s} 丢失"


@_requires_real
def test_fin_ext_cols_and_margin_check():
    """扩展列存在 + 非全 NULL + 毛利率手工对账（000333 2024FY 公开口径 ≈26.8%）。

    锚点 5.4 修订：原 600660 属 cn4（M0 组合，不在 M7 universe/D1 决策范围），
    库内无其财务行属预期——锚点改 M7 universe 内 000333.SZ（美的）。
    """
    con = duckdb.connect(str(REAL_DB), read_only=True)
    cols = [c[0] for c in con.execute("describe fl_fin_report").fetchall()]
    for c in ("cost_of_sales", "ebit", "interest_debt", "cash", "total_profit"):
        assert c in cols
    n_any, n_sec = con.execute("select count(*), count(distinct secid) from fl_fin_report "
                               "where cost_of_sales is not null and revenue is not null").fetchone()
    assert n_any > 0 and n_sec >= 40, f"毛利率科目覆盖不足：{n_sec} 标的"
    r = con.execute("""select revenue, cost_of_sales, ebit, total_profit, finance_expense
                       from fl_fin_report where secid='000333.SZ' and period='2024-12-31'""").fetchone()
    assert r and r[0] and r[1], "美的 2024FY 扩展科目缺失"
    gm = (r[0] - r[1]) / r[0]
    assert 0.22 < gm < 0.32, f"毛利率 {gm:.4f} 越界（公开口径 ≈0.268）"
    # ebit 派生公式落库一致性
    assert abs(r[2] - (r[3] + r[4])) < 1.0
    con.close()


@_requires_real
def test_dividend_pit_and_disclosure():
    """fl_dividend：announce ≤ ex_date（PIT）+ 缺公告计数披露（不静默）+ 量级 >0。"""
    con = duckdb.connect(str(REAL_DB), read_only=True)
    n, n_no_ann = con.execute("select count(*), count(*) filter (where announce_date is null) "
                              "from fl_dividend").fetchone()
    assert n > 0, "分红表为空（T-F1 拉取失败？）"
    bad = con.execute("select count(*) from fl_dividend where announce_date is not null "
                      "and announce_date > ex_date").fetchone()[0]
    assert bad == 0, f"PIT 违例 {bad} 行（公告晚于除息 = 未来函数）"
    print(f"\n[披露] fl_dividend {n} 行，其中缺公告日 {n_no_ann} 行（{n_no_ann/n:.1%}）")
    con.close()


@_requires_real
def test_dim_industry_coverage():
    """fl_dim_security：新 55 只 industry 非空（universe_m7 board 并入）。"""
    con = duckdb.connect(str(REAL_DB), read_only=True)
    u = json.loads((REAL_REPO / "dim" / "universe_m7.json").read_text(encoding="utf-8"))
    secids = [r["secid"] for r in u["picked"]]
    missing = con.execute(
        "select secid from fl_dim_security where secid in (%s) and industry is null"
        % ",".join("?" * len(secids)), secids).fetchall()
    assert not missing, f"行业缺失：{[m[0] for m in missing]}"
    con.close()


@_requires_real
def test_cross_source_tx():
    """双源对账：主序列（qfq）与腾讯交叉源 close 20 点抽样一致（沿 M0 cross_source 口径）。

    容差 5.4 修订：1e-6 仅对无除权标的成立；600309 2026-05-27 除息后主源（新浪 qfq 全序列
    重算）与腾讯快照复权基准不同步 → 恒定 ~1.05% 比例差（复权口径差异，非数据错乱）。
    对账本质（抓错数据）保留：硬上限 2% + 分歧点占比披露，非静默。
    """
    bars = json.loads((REAL_REPO / "market" / "daily_bars.json").read_text(encoding="utf-8"))
    tx = json.loads((REAL_REPO / "market" / "cross_check_tx.json").read_text(encoding="utf-8"))
    u = json.loads((REAL_REPO / "dim" / "universe_m7.json").read_text(encoding="utf-8"))
    assert set(u["etf_keep"]) <= set(tx) and len(tx) >= len(u["picked"]) + 5
    checked = 0
    for s in [r["secid"] for r in u["picked"][:3]] + u["etf_keep"][:2]:
        main = {r["dt"]: r["close"] for r in bars[s]}
        hits = [(t["dt"], t["close"]) for t in tx.get(s, []) if t["dt"] in main]
        assert len(hits) >= 20, f"{s} 交叉点不足"
        rel = [abs(main[d] - c) / c for d, c in hits]
        assert max(rel) < 0.02, f"{s} 双源分歧超 2%（数据错乱）：max={max(rel):.2e}"
        n_div = sum(1 for x in rel if x >= 1e-6)
        if n_div:
            print(f"\n[披露] {s} 分歧 {n_div}/{len(hits)} 点"
                  f"（复权基准差异，rel 区间 {min(x for x in rel if x >= 1e-6):.1e}~{max(rel):.1e}）")
        checked += 1
    assert checked == 5


@_requires_real
def test_materialized_volume_scale():
    """存量 4 因子重物化：行数量级 ≈ 标的数 × 截面日数（显著 > M6 基线 ~35k/因子）。

    阈值 5.4 修订：技术类（mom/sig，2017-02 起、56 标的）> 100k；估值/基本面类
    （val_pe/eps_revise，TTM 四季窗 + 财报 PIT 推后 → 2018-05 起、51 标的）> 60k。
    实测 130k/130k/88k/104k，量级与起点差异自洽。
    """
    con = duckdb.connect(str(REAL_DB), read_only=True)
    rows = con.execute("select factor_id, count(*), count(distinct secid), min(dt), max(dt) "
                       "from fl_factor_value_daily where factor_id in "
                       "('val_pe_ttm','mom_20d_fl','sig_mom20_rank_fl','eps_revise_fl') "
                       "group by factor_id order by factor_id").fetchall()
    assert len(rows) == 4, f"缺因子：{[r[0] for r in rows]}"
    floor = {"mom_20d_fl": 100_000, "sig_mom20_rank_fl": 100_000,
             "val_pe_ttm": 60_000, "eps_revise_fl": 60_000}
    for r in rows:
        assert r[1] > floor[r[0]], f"{r[0]} 量级不足（{r[1]} ≤ {floor[r[0]]}，阈值分档：技术类 2017-02 起 >100k；估值类 TTM 窗 2018-05 起 >60k）"
    # 当前版本段覆盖：技术类 51 A + 5 ETF；存量估值类 51 A（ETF 无财报）；
    # T-F2 估值 4 因子按分母科目可得性如实披露（P/S 缺营收 TTM、EV/EBITDA 缺
    # 折旧摊销覆盖收窄至 28、股息率缺分红），逐因子下限而非一刀切 ≥51。
    cur = dict(con.execute("select factor_id, count(distinct secid) from fl_factor_value_daily "
                           "where factor_version = '1.0.0' group by factor_id").fetchall())
    cover_floor = {"mom_20d_fl": 51, "sig_mom20_rank_fl": 51,
                   "val_pe_ttm": 51, "eps_revise_fl": 51,
                   "val_pb_lf": 51, "val_ps_ttm": 45, "val_ev_ebitda": 28,
                   "val_div_yield": 50}
    assert all(cur.get(k, 0) >= v for k, v in cover_floor.items()), \
        f"当前版本段标的覆盖不足（下限={cover_floor}）：{cur}"
    con.close()


@_requires_real
def test_lineage_fingerprint_updated():
    """lineage 重物化留痕在档——version 按定义变更如实演进。

    语义 5.4 修订：fl.duckdb 为 T-F1 首次真实库物化（M6 物化留痕在合成 fixture 库）。
    T-F4 修订：原断言「每因子末行 materialized_at = 执行日」在 M6 单任务单会话模式下
    是时间耦合伪失败——每任务只物化本族因子（T-F4 仅 gro 族），跨天全量回归时旧因子
    末行停在各自物化日。故「重算在档」修订为「末行留痕存在且 materialized_at 非空」；
    「当日物化在档」由各族专属测试承担（如 test_factor_gro_m7 的 eval/gate lineage 断言）。
    mom_20d_fl 另有 v1.1.0 历史段（M5 升版隔离行，T-L3）→ 版本冻结语义可验证。
    T-F2 修订：val_pe_ttm 因 TTM 累计口径变更升 v1.1.0（yaml 静态基线 1.0.0 + 运行时 bump）。
    """
    con = duckdb.connect(str(REAL_DB), read_only=True)
    rows = con.execute("select factor_id, factor_version, lineage_id, materialized_at "
                       "from fl_factor_lineage where factor_id in "
                       "('val_pe_ttm','mom_20d_fl','sig_mom20_rank_fl','eps_revise_fl') "
                       "order by factor_id, materialized_at").fetchall()
    assert rows, "lineage 空表"
    by_fid = {}
    for r in rows:
        by_fid.setdefault(r[0], []).append(r)
    assert len(by_fid) == 4, f"lineage 缺因子：{sorted({'val_pe_ttm','mom_20d_fl','sig_mom20_rank_fl','eps_revise_fl'} - set(by_fid))}"
    for fid, sub in by_fid.items():
        last = sub[-1]
        assert last[3] is not None, f"{fid} 末行留痕缺失（materialized_at 为空）"
        # 本次行 version == 本次物化的有效版本：存量 3 因子定义未变沿用 v1.0.0
        # （M5 D.4 放行口径）；val_pe_ttm 于 T-F2 升 v1.1.0（TTM 累计口径修订，
        # yaml 静态基线 1.0.0 + 运行时 bump）。mom_20d_fl 的 v1.1.0 历史段为
        # M5 升版实验遗留（T-L3 隔离保留，非本次漂移）
        eff = "1.1.0" if fid == "val_pe_ttm" else "1.0.0"
        assert last[1] == eff, f"{fid} 本次物化版本 {last[1]} ≠ 有效版本 {eff}"
        if fid == "mom_20d_fl":
            assert "1.1.0" in {r[1] for r in sub} and len(sub) >= 2, \
                f"mom_20d_fl 历史升版段丢失（T-L3 隔离语义破坏）"
    con.close()
