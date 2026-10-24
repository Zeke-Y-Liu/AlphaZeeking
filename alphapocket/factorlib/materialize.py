"""物化管道（架构文档 §17/§18 + 计划 M1.4）：三模式写入 fl_factor_value_daily / fl_factor_pit_meta。

模式：
- full         全量重算（定义/口径变更、回补；DELETE 后整段重写，可重放）
- incremental  盘后增量（起点 = 该因子**当前版本段**已物化最大 dt + 1 日）
- as_of        回测快照（单日，严格 point-in-time）

可复现性（M1.6）：同一 as_of 两次全量重算逐行一致 —— 由「确定性引擎输出 + 幂等写入」保证，
lineage_id 只随 (factor_id, version, 定义文本, 输入数据指纹) 变化。

版本冻结（M5 / 计划 8.3，T-L3）：DELETE 与增量水位全部收敛到 (factor_id, factor_version)——
升版重算不再触碰旧版行（旧版本冻结只读）；full 模式前置「定义指纹守卫」：同 version 定义
文本漂移 = 冻结红线违例（须 bump_version 升版），输入指纹漂移（新财报入库等数据面更新）放行。
"""
from __future__ import annotations

import hashlib
import json
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import duckdb

from factorlib.engine import get_engine

_TABLE_VALUES = "fl_factor_value_daily"
_TABLE_PIT = "fl_factor_pit_meta"
_TABLE_LINEAGE = "fl_factor_lineage"

# version 三段式数值序（1.10.0 > 1.9.0；split_part 消除零填充歧义；逐键 DESC）。
# TRY_CAST：非数字段（如参数网格变体 1.0.0+grid）→ NULL 排末位，不崩溃（M4 兼容）
_VKEYS = ("TRY_CAST(split_part(factor_version, '.', 1) AS INT)",
          "TRY_CAST(split_part(factor_version, '.', 2) AS INT)",
          "TRY_CAST(split_part(factor_version, '.', 3) AS INT)")
_VORDER_DESC = ", ".join(f"{k} DESC NULLS LAST" for k in _VKEYS)


class MaterializeError(Exception):
    """物化失败（依赖缺失 / 参数非法 / 版本冻结守卫违例）。"""


def _ensure_tables(con) -> None:
    con.execute(f"""CREATE TABLE IF NOT EXISTS {_TABLE_VALUES} (
        dt DATE, secid TEXT, factor_id TEXT, value DOUBLE,
        factor_version TEXT, lineage_id TEXT)""")
    con.execute(f"""CREATE TABLE IF NOT EXISTS {_TABLE_PIT} (
        dt DATE, secid TEXT, factor_id TEXT, used_period DATE, used_announce DATE)""")
    # M5 T-L3：定义指纹登记（factor_id+version 一行）——full 守卫与被引用追溯的基线
    con.execute(f"""CREATE TABLE IF NOT EXISTS {_TABLE_LINEAGE} (
        factor_id TEXT, factor_version TEXT, definition_fp TEXT,
        lineage_id TEXT, materialized_at TEXT)""")


def _input_fingerprint(con) -> str:
    """输入数据指纹：行数 + 关键列极值（决定 lineage 的数据面）。"""
    fp = {}
    for t, cols in (("fl_daily_bar", ("count(*)", "min(dt)", "max(dt)")),
                    ("fl_fin_report", ("count(*)", "max(announce_date)")),
                    ("fl_dws_financial_ttm", ("count(*)", "max(announce_date)"))):
        row = con.execute(f"SELECT {', '.join(cols)} FROM {t}").fetchone()
        fp[t] = [str(x) for x in row]
    return hashlib.sha256(json.dumps(fp, sort_keys=True).encode()).hexdigest()[:16]


def definition_fingerprint(meta) -> str:
    """定义指纹（口径面，M5 T-L3）：sha256(id|version|engine|定义文本)。

    与 lineage_id 的区别：剥离输入数据指纹——新财报入库只动数据面（lineage 变、
    定义指纹不变 = 合法漂移）；改定义文本不升版则定义指纹变 = 冻结守卫违例。
    """
    definition = meta.sql or meta.udf or meta.expr or ""
    payload = json.dumps([meta.id, meta.version, meta.engine, definition.strip()],
                         ensure_ascii=False)
    return hashlib.sha256(payload.encode()).hexdigest()


def lineage_id(meta, db_path: Path | None = None, con=None) -> str:
    """lineage = sha256(factor_id|version|定义文本|输入指纹)。定义或数据一变即变。

    con 缺省时自开 read_only 连接（独立调用场景）；materialize 内传入其读写连接，
    避免 DuckDB「同一文件不同配置并发连接」冲突。
    """
    own = con is None
    if own:
        con = duckdb.connect(str(db_path), read_only=True)
    try:
        fp = _input_fingerprint(con)
    finally:
        if own:
            con.close()
    definition = meta.sql or meta.udf or meta.expr or ""
    payload = json.dumps([meta.id, meta.version, meta.engine, definition.strip(), fp],
                         ensure_ascii=False)
    return hashlib.sha256(payload.encode()).hexdigest()


