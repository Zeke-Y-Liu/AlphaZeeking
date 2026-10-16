# M7 T-F1 数据修补：603986.SH 新浪窗口参数缺陷导致 2017-01-03..2017-03-10 缺失
"""一次性修补脚本（保留为留证）：全量拉取取缺失段 → 合并 + pct 链重算 → provenance 登记。

背景：新浪 stock_zh_a_daily 对 603986 带 start_date 的请求返回数据被截断
（全量拉取正常：首K 2016-08-18；start=2017-01-01 时首日 2017-03-13）。
其余 55 标的窗口拉取正常（起点全部 ≤2017-02-01）。
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

SECID = "603986.SH"
SINA = "sh603986"


def _num(v):
    try:
        f = float(v)
        return f if f == f else None          # NaN → None
    except (TypeError, ValueError):
        return None


def _iso(d):
    return str(d)[:10] if d is not None else None


def main() -> None:
    import akshare as ak

    f_bars = ROOT / "remote/repo/market/daily_bars.json"
    f_raw = ROOT / "remote/repo/market/daily_bars_raw.json"
    f_prov = ROOT / "remote/repo/market/provenance.json"
    bars = json.loads(f_bars.read_text(encoding="utf-8"))
    raw = json.loads(f_raw.read_text(encoding="utf-8"))

    df = ak.stock_zh_a_daily(symbol=SINA, adjust="qfq")
    df_raw = ak.stock_zh_a_daily(symbol=SINA, adjust="")
    have = {r["dt"] for r in bars[SECID]}
    miss = [( _iso(r["date"]), _num(r["close"]), _num(r["open"]), _num(r["high"]),
              _num(r["low"]), int(r["volume"]), _num(r["amount"]))
            for _, r in df.iterrows() if _iso(r["date"]) not in have]
    print(f"[patch] {SECID} 现有 {len(bars[SECID])} 行，补段 {len(miss)} 行"
          f"（{miss[0][0]}..{miss[-1][0]}）" if miss else "[patch] 无缺失")

    if miss:
        merged = sorted(bars[SECID] + [{"dt": m[0], "open": m[2], "high": m[3], "low": m[4],
                                        "close": m[1], "volume": m[5], "amount": m[6],
                                        "pct": None} for m in miss], key=lambda r: r["dt"])
        prev = None
        for r in merged:                       # pct 链全量重算（口径与主链一致）
            c = r["close"]
            r["pct"] = (c / prev - 1) if (prev and c and prev) else None
            prev = c
        bars[SECID] = merged
        have_raw = {r["dt"] for r in raw[SECID]}
        raw[SECID] = sorted(raw[SECID] + [{"dt": _iso(r["date"]), "close": _num(r["close"])}
                                          for _, r in df_raw.iterrows()
                                          if _iso(r["date"]) not in have_raw],
                            key=lambda r: r["dt"])
        f_bars.write_text(json.dumps(bars, ensure_ascii=False), encoding="utf-8")
        f_raw.write_text(json.dumps(raw, ensure_ascii=False), encoding="utf-8")
        prov = json.loads(f_prov.read_text(encoding="utf-8"))
        main = next(s for s in prov["sources"] if s["dataset"] == "dwd_daily_bar")
        # 留痕口径：补段是一次性修补动作，并入既有 dwd_daily_bar 主条目 note（不新增
        # sources[] 行——主条目 interface/upstream 与 spec 对拍语义不被补段扰动）
        main["note"] = f"{SECID} 窗口参数缺陷补段 {len(miss)} 行（pct 链重算；见 scripts/m7_patch_603986.py）"
        f_prov.write_text(json.dumps(prov, ensure_ascii=False), encoding="utf-8")
        print(f"[patch] 完成：{SECID} 共 {len(merged)} 行，首日 {merged[0]['dt']}")


if __name__ == "__main__":
    main()
