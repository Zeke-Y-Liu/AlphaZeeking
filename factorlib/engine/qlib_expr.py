"""qlib_expr 引擎（实施计划 §4 任务 1.2：对接 Alpha158/360 表达式口径的时序子集）。

α 阶段自研子集解释器（不依赖 qlib 安装；M7 三引擎交叉核对时再接真 qlib）：

文法（递归下降）::

    expr   := term (('+'|'-') term)*
    term   := factor (('*'|'/') factor)*
    factor := ['-'] atom
    atom   := NUMBER | '$' FIELD | '(' expr ')' | FUNC '(' args ')'
    FUNC   := Ref | Mean | Std | Sum | Max | Min | Abs | Log | Delta

- Ref($close, 1)      滞后 n 期（n>=0）
- Mean/Std/Sum/Max/Min(x, n)  滚动窗口（含当日，min_periods=n，不足为 NaN）
- Delta($close, 5)    x - Ref(x, n)
- Abs/Log             逐点变换

执行面：对 dwd_daily_bar 每标的时序 DataFrame 求值 → (dt, secid, value, NULL, NULL)。
qlib_expr 无财务依赖 → used_period/used_announce 恒为 NULL（PIT 审计面外，纯行情时序）。
"""
from __future__ import annotations

import math
import duckdb
import pandas as pd

from .sql_engine import require_tables, EngineError


# ── 词法 ────────────────────────────────────────────────
def _tokenize(s: str) -> list[tuple[str, str]]:
    toks, i, n = [], 0, len(s)
    while i < n:
        c = s[i]
        if c.isspace():
            i += 1
        elif c.isdigit() or (c == "." and i + 1 < n and s[i + 1].isdigit()):
            j = i
            while j < n and (s[j].isdigit() or s[j] == "."):
                j += 1
            toks.append(("num", s[i:j]))
            i = j
        elif c == "$":
            j = i + 1
            while j < n and (s[j].isalnum() or s[j] == "_"):
                j += 1
            if j == i + 1:
                raise EngineError(f"qlib 表达式：$ 后缺字段名（pos {i}）")
            toks.append(("field", s[i + 1:j]))
            i = j
        elif c.isalpha():
            j = i
            while j < n and (s[j].isalnum() or s[j] == "_"):
                j += 1
            toks.append(("name", s[i:j]))
            i = j
        elif c in "+-*/(),":
            toks.append(("op", c))
            i += 1
        else:
            raise EngineError(f"qlib 表达式：非法字符 {c!r}（pos {i}）")
    return toks


_FUNCS_1 = {"abs", "log"}
_FUNCS_2 = {"ref", "mean", "std", "sum", "max", "min", "delta"}