def latest_version(con, factor_id: str) -> str | None:
    """该因子在物化表的最新 version（三段式数值序；无行返回 None）。

    消费端 pin 的「默认最新」统一入口（factor_backtest / factor_eval_fl）。
    """
    row = con.execute(f"SELECT factor_version FROM {_TABLE_VALUES} "
                      "WHERE factor_id = ? ORDER BY " + _VORDER_DESC + " LIMIT 1",
                      [factor_id]).fetchone()
    return row[0] if row else None


def _guard_definition_frozen(con, meta) -> None:
    """full 模式版本冻结守卫（物化前）：同 (factor_id, version) 定义指纹漂移即拒。

    - 登记无记录 = 首次物化（含 M5 升级前的存量基线）→ 放行，物化成功后建立登记；
    - 定义指纹不同 = 定义文本变了没升版 → MaterializeError（口径变更必须 bump_version）；
    - 输入指纹漂移不改定义指纹 → 放行；incremental/as_of 不校验（增量前提即 lineage 不变）。
    """
    row = con.execute(f"SELECT definition_fp FROM {_TABLE_LINEAGE} "
                      "WHERE factor_id = ? AND factor_version = ?",
                      [meta.id, meta.version]).fetchone()
    now = definition_fingerprint(meta)
    if row and row[0] != now:
        raise MaterializeError(
            f"{meta.id} v{meta.version}: 定义文本相对上次物化已漂移"
            f"（definition_fp {str(row[0])[:12]}… → {now[:12]}…）"
            "——口径变更必须 bump_version 升版，旧版本冻结（M5 T-L3 红线）")


def _register_lineage(con, meta, lid: str) -> None:
    """full 物化成功后登记定义指纹（幂等 upsert，仅 full 写——增量不触碰守卫基线）。"""
    con.execute(f"DELETE FROM {_TABLE_LINEAGE} WHERE factor_id = ? AND factor_version = ?",
                [meta.id, meta.version])
    con.execute(f"INSERT INTO {_TABLE_LINEAGE} VALUES (?,?,?,?,?)",
                (meta.id, meta.version, definition_fingerprint(meta), lid,
                 datetime.now(UTC).strftime("%Y-%m-%d %H:%M:%S")))


def _calendar_bounds(con) -> tuple[date, date]:
    row = con.execute("SELECT min(dt), max(dt) FROM fl_trade_calendar").fetchone()
    if not row or not row[0]:
        raise MaterializeError("fl_trade_calendar 为空：请先执行 factorlib.prepare")
    lo = row[0] if isinstance(row[0], date) else date.fromisoformat(str(row[0]))
    hi = row[1] if isinstance(row[1], date) else date.fromisoformat(str(row[1]))
    return lo, hi


def _apply_transforms(con, meta, rows: list[dict]) -> list[dict]:
    """按 dt 截面应用变换链（meta.transform + meta.neutralization）。

    变换是同一 dt 内的相对化 → 不引入跨日信息，PIT 语义不变；
    缺失值（None）原样保留，行数不变（审计 join 不受影响）。
    """
    from factorlib.transform import apply_chain
    has_dim = con.execute("SELECT count(*) FROM information_schema.tables "
                          "WHERE table_name = 'fl_dim_security'").fetchone()[0]
    industries = dict(con.execute("SELECT secid, industry FROM fl_dim_security").fetchall()) \
        if has_dim else {}
    by_dt: dict = {}
    for r in rows:
        by_dt.setdefault(r["dt"], []).append(r)
    out = []
    for dt in sorted(by_dt, key=str):
        rs = by_dt[dt]
        vals = {r["secid"]: r["value"] for r in rs}
        try:
            tv = apply_chain(vals, meta.transform, meta.neutralization, industries)
        except Exception as e:
            raise MaterializeError(f"{meta.id} @ {dt}: 变换链失败：{e}") from e
        for r in rs:
            out.append(dict(r, value=tv[r["secid"]]))
    return out


