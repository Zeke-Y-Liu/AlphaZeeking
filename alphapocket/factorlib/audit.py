"""PIT 审计（计划 M0.6 第一版 + M1 DoD 篡改测试）。

两道独立检查（双保险，任一 > 0 即 FAIL）：
1. 值↔元数据：fl_factor_value_daily.dt < fl_factor_pit_meta.used_announce 的行数必须为 0
   —— 因子值不得早于其所用报告的公告日（未来函数主审计，M0.6 SQL 口径）；
2. 元数据↔真源：pit_meta.used_announce 必须等于 fl_dws_financial_ttm 中该 (secid, used_period)
   的公告日 —— 防「值对了、元数据被改」的篡改路径。

篡改注入（M1 DoD）：把定义 SQL 的 `announce_date <= d.dt` 改为 `period <= d.dt` 后重算，
检查 1 捕获违例。
"""
from __future__ import annotations

import json
from pathlib import Path

import duckdb


def pit_audit(db_path: Path, artifact_dir: Path | None = None) -> dict:
    con = duckdb.connect(str(db_path), read_only=True)
    try:
        # 检查 1：值早于公告日（未来函数主审计）
        c1 = con.execute("""
            SELECT count(*)
            FROM fl_factor_value_daily v
            JOIN fl_factor_pit_meta m
              ON v.dt = m.dt AND v.secid = m.secid AND v.factor_id = m.factor_id
            WHERE v.dt < m.used_announce
        """).fetchone()[0]
        c1_samples = con.execute("""
            SELECT v.factor_id, v.dt, v.secid, m.used_period, m.used_announce
            FROM fl_factor_value_daily v
            JOIN fl_factor_pit_meta m
              ON v.dt = m.dt AND v.secid = m.secid AND v.factor_id = m.factor_id
            WHERE v.dt < m.used_announce
            LIMIT 10
        """).fetchall()

        # 检查 2：元数据与 TTM 真源的公告日一致
        c2 = con.execute("""
            SELECT count(*)
            FROM fl_factor_pit_meta m
            JOIN fl_dws_financial_ttm t
              ON m.secid = t.secid AND m.used_period = t.period
            WHERE m.used_announce <> t.announce_date
        """).fetchone()[0]
        c2_samples = con.execute("""
            SELECT m.factor_id, m.secid, m.used_period, m.used_announce, t.announce_date
            FROM fl_factor_pit_meta m
            JOIN fl_dws_financial_ttm t
              ON m.secid = t.secid AND m.used_period = t.period
            WHERE m.used_announce <> t.announce_date
            LIMIT 10
        """).fetchall()
    finally:
        con.close()

    cols1 = ["factor_id", "dt", "secid", "used_period", "used_announce"]
    cols2 = ["factor_id", "secid", "used_period", "used_announce", "source_announce"]
    report = {
        "check1_early_value": {"violations": c1,
                               "samples": [dict(zip(cols1, r)) for r in c1_samples]},
        "check2_meta_tamper": {"violations": c2,
                               "samples": [dict(zip(cols2, r)) for r in c2_samples]},
        "pass": c1 == 0 and c2 == 0,
    }
    if artifact_dir is not None:
        artifact_dir.mkdir(parents=True, exist_ok=True)
        out = artifact_dir / f"pit_audit_{_stamp()}.json"
        out.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        report["artifact"] = str(out)
    return report


def _stamp() -> str:
    from datetime import datetime
    return datetime.now().strftime("%Y%m%d_%H%M%S")