# ── 递归下降解析（输出闭包树） ───────────────────────────
class _Parser:
    def __init__(self, toks):
        self.toks, self.i = toks, 0

    def peek(self):
        return self.toks[self.i] if self.i < len(self.toks) else ("eof", "")

    def eat(self, kind=None, val=None):
        t = self.peek()
        if (kind and t[0] != kind) or (val and t[1] != val):
            raise EngineError(f"qlib 表达式：期望 {val or kind}，实得 {t[1]!r}")
        self.i += 1
        return t

    def parse(self):
        e = self.expr()
        if self.peek()[0] != "eof":
            raise EngineError(f"qlib 表达式：结尾多余 token {self.peek()[1]!r}")
        return e

    def expr(self):
        e = self.term()
        while self.peek() == ("op", "+") or self.peek() == ("op", "-"):
            op = self.eat()[1]
            rhs = self.term()
            e = (lambda l, r, op=op: lambda df: op(l(df), r(df)))(e, rhs,
                                                                 {"+": lambda a, b: a + b,
                                                                  "-": lambda a, b: a - b}[op])
        return e

    def term(self):
        e = self.factor()
        while self.peek() == ("op", "*") or self.peek() == ("op", "/"):
            op = self.eat()[1]
            rhs = self.factor()
            e = (lambda l, r, op=op: lambda df: op(l(df), r(df)))(e, rhs,
                                                                 {"*": lambda a, b: a * b,
                                                                  "/": lambda a, b: a / b}[op])
        return e

    def factor(self):
        if self.peek() == ("op", "-"):
            self.eat()
            inner = self.factor()
            return lambda df: -inner(df)
        return self.atom()

    def atom(self):
        t = self.peek()
        if t[0] == "num":
            self.eat()
            v = float(t[1])
            return lambda df: pd.Series(v, index=df.index)
        if t[0] == "field":
            self.eat()
            col = t[1]
            return lambda df: df[col]
        if t == ("op", "("):
            self.eat()
            e = self.expr()
            self.eat("op", ")")
            return e
        if t[0] == "name":
            name = self.eat()[1].lower()
            if name not in _FUNCS_1 | _FUNCS_2:
                raise EngineError(f"qlib 表达式：未知函数 {t[1]}（支持 {sorted(_FUNCS_1 | _FUNCS_2)}）")
            self.eat("op", "(")
            args = [self.expr()]
            while self.peek() == ("op", ","):
                self.eat()
                args.append(self.expr())
            self.eat("op", ")")
            return self._make_fn(name, args)
        raise EngineError(f"qlib 表达式：意外 token {t[1]!r}")

    @staticmethod
    def _make_fn(name, args):
        if name in _FUNCS_1:
            if len(args) != 1:
                raise EngineError(f"{name}() 须 1 参")
            f = args[0]
            return (lambda df: f(df).abs()) if name == "abs" else (lambda df: f(df).map(math.log))
        if len(args) != 2:
            raise EngineError(f"{name}() 须 2 参（表达式, 窗口）")
        f, nfn = args[0], args[1]

        def window(df):
            nv = nfn(df)
            n = int(nv.iloc[0]) if hasattr(nv, "iloc") else int(nv)
            if n < 0:
                raise EngineError(f"{name}() 窗口须 >=0，实得 {n}")
            x = f(df)
            if name == "ref":
                return x.shift(n)
            if name == "delta":
                return x - x.shift(n)
            if name == "mean":
                return x.rolling(n, min_periods=n).mean()
            if name == "std":
                return x.rolling(n, min_periods=n).std(ddof=1)
            if name == "sum":
                return x.rolling(n, min_periods=n).sum()
            if name == "max":
                return x.rolling(n, min_periods=n).max()
            return x.rolling(n, min_periods=n).min()

        return window


def parse(expr: str):
    """qlib.expr 子集 → 求值闭包（parse 期即拦截语法错）。"""
    return _Parser(_tokenize(expr)).parse()


def run_expr(con: duckdb.DuckDBPyConnection, expr: str, start_dt: str | None = None,
             end_dt: str | None = None) -> list[tuple]:
    """对每标的时序求值 → [(dt, secid, value, None, None), ...]（value NaN 丢弃）。"""
    require_tables(con)
    fn = parse(expr)                      # 解析失败 → EngineError（先于取数）
    dt_filter = "WHERE ($s IS NULL OR dt >= CAST($s AS DATE)) AND ($e IS NULL OR dt <= CAST($e AS DATE))"
    out: list[tuple] = []
    for secid, in con.execute(
            f"SELECT DISTINCT secid FROM dwd_daily_bar {dt_filter}", {"s": start_dt, "e": end_dt}).fetchall():
        rows = con.execute(
            "SELECT strftime(dt,'%Y-%m-%d'), open, high, low, close, volume, amount, pct "
            f"FROM dwd_daily_bar {dt_filter} AND secid = $sid ORDER BY dt",
            {"s": start_dt, "e": end_dt, "sid": secid}).fetchall()
        df = pd.DataFrame(rows, columns=["dt", "open", "high", "low", "close", "volume", "amount", "pct"])
        ser = fn(df)
        for dt, v in zip(df["dt"], ser):
            if v is not None and not (isinstance(v, float) and math.isnan(v)):
                out.append((dt, secid, float(v), None, None))
    return out
