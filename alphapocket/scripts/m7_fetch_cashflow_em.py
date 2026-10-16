"""M7 T-F2：东财现金流量表折旧摊销补拉（T-F1 depreciation 缺口修正）。

T-F1 登记「新浪/东财均无折旧摊销列」→ T-F2 探测修正：东财报表接口
``ak.stock_cash_flow_sheet_by_report_em`` **可达**且含全套折旧摊销列（现金流量表
补充资料口径）。本脚本对 M7 universe 51 只 A 股补拉并按报告期并入
``remote/repo/factors/fundamental_values.json`` 的 reports（字段 dep_amort）：
    dep_amort = FA_IR_DEPR + IA_AMORTIZE + LPE_AMORTIZE + USERIGHT_ASSET_AMORTIZE
              （固定资产折旧+无形资产摊销+长期待摊费用摊销+使用权资产摊销；
                可得项求和，全缺 → None 如实）
- 只补挂既有 reports 范围内的报告期（不扩 reports 期数，最小扰动）；
- announce_date 沿用全表统一口径（PIT_LAG_DAYS 保守推，不新增来源口径）；
- provenance 登记 note 并入 dwd_financial_report 主条目（沿 T-F1 留痕口径）。

运行：``cd alphapocket && /workspace/alphapocket-web/core/.venv/bin/python scripts/m7_fetch_cashflow_em.py``
"""
from __future__ import annotations

import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

FUND = ROOT / "remote" / "repo" / "factors" / "fundamental_values.json"
PROV = ROOT / "remote" / "repo" / "market" / "provenance.json"

# 东财现金流量表折旧摊销四列（现金流量表补充资料口径，单位：元）
_DEP_COLS = ("FA_IR_DEPR", "IA_AMORTIZE", "LPE_AMORTIZE", "USERIGHT_ASSET_AMORTIZE")


def _em_symbol(secid: str) -> str:
    code, _, ex = secid.partition(".")
    return f"{ex}{code}"


def _dep_amort(r) -> float | None:
    """四列可得项求和；全缺 → None（如实，不 mock）。"""
    vals = [r.get(c) for c in _DEP_COLS]
    vals = [float(v) for v in vals if v is not None and str(v) != "nan"]
    return sum(vals) if vals else None


def main() -> int:
    import akshare as ak

    fund = json.loads(FUND.read_text(encoding="utf-8"))
    secids = sorted(fund)
    print(f"补拉 {len(secids)} 标的东财现金流量表 dep_amort")
    n_rows = n_missing = n_skip = 0
    failed: list[str] = []
    t0 = time.time()
    for i, secid in enumerate(secids, 1):
        sym = _em_symbol(secid)
        reports = fund[secid].get("reports") or []
        periods = {r["period"] for r in reports}
        try:
            df = ak.stock_cash_flow_sheet_by_report_em(symbol=sym)
        except Exception as e:                     # noqa: BLE001 - 单标的失败透明登记
            failed.append(f"{secid}:{type(e).__name__}")
            print(f"  [{i:2d}/{len(secids)}] {secid} 拉取失败 {type(e).__name__}")
            continue
        if df is None or len(df) == 0:
            failed.append(f"{secid}:EMPTY")
            continue
        hit = 0
        for _, r in df.iterrows():
            period = str(r.get("REPORT_DATE", ""))[:10]
            if period not in periods:
                continue
            v = _dep_amort(r)
            rep = next(x for x in reports if x["period"] == period)
            rep["dep_amort"] = v
            if v is None:
                n_missing += 1
            hit += 1
        n_rows += hit
        n_skip += len(df) - hit
        print(f"  [{i:2d}/{len(secids)}] {secid} 补挂 {hit}/{len(periods)} 期"
              + ("（含全缺期，如实 None）" if any(
                  (next(x for x in reports if x['period'] == p)).get('dep_amort') is None
                  for p in periods) else ""))
        time.sleep(0.3)

    FUND.write_text(json.dumps(fund, ensure_ascii=False), encoding="utf-8")

    # provenance 留痕（并入 dwd_financial_report 主条目 note，沿 T-F1 口径）
    prov = json.loads(PROV.read_text(encoding="utf-8"))
    main = next(s for s in prov["sources"] if s["dataset"] == "dwd_financial_report")
    main["note"] = (f"M7 T-F2 折旧摊销补拉：东财 stock_cash_flow_sheet_by_report_em，"
                    f"{n_rows} 期 dep_amort 并入（四列补充资料口径求和），"
                    f"失败 {len(failed)} 标的；见 scripts/m7_fetch_cashflow_em.py")
    PROV.write_text(json.dumps(prov, ensure_ascii=False, indent=1), encoding="utf-8")

    print(f"完成：{n_rows} 期补挂 / 全缺期 {n_missing} / 范围外跳过 {n_skip} / "
          f"失败 {len(failed)} {failed[:5]}，耗时 {time.time() - t0:.0f}s")
    return 0 if n_rows else 1


if __name__ == "__main__":
    raise SystemExit(main())
