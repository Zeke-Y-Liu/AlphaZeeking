"""覆盖度日报（实施计划 §5 任务 5.2）。

月度截面覆盖率 + 缺失分布，供 DoD 复核（估值族 ≥85% / 质量族 ≥90%）与 M3 检验入池判断。

诚实口径（CP-0004 登记）：
- 分母 = 库内标的全集（dwd_daily_bar 出现过的 secid），非"全 A"——repo 样本仅 9 标的；
- 非 ST 过滤用 flt_st 因子值（dim_security 静态快照口径，见 flt_st.yaml 数据限制）；
- 财务空窗（5.3）：staleness 分布随报告一并输出（距最近财报季度数）。
"""
from __future__ import annotations

import duckdb


def month_end_dates(con: duckdb.DuckDBPyConnection, start: str, end: str) -> list[str]:
    """每月最后一个交易日（库内口径）。"""
    rows = con.execute("""
        SELECT substr(dt, 1, 7) AS ym, max(dt) AS me
        FROM (SELECT CAST(dt AS VARCHAR) AS dt FROM dwd_daily_bar
              WHERE dt >= CAST(? AS DATE) AND dt <= CAST(? AS DATE))
        GROUP BY ym ORDER BY me
    """, [start, end]).fetchall()
    return [r[1] for r in rows]


def coverage_report(con: duckdb.DuckDBPyConnection, factor_ids: list[str],
                    start: str, end: str,
                    universe_secids: list[str] | None = None) -> dict:
    """月度截面覆盖度：{factor_id: {month: {cov_ratio, n_cov, n_universe, missing, staleness}}}。

    universe_secids 缺省 = 库内标的全集；提供时（如 flt_st=1 过滤后的非 ST 池）按池计算。
    missing 给出前 N 个缺失 secid 样例（缺失分布）。
    """
    mes = month_end_dates(con, start, end)
    secids = universe_secids
    if secids is None:
        secids = [r[0] for r in con.execute(
            "SELECT DISTINCT secid FROM dwd_daily_bar ORDER BY secid").fetchall()]
    n_u = len(secids)
    out: dict = {}
    for fid in factor_ids:
        rows = con.execute(
            "SELECT CAST(dt AS VARCHAR), secid FROM factor_value_daily WHERE factor_id = ?"
            " AND dt >= CAST(? AS DATE) AND dt <= CAST(? AS DATE)",
            [fid, start, end]).fetchall()
        have: dict[str, set] = {}
        for dt, s in rows:
            have.setdefault(dt, set()).add(s)
        months = {}
        for me in mes:
            cov = have.get(me, set())
            missing = sorted(set(secids) - cov)
            st = con.execute(
                "SELECT avg(data_staleness), max(data_staleness) FROM factor_pit_meta"
                " WHERE factor_id = ? AND CAST(dt AS VARCHAR) = ?",
                [fid, me]).fetchone()
            months[me] = {"n_universe": n_u, "n_cov": len(cov),
                          "cov_ratio": round(len(cov) / n_u, 4) if n_u else 0.0,
                          "missing_sample": missing[:5],
                          "staleness_avg": round(st[0], 2) if st and st[0] is not None else None,
                          "staleness_max": st[1] if st else None}
        out[fid] = months
    return out


def render_md(rep: dict) -> str:
    """覆盖度日报 → Markdown（月度矩阵，每因子一行 × 每月一列覆盖率 %）。"""
    if not rep:
        return "（无覆盖度数据）"
    months = sorted({m for v in rep.values() for m in v})
    lines = ["# 因子覆盖度日报（月度截面）", "",
             "| factor_id | " + " | ".join(m[2:] for m in months) + " |",
             "|---" * (len(months) + 1) + "|"]
    for fid, months_map in rep.items():
        cells = [f"{months_map[m]['cov_ratio'] * 100:.0f}%" if m in months_map else "-"
                 for m in months]
        lines.append(f"| {fid} | " + " | ".join(cells) + " |")
    lines += ["", "覆盖率 = 月末截面有值标的 / 非池过滤后标的全集；财务因子另附 staleness"
              "（距最近财报季度数，avg/max）于 detail JSON。"]
    return "\n".join(lines)