def materialize(meta, db_path: Path, mode: str = "full", as_of: str | None = None) -> dict:
    """按模式物化单因子。返回摘要（含 lineage、起止区间、写入行数）。"""
    if mode not in ("full", "incremental", "as_of"):
        raise MaterializeError(f"mode 非法：{mode!r}（full | incremental | as_of）")
    if mode == "as_of" and not as_of:
        raise MaterializeError("mode=as_of 必须提供 as_of 日期")

    con = duckdb.connect(str(db_path))
    try:
        _ensure_tables(con)
        # 版本冻结守卫（M5 T-L3）：full 模式前置，定义漂移在重算前即拒（不白算）
        if mode == "full":
            _guard_definition_frozen(con, meta)
        # 依赖解析（M1.5）：输入表必须就位
        missing = [t for t in meta.inputs
                   if not con.execute("SELECT count(*) FROM information_schema.tables "
                                      "WHERE table_name = ?", [t]).fetchone()[0]]
        if missing:
            raise MaterializeError(f"{meta.id}: 依赖表缺失 {missing}（先跑 factorlib.prepare）")

        cal_lo, cal_hi = _calendar_bounds(con)
        if mode == "as_of":
            start = end = date.fromisoformat(str(as_of)[:10])
        elif mode == "incremental":
            # 水位按 (factor_id, version) 隔离（T-L3）：升版后新版本段从 cal_lo 起算，
            # 不从旧版水位续算（旧版本行冻结只读，新版本独立成段）
            row = con.execute(f"SELECT max(dt) FROM {_TABLE_VALUES} "
                              "WHERE factor_id = ? AND factor_version = ?",
                              [meta.id, meta.version]).fetchone()
            last = row[0] if row and row[0] else None
            start = (last + timedelta(days=1)) if last else cal_lo
            end = cal_hi
            if start > end:
                return {"factor_id": meta.id, "mode": mode, "written": 0,
                        "start": str(start), "end": str(end),
                        "lineage_id": lineage_id(meta, db_path, con=con),
                        "note": "已最新，无需增量"}
        else:
            start, end = cal_lo, cal_hi
    finally:
        # 引擎自行管理连接（read_only）：须先关闭本读写连接，避免同文件双配置冲突
        con.close()

    rows = get_engine(meta).run(db_path, start, end)

    con = duckdb.connect(str(db_path))
    try:
        if meta.transform or meta.neutralization:
            rows = _apply_transforms(con, meta, rows)
        lid = lineage_id(meta, db_path, con=con)

        # 全量/快照先清区间旧值（幂等）；增量只追加（起点已排除已物化区间）。
        # T-L3 版本冻结：value 清除收敛到 (factor_id, factor_version)——升版重算
        # 不再抹掉旧版本行；pit 无 version 列，随 value 同生同灭按 (factor_id, dt) 清
        # （pit 无值即无行；升版不破坏旧 pit 审计——旧版 value 行在、pit 行也在）。
        if mode in ("full", "as_of"):
            con.execute(f"DELETE FROM {_TABLE_VALUES} "
                        "WHERE factor_id = ? AND factor_version = ? AND dt BETWEEN ? AND ?",
                        [meta.id, meta.version, start, end])
            con.execute(f"DELETE FROM {_TABLE_PIT} WHERE factor_id = ? AND dt BETWEEN ? AND ?",
                        [meta.id, start, end])

        vals, pits = [], []
        for r in rows:
            if r["value"] is None:      # 无值不落行（NULL 语义 = 缺席）
                continue
            vals.append((r["dt"], r["secid"], meta.id, float(r["value"]), meta.version, lid))
            if meta.pit and r["used_period"] and r["used_announce"]:
                pits.append((r["dt"], r["secid"], meta.id, r["used_period"], r["used_announce"]))
        if vals:
            con.executemany(f"INSERT INTO {_TABLE_VALUES} VALUES (?,?,?,?,?,?)", vals)
        if pits:
            con.executemany(f"INSERT INTO {_TABLE_PIT} VALUES (?,?,?,?,?)", pits)
        if mode == "full":
            _register_lineage(con, meta, lid)   # 定义指纹基线登记（守卫依据）
        return {"factor_id": meta.id, "mode": mode, "written": len(vals), "pit_meta": len(pits),
                "start": str(start), "end": str(end), "version": meta.version,
                "lineage_id": lid}
    finally:
        con.close()


def query(db_path: Path, factor_id: str, dt: str | None = None,
          secid: str | None = None) -> list[dict]:
    """点查物化结果（M1 DoD「物化 → 查询」）。"""
    con = duckdb.connect(str(db_path), read_only=True)
    try:
        sql = f"""SELECT dt, secid, factor_id, value, factor_version, lineage_id
                  FROM {_TABLE_VALUES} WHERE factor_id = ?"""
        args: list = [factor_id]
        if dt:
            sql += " AND dt = ?"
            args.append(dt)
        if secid:
            sql += " AND secid = ?"
            args.append(secid)
        sql += " ORDER BY dt, secid"
        cur = con.execute(sql, args)
        return [dict(zip([c[0] for c in cur.description], r)) for r in cur.fetchall()]
    finally:
        con.close()
