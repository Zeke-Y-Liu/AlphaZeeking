"""M7 多市场扩展位（实施计划 §10 任务 10.3）：universe 扩市场 + 科目映射接口 + 空跑演练。

设计（接口预留，本仓暂只接 CN 数据源，HK/US 为映射位）：
- 统一契约：因子 SQL 只依赖 dwd_* 统一 schema（与市场无关），universe 字段
  声明市场集合（registry FactorMeta.universe，["CN"] 可扩 ["HK","US"]）；
- SCHEMA_MAPS：统一逻辑科目（dwd_financial_report 列）→ 各市场准则下源字段名。
  CN = 中国会计准则（CAS，repo 采集管道已对齐）；HK = HKFRS（香港财务报告
  准则，综合损益表/财务状况表科目）；US = US GAAP（10-K 科目）。
  新市场接入三步：上游落 raw_<mkt>_financial → 补 SCHEMA_MAPS → 注册
  MARKET_SOURCE 视图——因子定义零改动（估值/动量/低波三族先行；
  质量/成长族跨准则可比性弱，接入后须重过 §6.4 检验）；
- market_readiness：per 市场数据源就绪检查（dim_security.market 覆盖 + 必需表）；
- dry_run：一键空跑演练——目标因子 universe 加市场（运行时补丁，不改 YAML）
  → 物化 → PIT 审计 → 市场覆盖统计，无源市场 SKIP 降级并登记
  （DoD 10.3：val_pe_ttm 加 HK 后重检验流程可一键触发，结果另议）。

CLI：python -m factorlib.multimarket --repo <dir> [--market HK] [--factors val_pe_ttm] [--out <dir>]
"""
from __future__ import annotations

import json
from pathlib import Path

import duckdb

# 市场代码归一：repo dim_security.market 取值 → universe 市场代码
MARKET_ALIASES = {"A": "CN", "CN": "CN", "HK": "HK", "US": "US"}
MARKETS = ("CN", "HK", "US")
REQUIRED_TABLES = ("dwd_daily_bar", "dwd_financial_report", "dwd_dividend",
                   "dim_security")

# 统一科目 → 各市场源字段（接口预留：新市场接入时上游 raw 表按此命名对齐）
SCHEMA_MAPS: dict[str, dict[str, str]] = {
    "eps_ytd":       {"CN": "eps_ytd",    "HK": "basic_eps_ytd",      "US": "diluted_eps_ytd"},
    "revenue":       {"CN": "revenue",    "HK": "revenue",            "US": "revenues"},
    "net_profit":    {"CN": "net_profit", "HK": "profit_attributable", "US": "net_income"},
    "total_assets":  {"CN": "total_assets", "HK": "total_assets",     "US": "total_assets"},
    "total_liab":    {"CN": "total_liab", "HK": "total_liabilities",  "US": "total_liabilities"},
    "ocf":           {"CN": "ocf",        "HK": "cash_operating",     "US": "cfo"},
    "capex":         {"CN": "capex",      "HK": "capex",              "US": "capex"},
    "roe":           {"CN": "roe",        "HK": "roe",                "US": "roe"},
    "period":        {"CN": "period",     "HK": "period",             "US": "period"},
    "announce_date": {"CN": "announce_date", "HK": "announce_date",   "US": "filing_date"},
}
# 跨准则可比性提示（§10.3：质量/成长须重过 §6.4）
FAMILY_MARKET_NOTE = {
    "valuation": "先行（价格/股本口径跨市场可比）",
    "momentum": "先行（收益率口径市场无关）",
    "lowvol": "先行（收益率口径市场无关）",
    "quality": "须重过 §6.4（HKFRS/US GAAP 科目口径差异大）",
    "growth": "须重过 §6.4（财报期结构跨市场不一致）",
}


def market_readiness(con: duckdb.DuckDBPyConnection,
                     markets: tuple = MARKETS) -> dict:
    """per 市场就绪检查：必需表存在 + dim_security 覆盖（返回 {mkt: {status,...}}）。"""
    tables = {r[0] for r in con.execute(
        "SELECT table_name FROM duckdb_tables()").fetchall()}
    missing_all = [t for t in REQUIRED_TABLES if t not in tables]
    by_mkt: dict[str, int] = {}
    if "dim_security" in tables and not missing_all:
        for mkt, n in con.execute(
                "SELECT market, count(DISTINCT secid) FROM dim_security GROUP BY 1").fetchall():
            code = MARKET_ALIASES.get(mkt, mkt)
            by_mkt[code] = by_mkt.get(code, 0) + n
    return {m: {"status": ("ready" if by_mkt.get(m) else "no_source") if not missing_all
                else "missing_tables",
                "n_secs": by_mkt.get(m, 0),
                "missing_tables": missing_all}
            for m in markets}


