"""python_udf 引擎（实施计划 §4 任务 1.2：纯函数约束——禁联网/禁随机数/禁 IO）。

注册方式：YAML ``fn: 包路径.函数名``（如 ``factorlib_builtins.pe_ttm``）；
注册中心加载时经 :func:`resolve_udf` 解析并做 AST 纯函数守卫：

- import 白名单：math / statistics / collections / itertools / functools / typing / dataclasses；
- 禁用内置：open / eval / exec / compile / __import__ / input；
- 禁 global 语句、禁黑名单模块（os/sys/socket/urllib/requests/http/subprocess/random/...）；
- 违例 → UdfViolation（注册期拦截，不进运行期）。

函数签名约定（M1 冻结）::

    fn(bars: dict[str, list[dict]], reports: dict[str, list[dict]]) -> list[tuple]

- bars：{secid: [{"dt": "YYYY-MM-DD", "open","high","low","close","volume","amount","pct"}, ...]}
- reports：{secid: [{"period","announce_date","eps_ytd", ...}, ...]}（PIT 纪律由作者自负：
  只允许使用 announce_date <= dt 的记录，CI 由 pit_audit 抽样校验）
- 返回：[(dt, secid, value, used_period|None, used_announce|None), ...]；
  used_* 为 None 时该行不写 factor_pit_meta（审计面外，登记于 summary.pit_uncovered）。
"""
from __future__ import annotations

import ast
import importlib
import inspect

ALLOWED_IMPORTS = {"math", "statistics", "collections", "itertools", "functools",
                   "typing", "dataclasses"}
FORBIDDEN_IMPORTS = {"os", "sys", "socket", "urllib", "requests", "http", "subprocess",
                     "shutil", "random", "secrets", "uuid", "pathlib", "pickle", "builtins"}
FORBIDDEN_BUILTINS = {"open", "eval", "exec", "compile", "__import__", "input", "globals"}


class UdfViolation(ValueError):
    """UDF 纯函数约束违例。"""


def _check_node(node: ast.AST, errors: list[str]) -> None:
    if isinstance(node, (ast.Import, ast.ImportFrom)):
        mods = ([a.name for a in node.names] if isinstance(node, ast.Import)
                else [node.module or ""])
        for m in mods:
            root = m.split(".")[0]
            if root in FORBIDDEN_IMPORTS:
                errors.append(f"禁用模块 import：{m}")
            elif root not in ALLOWED_IMPORTS:
                errors.append(f"非白名单模块 import：{m}（白名单 {sorted(ALLOWED_IMPORTS)}）")
    elif isinstance(node, ast.Name) and node.id in FORBIDDEN_BUILTINS:
        errors.append(f"禁用内置调用：{node.id}")
    elif isinstance(node, ast.Global):
        errors.append("禁用 global 语句（纯函数约束）")
    for child in ast.iter_child_nodes(node):
        _check_node(child, errors)


def guard_pure_function(fn) -> None:
    """AST 纯函数守卫（注册期一次性拦截）。"""
    try:
        src = inspect.getsource(fn)
    except (OSError, TypeError) as e:            # 内置/交互式定义无法取源码 → 拒绝
        raise UdfViolation(f"无法取函数源码做纯函数校验：{e}") from e
    errors: list[str] = []
    _check_node(ast.parse(textwrap_dedent(src)), errors)
    if errors:
        raise UdfViolation(f"{fn.__name__}: 纯函数约束违例 → {errors}")


def textwrap_dedent(src: str) -> str:
    """极简 dedent（避免引入 textwrap 到白名单讨论——守卫自身不受限）。"""
    lines = src.splitlines()
    pad = len(lines[0]) - len(lines[0].lstrip()) if lines else 0
    return "\n".join(ln[pad:] for ln in lines)


def resolve_udf(ref: str):
    """``包路径.函数名`` → 经 AST 守卫后的函数对象。"""
    if "." not in ref:
        raise UdfViolation(f"fn 须为 '包路径.函数名'，实得 {ref!r}")
    mod_name, _, attr = ref.rpartition(".")
    try:
        mod = importlib.import_module(mod_name)
    except ImportError as e:
        raise UdfViolation(f"UDF 模块不可导入：{mod_name}（{e}）") from e
    fn = getattr(mod, attr, None)
    if not callable(fn):
        raise UdfViolation(f"UDF 不可调用：{ref}")
    guard_pure_function(fn)
    return fn


def run_udf(con, ref: str, start_dt: str | None = None, end_dt: str | None = None) -> list[tuple]:
    """执行 UDF：从 DuckDB 拉数（按 start/end 过滤）→ 函数 → 输出契约校验。"""
    from .sql_engine import require_tables, OUTPUT_COLUMNS, EngineError
    require_tables(con)
    fn = resolve_udf(ref)
    dt_filter = "WHERE ($s IS NULL OR dt >= CAST($s AS DATE)) AND ($e IS NULL OR dt <= CAST($e AS DATE))"
    bars: dict[str, list[dict]] = {}
    for secid, dt, o, h, l, c, v, amt, pct in con.execute(
            f"SELECT secid, strftime(dt,'%Y-%m-%d'), open, high, low, close, volume, amount, pct "
            f"FROM dwd_daily_bar {dt_filter} ORDER BY secid, dt",
            {"s": start_dt, "e": end_dt}).fetchall():
        bars.setdefault(secid, []).append(
            {"dt": dt, "open": o, "high": h, "low": l, "close": c, "volume": v,
             "amount": amt, "pct": pct})
    reports: dict[str, list[dict]] = {}
    cols = ("period", "announce_date", "eps_ytd", "bps", "roe", "roe_w", "rev_g", "np_g", "margin")
    for secid, *vals in con.execute(
            f"SELECT secid, {', '.join(c for c in cols)} FROM dwd_financial_report "
            f"ORDER BY secid, period").fetchall():
        reports.setdefault(secid, []).append(
            {c: (v.strftime("%Y-%m-%d") if hasattr(v, "strftime") else v)
             for c, v in zip(cols, vals)})
    rows = fn(bars, reports)
    for r in rows:
        if not isinstance(r, tuple) or len(r) != len(OUTPUT_COLUMNS):
            raise EngineError(f"UDF 输出行须为 {len(OUTPUT_COLUMNS)} 元组，实得 {r!r}")
    return [tuple(r) for r in rows]
