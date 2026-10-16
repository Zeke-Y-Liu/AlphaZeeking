"""T-F9 科目映射接口（计划 10.3）：HKFRS/US GAAP → 内部科目名 + 多市场就绪性预检。

接口预留不冒进（T-F9 红线）：本模块只做三件事——
1) dim/account_mapping.json 的读取与校验（映射缺失 → 逐条列缺项，不静默）；
2) 内部科目 ↔ 外部标准科目名的双向映射（往返可核）；
3) 扩展市场（HK/US）就绪性预检：科目映射 + 行情准入 + 财务源数据行，缺口显式披露。

**不实现** HK/US 财务拉取或因子重检验——V3 §6.3 要求质量/成长分市场重过 §6.4，
那是 M8+ 的事；预检结论只用于披露（「结果可另议」的诚实实现），不做任何替代计算。

依赖方向：factorlib 内部模块（零 applib import）；duckdb 只读直连（与 lifecycle 同口径）。
"""
from __future__ import annotations

import json
from pathlib import Path

import duckdb

MAP_REL = Path("dim") / "account_mapping.json"      # 相对 repo 根
MARKETS = ("CN", "HK", "US")

# 财务表 → 预检最低内部科目集（接口预留粒度：表在 factor inputs 中出现即视为财务依赖；
# 科目集为 M7 已落地财务因子的并集下界，M8+ 分市场重检验时按因子细化为逐科目需求）
TABLE_ACCOUNTS: dict[str, tuple[str, ...]] = {
    "fl_dws_financial_ttm": ("eps", "revenue", "net_profit"),
}


class AccountMapError(Exception):
    """科目映射表非法 / 市场未配置。"""


def load_account_map(repo: Path) -> dict:
    """读取 dim/account_mapping.json；文件缺失 → {"markets": {}}（校验函数会把
    全部需求科目判缺，不抛异常——缺口走披露路径）。"""
    p = Path(repo) / MAP_REL
    if not p.exists():
        return {"markets": {}}
    return json.loads(p.read_text(encoding="utf-8"))


def _market_entry(mapping: dict, market: str) -> dict:
    return (mapping.get("markets") or {}).get(market) or {}


def external_name(mapping: dict, market: str, internal: str) -> str | None:
    """内部科目 → 外部标准科目名（HKFRS/US GAAP）。未映射 → None。"""
    return (_market_entry(mapping, market).get("mappings") or {}).get(internal)


def internal_name(mapping: dict, market: str, external: str) -> str | None:
    """外部科目 → 内部科目（反向映射，往返校验用）。未命中 → None。"""
    for k, v in (_market_entry(mapping, market).get("mappings") or {}).items():
        if v == external:
            return k
    return None


def validate_market(repo: Path, market: str, required: list[str] | tuple[str, ...]) -> dict:
    """required 内部科目在 market 的映射核验：缺失 → missing 逐条列名（不静默）。

    返回 {market, standard, ok, mapped, missing, reason}；市场未配置 → 全部需求科目判缺。
    """
    if market not in MARKETS:
        raise AccountMapError(f"市场枚举非法：{market!r}（允许 {MARKETS}）")
    mapping = load_account_map(repo)
    entry = _market_entry(mapping, market)
    mapped = entry.get("mappings") or {}
    missing = sorted(set(required) - set(mapped))
    return {"market": market, "standard": entry.get("standard"),
            "ok": not missing, "mapped": sorted(mapped), "missing": missing,
            "reason": ("映射齐备" if not missing else
                       (f"{market} 无科目映射（{MAP_REL} 未配置该市场）" if not mapped
                        else f"科目映射缺失：{missing}（报错列缺项，不静默）"))}


def _count_rows(db_path: Path | None, sql: str, params: list) -> int | None:
    """只读计数；库/表缺失 → None（调用方把 None 披露为缺口，不抛异常）。"""
    if db_path is None or not Path(db_path).exists():
        return None
    con = duckdb.connect(str(db_path), read_only=True)
    try:
        return con.execute(sql, params).fetchone()[0]
    except duckdb.Error:
        return None
    finally:
        con.close()