def dry_run(repo_dir: str | Path, markets: tuple = ("CN", "HK"),
            factor_ids: tuple = ("val_pe_ttm",),
            out_dir: str | Path | None = None) -> dict:
    """一键空跑演练（DoD 10.3）：universe 加市场（运行时补丁，不改 YAML）→
    物化 → PIT 审计 → per 市场覆盖统计；无源市场 SKIP 降级并登记。

    演练语义：流程可通 = 通过；「无源市场因子值为空」不是失败（结果另议）。
    """
    from . import load_definitions, materialize, pit_audit
    from .evaluate import build_universe_db
    con = build_universe_db(repo_dir)
    readiness = market_readiness(con, markets)
    root = Path(__file__).resolve().parent
    metas = {m.id: m for m in load_definitions(root)}
    factors = []
    for fid in factor_ids:
        meta = metas[fid]
        added = [m for m in markets if m not in meta.universe]
        meta.universe = list(meta.universe) + added        # 运行时补丁（演练位）
        materialize(con, meta, mode="full")
        aud = pit_audit(con, fid)
        cov = dict(con.execute("""
            SELECT coalesce(s.market, '?'), count(DISTINCT v.secid)
            FROM factor_value_daily v LEFT JOIN dim_security s USING (secid)
            WHERE v.factor_id = ? GROUP BY 1""", [fid]).fetchall())
        cov = {MARKET_ALIASES.get(k, k): v for k, v in cov.items()}
        factors.append({
            "factor_id": fid, "universe": meta.universe,
            "n_rows": con.execute(
                "SELECT count(*) FROM factor_value_daily WHERE factor_id = ?",
                [fid]).fetchone()[0],
            "coverage_by_market": cov,
            "pit_audit": {"checked": aud["checked"], "violations": aud["violations"],
                          "pass": aud["violations"] == 0},
            "skipped_markets": [m for m in markets if readiness[m]["status"] != "ready"],
            "family_note": FAMILY_MARKET_NOTE.get(meta.family, "—"),
        })
    rep = {"repo": str(repo_dir), "markets": list(markets),
           "readiness": readiness, "factors": factors,
           "schema_maps": SCHEMA_MAPS,
           "conclusion": ("流程可一键触发 ✅（空跑：无源市场 SKIP 降级，"
                          "接入数据源后重跑即实跑；质量/成长族须重过 §6.4）")}
    if out_dir is not None:
        d = Path(out_dir)
        d.mkdir(parents=True, exist_ok=True)
        (d / "multimarket_dryrun.json").write_text(
            json.dumps(rep, ensure_ascii=False, indent=1), encoding="utf-8")
        (d / "multimarket_dryrun.md").write_text(render_md(rep), encoding="utf-8")
    return rep


def render_md(rep: dict) -> str:
    """演练报告：市场就绪表 + 因子覆盖表 + 科目映射接口说明。"""
    L = ["# M7 多市场扩展位 · 空跑演练", "",
         f"- repo：{rep['repo']} · 市场：{rep['markets']}", "",
         "## 市场就绪", "",
         "| 市场 | 状态 | 证券数 | 缺表 |", "|---|---|---|---|"]
    for m, r in rep["readiness"].items():
        L.append(f"| {m} | {r['status']} | {r['n_secs']} | "
                 f"{', '.join(r['missing_tables']) or '—'} |")
    L += ["", "## 因子重检验（universe 运行时加市场）", "",
          "| 因子 | universe | 行数 | 覆盖 | PIT 审计 | SKIP 市场 | 族备注 |",
          "|---|---|---|---|---|---|---|"]
    for f in rep["factors"]:
        cov = ", ".join(f"{k}:{v}" for k, v in f["coverage_by_market"].items()) or "—"
        aud = f["pit_audit"]
        L.append(f"| {f['factor_id']} | {','.join(f['universe'])} | {f['n_rows']} | {cov} "
                 f"| {'✅' if aud['pass'] else '❌'} ({aud['checked']}) "
                 f"| {','.join(f['skipped_markets']) or '—'} | {f['family_note']} |")
    L += ["", "## 科目映射接口（SCHEMA_MAPS，接口预留）", "",
          "| 统一科目 | CN (CAS) | HK (HKFRS) | US (GAAP) |", "|---|---|---|---|"]
    for k, v in rep["schema_maps"].items():
        L.append(f"| {k} | {v['CN']} | {v['HK']} | {v['US']} |")
    L += ["", f"## 结论", "", f"- {rep['conclusion']}"]
    return "\n".join(L) + "\n"


def main() -> None:
    """CLI：python -m factorlib.multimarket --repo <dir> [--market HK] [--factors ...]"""
    import argparse
    ap = argparse.ArgumentParser(description="M7 多市场扩展位：科目映射接口 + 空跑演练")
    ap.add_argument("--repo", default="alphapocket/remote/repo")
    ap.add_argument("--market", default="HK", help="演练追加市场（默认 HK）")
    ap.add_argument("--factors", default="val_pe_ttm", help="逗号分隔因子 id")
    ap.add_argument("--out", default=None)
    args = ap.parse_args()
    rep = dry_run(args.repo, markets=("CN", args.market),
                  factor_ids=tuple(x.strip() for x in args.factors.split(",")),
                  out_dir=args.out)
    for f in rep["factors"]:
        cov = " ".join(f"{k}:{v}" for k, v in f["coverage_by_market"].items())
        print(f"{f['factor_id']:<14} universe={','.join(f['universe'])} "
              f"rows={f['n_rows']} 覆盖[{cov}] PIT={'✅' if f['pit_audit']['pass'] else '❌'} "
              f"skip={f['skipped_markets'] or '—'}")
    print("readiness:", {m: r["status"] for m, r in rep["readiness"].items()})


if __name__ == "__main__":
    main()
