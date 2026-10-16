"""qlib_expr 引擎（架构文档 §16「Qlib Expression」）：极简表达式 → DuckDB SQL 翻译器。

M1 范围（明确边界，Alpha158/360 全集与多层窗口属 M7）：
- 字段：$open/$high/$low/$close/$volume/$amount/$pct（fl_daily_bar 列，防注入白名单）
- 窗口函数（单层，不支持窗口嵌套）：Ref(x,n) / Mean(x,n) / Std(x,n) / Sum(x,n) /
  Max(x,n) / Min(x,n) / Rank(x)
  · Rank 为截面（按 dt 分区 percent_rank），允许包裹一个单层窗口表达式
    （如 Rank($close/Ref($close,20))）—— 经子查询提升为两级 SQL 执行；
    其余窗口嵌套（如 Mean(Ref(x,n),m)）仍拒绝。
- 四则运算与括号、数字字面量

输出 SQL 产出 dt, secid, value；used_period/used_announce 为 NULL（量价因子无财务 PIT 元数据）。
"""
from __future__ import annotations

import duckdb

from factorlib.engine.base import EngineError

_FIELDS = {"open", "high", "low", "close", "volume", "amount", "pct"}
_WINDOW_AGGS = {"Mean": "avg", "Std": "stddev_samp", "Sum": "sum", "Max": "max", "Min": "min"}

# ── 递归下降解析器 ────────────────────────────────────
class _Parser:
    def __init__(self, s: str):
        self.s = s.replace(" ", "")
        self.i = 0

    def error(self, msg: str):
        raise EngineError(f"qlib_expr 语法错误（位置 {self.i}）：{msg} → {self.s!r}")

    def peek(self) -> str:
        return self.s[self.i] if self.i < len(self.s) else ""

    def eat(self, tok: str) -> None:
        if self.s.startswith(tok, self.i):
            self.i += len(tok)
        else:
            self.error(f"期望 {tok!r}")

    def parse(self):
        node = self.expr()
        if self.i != len(self.s):
            self.error(f"多余尾部 {self.s[self.i:]!r}")
        return node

    def expr(self):
        node = self.term()
        while self.peek() and self.peek() in "+-":
            op = self.peek()
            self.i += 1
            node = ("bin", op, node, self.term())
        return node

    def term(self):
        node = self.factor()
        while self.peek() and self.peek() in "*/":
            op = self.peek()
            self.i += 1
            node = ("bin", op, node, self.factor())
        return node

    def factor(self):
        c = self.peek()
        if c == "(":
            self.eat("(")
            node = self.expr()
            self.eat(")")
            return node
        if c == "-":
            self.i += 1
            return ("neg", self.factor())
        if c == "$":
            self.i += 1
            name = self._ident()
            if name not in _FIELDS:
                self.error(f"字段 {name!r} 不在白名单 {sorted(_FIELDS)}")
            return ("field", name)
        if c.isdigit():
            return ("num", self._number())
        if c.isalpha():
            return self._func()
        self.error(f"意外字符 {c!r}")

    def _ident(self) -> str:
        j = self.i
        while j < len(self.s) and (self.s[j].isalnum() or self.s[j] == "_"):
            j += 1
        name = self.s[self.i:j]
        self.i = j
        return name

    def _number(self):
        j = self.i
        while j < len(self.s) and (self.s[j].isdigit() or self.s[j] == "."):
            j += 1
        lit = self.s[self.i:j]
        self.i = j
        try:
            return float(lit) if "." in lit else int(lit)
        except ValueError:
            self.error(f"非法数字 {lit!r}")

    def _func(self):
        name = self._ident()
        self.eat("(")
        args = [self.expr()]
        while self.peek() == ",":
            self.i += 1
            args.append(self.expr())
        self.eat(")")
        return ("func", name, args)


def _has_window(node) -> bool:
    if node[0] == "func":
        return True
    if node[0] == "bin":
        return _has_window(node[2]) or _has_window(node[3])
    if node[0] == "neg":
        return _has_window(node[1])
    return False


def _rank_lift(ast_):
    """顶层 Rank(x) 且 x 含单层窗口 → 返回 x（走子查询提升）；否则 None。"""
    if ast_[0] == "func" and ast_[1] == "Rank" and _has_window(ast_[2][0]):
        return ast_[2][0]
    return None


