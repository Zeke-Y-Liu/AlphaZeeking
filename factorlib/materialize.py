"""物化管道（实施计划 §4 任务 1.4）：三模式 → factor_value_daily 长表 + factor_pit_meta。

模式（mode）：
- full         全量重算：先清该因子全部行，再按全区间（或 start/end）重算；
- incremental  增量：水位 = 该因子已物化 max(dt)，仅补 dt > 水位 区间；
- snapshot     回测 as_of 快照：只计算返回行、不落库（供回测/评估只读消费）。

幂等纪律：full 增量重复执行结果逐行一致；factor_pit_meta 与 factor_value_daily 同区间同删同写。
复现纪律：verify_reproducible 对同一 as_of 两次全量重算做 md5 逐行比对（DoD：diff=0）。
表结构（与 applib/data.py 声明一致；ensure_tables 幂等创建，data_sync 重建后可直接物化）：
    factor_value_daily(dt DATE, secid TEXT, factor_id TEXT, value DOUBLE)
    factor_pit_meta(dt DATE, secid TEXT, factor_id TEXT, used_period DATE, used_announce DATE,
                    data_staleness INTEGER)

data_staleness（任务 5.3）：dt 所在季度与 used_period 所在季度相隔的报告期数
（(y差)*4 + 季度差），衡量财务数据新鲜度；used_period 晚于 dt 所在季度（业绩预告/快报
提前披露）→ clamp 0（最新鲜）；非财务因子（used_period NULL）→ NULL。
"""
from __future__ import annotations

import duckdb

from .engine import run_factor, OUTPUT_COLUMNS

VALID_MODES = ("full", "incremental", "snapshot")


def ensure_tables(con: duckdb.DuckDBPyConnection) -> None:
    con.execute("""CREATE TABLE IF NOT EXISTS factor_value_daily
                   (dt DATE, secid TEXT, factor_id TEXT, value DOUBLE)""")
    con.execute("""CREATE TABLE IF NOT EXISTS factor_pit_meta
                   (dt DATE, secid TEXT, factor_id TEXT, used_period DATE, used_announce DATE,
                    data_staleness INTEGER)""")


def data_staleness(dt: str, used_period: str) -> int:
    """距最近财报季度数：dt 与 used_period 相隔的报告期个数（预告期 clamp 0）。"""
    from datetime import date
    d = date.fromisoformat(str(dt)[:10])
    p = date.fromisoformat(str(used_period)[:10])
    q = (d.year - p.year) * 4 + (d.month - 1) // 3 - (p.month - 1) // 3
    return max(0, q)


def _watermark(con, factor_id: str) -> str | None:
    row = con.execute("SELECT max(dt) FROM factor_value_daily WHERE factor_id = ?",
                      [factor_id]).fetchone()
    return row[0].strftime("%Y-%m-%d") if row and row[0] else None


def _check_depends(con, meta) -> None:
    """依赖解析（任务 1.5）：meta.depends 声明的表必须已存在（缺失显式报错）。"""
    have = {r[0] for r in con.execute(
        "SELECT table_name FROM information_schema.tables "
        "WHERE table_schema = 'main'").fetchall()}
    missing = [t for t in meta.depends if t not in have]
    if missing:
        raise ValueError(f"{meta.id}: 依赖数据表缺失 {missing}（先做数据同步）")


def _write(con, factor_id: str, rows: list[tuple], start_dt: str | None, end_dt: str | None) -> dict:
    """落库：值行进 factor_value_daily，used_* 进 factor_pit_meta（value NULL 丢弃）。

    删除区间语义：[start_dt, end_dt] 闭区间内该因子旧行先删（无界端 = 负无穷/正无穷），
    保证同区间重复物化幂等。
    """
    ensure_tables(con)
    con.execute("DELETE FROM factor_value_daily WHERE factor_id = $fid"
                " AND ($s IS NULL OR dt >= CAST($s AS DATE)) AND ($e IS NULL OR dt <= CAST($e AS DATE))",
                {"fid": factor_id, "s": start_dt, "e": end_dt})
    con.execute("DELETE FROM factor_pit_meta WHERE factor_id = $fid"
                " AND ($s IS NULL OR dt >= CAST($s AS DATE)) AND ($e IS NULL OR dt <= CAST($e AS DATE))",
                {"fid": factor_id, "s": start_dt, "e": end_dt})
    vals, pits = [], []
    for r in rows:
        r = tuple(r)
        if len(r) != len(OUTPUT_COLUMNS):
            raise ValueError(f"物化行须 {len(OUTPUT_COLUMNS)} 列，实得 {r!r}")
        dt, secid, _, used_period, used_announce = r
        value = r[2]
        if value is None:
            continue
        vals.append((dt, secid, factor_id, float(value)))
        if used_period is not None and used_announce is not None:
            pits.append((dt, secid, factor_id, used_period, used_announce,
                         data_staleness(dt, used_period)))
    if vals:
        con.executemany("INSERT INTO factor_value_daily VALUES (?,?,?,?)", vals)
    if pits:
        con.executemany("INSERT INTO factor_pit_meta VALUES (?,?,?,?,?,?)", pits)
    return {"rows": len(vals), "pit_rows": len(pits), "null_dropped": len(rows) - len(vals)}


def materialize(con: duckdb.DuckDBPyConnection, meta, mode: str = "full",
                start: str | None = None, end: str | None = None) -> dict:
    """物化入口：按模式执行并落库（snapshot 不落库）。"""
    if mode not in VALID_MODES:
        raise ValueError(f"未知物化模式：{mode}（可选 {VALID_MODES}）")
    _check_depends(con, meta)
    if mode == "incremental":
        wm = _watermark(con, meta.id)
        start = wm if start is None else max(start, wm) if wm else start
    rows = run_factor(con, meta, start_dt=start, end_dt=end)
    if mode == "snapshot":
        null_dropped = sum(1 for r in rows if r[2] is None)
        return {"factor_id": meta.id, "mode": mode, "rows": len(rows) - null_dropped,
                "pit_rows": sum(1 for r in rows if r[3] is not None and r[4] is not None),
                "null_dropped": null_dropped, "rows_data": [tuple(r) for r in rows]}
    res = _write(con, meta.id, rows, start, end)
    return {"factor_id": meta.id, "mode": mode, "start": start, "end": end, **res}


def verify_reproducible(con: duckdb.DuckDBPyConnection, meta, as_of: str | None = None) -> dict:
    """复现性测试（任务 1.6）：同一 as_of 两次全量重算，逐因子值逐行 diff 必须 = 0。"""
    import hashlib

    def _digest() -> str:
        rows = con.execute(
            "SELECT dt, secid, value FROM factor_value_daily WHERE factor_id = $fid"
            " AND ($e IS NULL OR dt <= CAST($e AS DATE)) ORDER BY dt, secid",
            {"fid": meta.id, "e": as_of}).fetchall()
        payload = repr(rows).encode("utf-8")
        return hashlib.md5(payload).hexdigest()

    d1 = _digest()
    materialize(con, meta, mode="full", end=as_of)
    d2 = _digest()
    return {"factor_id": meta.id, "as_of": as_of, "digest_1": d1, "digest_2": d2,
            "reproducible": d1 == d2}
