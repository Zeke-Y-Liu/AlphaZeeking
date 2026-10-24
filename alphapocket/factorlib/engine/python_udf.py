"""Python UDF 引擎（架构文档 §16）：复杂/个人化因子，纯函数约束。

纯函数守卫（AST 静态检查，注册与执行前各跑一次）：
- 禁 import：网络 / 随机 / 进程 / 文件系统相关模块；
- 禁调用：open / eval / exec / compile / __import__ / input；
违例抛 PurityViolation —— 「声明式、可重放、无外部状态」（§16 约束）。

UDF 契约：compute(secid, bars, reports, as_of) -> {dt: (value, used_period, used_announce)} | None
- 一次调用返回该 secid 全截面（键为 "YYYY-MM-DD" 字符串），物化区间外日期由引擎裁剪；
- bars：该 secid 全量日 K（[{dt, open, ..., close, volume, amount, pct}, ...]，dt 升序）；
- reports：该 secid 全量财报（[{period, announce_date, eps_ytd, ...}, ...]，period 升序）；
- as_of：预留参数（当前传 None）；
- PIT 由 UDF 自行保证（只允许使用 announce_date <= dt 的报告），审计层仍逐行复核。
"""
from __future__ import annotations

import ast

from factorlib.engine.base import EngineError, normalize_rows

_BANNED_IMPORTS = {"socket", "ssl", "requests", "urllib", "urllib3", "http", "httplib", "ftplib",
                   "smtplib", "random", "secrets", "subprocess", "multiprocessing", "os", "sys",
                   "shutil", "pathlib", "tempfile", "pickle", "sqlite3", "duckdb"}
_BANNED_CALLS = {"open", "eval", "exec", "compile", "__import__", "input", "globals", "locals"}


class PurityViolation(EngineError):
    """UDF 纯函数约束违例。"""


def check_purity(source: str) -> None:
    """AST 静态纯函数检查；违例抛 PurityViolation。"""
    try:
        tree = ast.parse(source)
    except SyntaxError as e:
        raise PurityViolation(f"UDF 源码语法错误：{e}") from e
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for a in node.names:
                root = a.name.split(".")[0]
                if root in _BANNED_IMPORTS:
                    raise PurityViolation(f"UDF 禁止 import {a.name}（无外部状态/联网/随机）")
        elif isinstance(node, ast.ImportFrom):
            root = (node.module or "").split(".")[0]
            if root in _BANNED_IMPORTS:
                raise PurityViolation(f"UDF 禁止 from {node.module} import …")
        elif isinstance(node, ast.Call):
            fn = node.func
            if isinstance(fn, ast.Name) and fn.id in _BANNED_CALLS:
                raise PurityViolation(f"UDF 禁止调用 {fn.id}()")
            if isinstance(fn, ast.Attribute) and fn.attr in _BANNED_CALLS:
                raise PurityViolation(f"UDF 禁止调用 ….{fn.attr}()")


class UdfEngine:
    def __init__(self, meta):
        self.meta = meta
        if not (meta.udf or "").strip():
            raise EngineError(f"{meta.id}: engine=python_udf 但定义缺少 udf")
        check_purity(meta.udf)
        self._fn = self._compile(meta.udf)

    @staticmethod
    def _compile(source: str):
        ns: dict = {}
        exec(compile(source, "<factor_udf>", "exec"), ns)  # noqa: S102 —— 守卫已过，仅取约定名
        fn = ns.get("compute")
        if not callable(fn):
            raise EngineError("UDF 必须定义 compute(secid, bars, reports, as_of) 函数")
        return fn

    def run(self, db_path, start, end) -> list[dict]:
        """逐 secid × 交易日调用 UDF。输入取自 fl_daily_bar / fl_fin_report（prepare 产物，
        与 repo JSON 同一形状契约）。注意 UDF 收到 secid 全量历史（窗口可跨 :start 回看）。"""
        from datetime import date

        import duckdb

        _BAR_COLS = ("dt", "open", "high", "low", "close", "volume", "amount", "pct")
        _REP_COLS = ("period", "announce_date", "eps_ytd", "net_profit", "revenue",
                     "total_assets", "total_liab", "ocf", "capex")
        d0, d1 = date.fromisoformat(str(start)[:10]), date.fromisoformat(str(end)[:10])
        con = duckdb.connect(str(db_path), read_only=True)
        try:
            bars: dict[str, list] = {}
            for secid, *vals in con.execute(
                    f"SELECT secid, {', '.join(_BAR_COLS)} FROM fl_daily_bar "
                    "ORDER BY secid, dt").fetchall():
                bars.setdefault(secid, []).append(
                    dict(zip(_BAR_COLS, vals)))
            reports: dict[str, list] = {}
            for secid, *vals in con.execute(
                    f"SELECT secid, {', '.join(_REP_COLS)} FROM fl_fin_report "
                    "ORDER BY secid, period").fetchall():
                reports.setdefault(secid, []).append(dict(zip(_REP_COLS, vals)))
        finally:
            con.close()

        rows = []
        for secid, arr in bars.items():
            out = self._fn(secid, arr, reports.get(secid, []), None)
            # 契约放宽：compute 可一次返回全截面（dict dt->三元组），亦可逐日模式（as_of 模式）
            if isinstance(out, dict):
                for dt, triple in out.items():
                    d = date.fromisoformat(str(dt)[:10])
                    if not (d0 <= d <= d1) or triple is None:
                        continue
                    value, used_period, used_announce = triple
                    rows.append({"dt": d, "secid": secid, "value": value,
                                 "used_period": used_period, "used_announce": used_announce})
                continue
            if out is None:
                continue
            raise EngineError(f"{self.meta.id}: UDF 返回类型非法（期望 dict 或 None）：{type(out).__name__}")
        return normalize_rows(rows)
