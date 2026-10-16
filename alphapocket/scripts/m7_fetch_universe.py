# M7 T-F1 正式拉取：56 标的 × 2017 起（行情+财务+分红+市值+日历）
"""一次性驱动脚本（任务执行后保留为 artifacts 留证）：universe_m7 → fetch_all(bootstrap)。

用途：``cd alphapocket && python scripts/m7_fetch_universe.py``（重跑幂等：fetch bootstrap 全量重建）。
"""
from __future__ import annotations

import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from applib.ctx import Ctx
from applib.datasource import fetch_all
from applib.universe import picked_secids

START = "2017-01-01"
FIN_START_YEAR = "2017"     # 财报主链（FIN_COLS）自 2017 起 → TTM 2017Q4 → yoy/cagr 预热达标


SOURCE = ROOT / "sandbox/artifacts/m7_data_ext/universe_m7.json"   # 真源（稳定层，fetch 不触碰）


def main() -> None:
    import shutil
    if not SOURCE.exists():
        sys.exit(f"✗ universe 真源缺失：{SOURCE}（先跑 build_universe(out_path=SOURCE)）")
    u = json.loads(SOURCE.read_text(encoding="utf-8"))
    secids = picked_secids(u)
    print(f"[m7-fetch] universe={len(secids)} 标的（A股 {len(u['picked'])} + ETF {len(u['etf_keep'])}）"
          f" start={START} fin_start_year={FIN_START_YEAR}", flush=True)
    t0 = time.time()
    res = fetch_all(Ctx(), start=START, fin_start_year=FIN_START_YEAR,
                    universe=secids, mode="bootstrap")
    # fetch bootstrap 会 rmtree remote/repo → universe_m7.json 恢复自真源（行业映射供 prepare 消费）
    dim = ROOT / "remote/repo/dim/universe_m7.json"
    dim.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy(SOURCE, dim)
    out = ROOT / "sandbox/artifacts/m7_data_ext/fetch_summary.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(res, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"[m7-fetch] done in {time.time()-t0:.0f}s → {out}", flush=True)
    print(json.dumps({k: res[k] for k in ("fetched", "universe_n", "failed") if k in res},
                     ensure_ascii=False, indent=1), flush=True)


if __name__ == "__main__":
    main()