def _render(node) -> str:
    """渲染为 SQL 片段；窗口函数单层约束：func 的参数不得再含窗口。"""
    kind = node[0]
    if kind == "field":
        return node[1]
    if kind == "num":
        return repr(node[1])
    if kind == "neg":
        return f"(-{_render(node[1])})"
    if kind == "bin":
        return f"({_render(node[2])} {node[1]} {_render(node[3])})"
    if kind == "func":
        _, name, args = node
        if name == "Ref":
            _arity(name, args, 2)
            if _has_window(args[0]):
                raise EngineError("qlib_expr：窗口嵌套（Ref 内含窗口函数）属 M7 范围，暂不支持")
            n = _int_arg(args[1], name)
            return f"lag({ _render(args[0])}, {n}) OVER (PARTITION BY secid ORDER BY dt)"
        if name == "Rank":
            _arity(name, args, 1)
            if _has_window(args[0]):
                raise EngineError("qlib_expr：窗口嵌套（Rank 内含窗口函数）属 M7 范围，暂不支持")
            return (f"percent_rank() OVER (PARTITION BY dt ORDER BY {_render(args[0])}) "
                    f":: DOUBLE")
        if name in _WINDOW_AGGS:
            _arity(name, args, 2)
            if _has_window(args[0]):
                raise EngineError("qlib_expr：窗口嵌套（聚合内含窗口函数）属 M7 范围，暂不支持")
            n = _int_arg(args[1], name)
            agg = _WINDOW_AGGS[name]
            return (f"{agg}({_render(args[0])}) OVER (PARTITION BY secid ORDER BY dt "
                    f"ROWS BETWEEN {n - 1} PRECEDING AND CURRENT ROW)")
        raise EngineError(f"qlib_expr：未知函数 {name!r}（支持 Ref/Mean/Std/Sum/Max/Min/Rank）")
    raise EngineError(f"qlib_expr：未知节点 {node!r}")


def _arity(name, args, n):
    if len(args) != n:
        raise EngineError(f"qlib_expr：{name} 需要 {n} 个参数，收到 {len(args)}")


def _int_arg(node, name) -> int:
    if node[0] != "num" or not isinstance(node[1], int):
        raise EngineError(f"qlib_expr：{name} 窗口长度必须为整数字面量")
    if node[1] < 1:
        raise EngineError(f"qlib_expr：{name} 窗口长度必须 ≥ 1")
    return node[1]


class QlibExprEngine:
    def __init__(self, meta):
        self.meta = meta
        if not (meta.expr or "").strip():
            raise EngineError(f"{meta.id}: engine=qlib_expr 但定义缺少 expr")

    def run(self, db_path, start, end) -> list[dict]:
        ast_ = _Parser(self.meta.expr).parse()
        # Rank 包裹单层窗口表达式 → 子查询提升为两级 SQL（DuckDB 不允许窗口嵌套）；
        # 其余情形单层渲染。两种路径下 dt 区间过滤都在外层 —— 窗口须基于全量历史计算
        # （Ref/Mean 回看 :start 之前的 K 线，否则物化区间头部 N 日窗口为 NULL）。
        lifted = _rank_lift(ast_)
        if lifted is not None:
            w0 = _render(lifted)
            sql = (f"SELECT * FROM (SELECT dt, secid, percent_rank() OVER "
                   f"(PARTITION BY dt ORDER BY w0) :: DOUBLE AS value "
                   f"FROM (SELECT d.dt, d.secid, {w0} AS w0 FROM fl_daily_bar d) "
                   f"WHERE w0 IS NOT NULL) WHERE dt BETWEEN $start AND $end")
        else:
            sql = (f"SELECT * FROM (SELECT d.dt, d.secid, {_render(ast_)} AS value "
                   f"FROM fl_daily_bar d) WHERE dt BETWEEN $start AND $end")
        con = duckdb.connect(str(db_path), read_only=True)
        try:
            cur = con.execute(sql, {"start": start, "end": end})
            rows = [dict(zip([c[0] for c in cur.description], r)) for r in cur.fetchall()]
        finally:
            con.close()
        from factorlib.engine.base import normalize_rows
        for r in rows:  # 量价因子：无财务 PIT 元数据
            r["used_period"] = None
            r["used_announce"] = None
        return normalize_rows(rows)