def market_readiness(repo: Path, factors: list[dict], markets: list[str],
                     db_path: Path | None = None) -> dict:
    """扩展市场就绪性预检（不抛异常，缺口显式披露）。

    factors: [{"id": str, "inputs": [str, ...]}]（调用方从 FactorMeta 取）。
    逐市场核验三块，gaps 逐条人读字符串：
      1) 科目映射——validate_market（映射缺 → 缺项列名）；
      2) 行情准入——fl_daily_bar 中 {MKT} 后缀标的行（0/库缺 → 披露）；
      3) 财务源——inputs 含财务表的因子清单 × 该市场财务数据行（0 → 「财务源缺失 →
         财务因子不可用：<清单>」）。
    返回 {"markets": {mkt: {...}}, "note": 预检口径说明}；CN 为主链现行原生口径，直接 ok。
    """
    fin_tables = sorted({t for f in (factors or []) for t in (f.get("inputs") or [])
                         if t in TABLE_ACCOUNTS})
    required_accounts = sorted({a for t in fin_tables for a in TABLE_ACCOUNTS[t]})
    fin_factor_ids = sorted({f["id"] for f in (factors or [])
                             if any(t in TABLE_ACCOUNTS for t in (f.get("inputs") or []))})
    out: dict[str, dict] = {}
    for mkt in markets:
        seg: dict = {"market": mkt, "ok": True, "gaps": []}
        if mkt == "CN":
            seg.update({"standard": "内部科目（原生口径）", "rechecked": True,
                        "note": "CN 为主链现行口径，无预检项"})
            out[mkt] = seg
            continue
        # 1) 科目映射
        vm = validate_market(repo, mkt, required_accounts)
        seg["standard"] = vm["standard"]
        seg["missing_accounts"] = vm["missing"]
        if not vm["ok"]:
            seg["ok"] = False
            seg["gaps"].append(
                f"{mkt} 科目映射缺失（{vm['standard'] or '标准未声明'}）：{vm['reason']}")
        # 2) 行情准入
        bars = _count_rows(db_path, "SELECT count(DISTINCT secid) FROM fl_daily_bar "
                                    "WHERE secid LIKE ?", [f"%.{mkt}"])
        seg["bar_secids"] = bars
        if bars is None:
            seg["ok"] = False
            seg["gaps"].append(f"{mkt} 行情准入不可核验（行情库缺失/表不存在）")
        elif bars == 0:
            seg["ok"] = False
            seg["gaps"].append(f"{mkt} 行情未接入：fl_daily_bar 无 .{mkt} 标的行")
        # 3) 财务源
        fin_rows = _count_rows(db_path, "SELECT count(*) FROM fl_dws_financial_ttm "
                                         "WHERE secid LIKE ?", [f"%.{mkt}"]) \
            if fin_factor_ids else 0
        seg["fin_rows"] = fin_rows
        seg["financial_factors"] = fin_factor_ids
        if fin_factor_ids and (fin_rows is None or fin_rows == 0):
            seg["ok"] = False
            why = "财务表不存在/行情库缺失" if fin_rows is None else "无该市场财务数据行"
            seg["gaps"].append(
                f"{mkt} 财务源缺失（{vm.get('standard') or '外部准则'}财务数据未接入：{why}）"
                f"→ 财务因子不可用：{'、'.join(fin_factor_ids)}")
        seg["rechecked"] = False           # 预检 ≠ 重检验：M8+ 分市场重过 §6.4 才算
        out[mkt] = seg
    return {"markets": out,
            "note": "就绪性预检 ≠ 重检验：扩展市场因子结论以 M8+ 分市场重过 §6.4 为准"
                    "（V3 §6.3）；本预检只做缺口披露，不做替代计算"}
