#!/usr/bin/env python3
"""R1 本地快照数据可靠性检查（RG-4）：当前真实 workspace 六项验证，不触外网。

- 数据面：``var/ws/default``（core 运行工作区）——six-check 直读 ``sandbox/data/ap.duckdb``
  与 ``sandbox/repo``，因此要求最近一次同步已物化 DuckDB；
- ``--probe``：编排器 requires 探测模式——workspace/duckdb 存在才认为可跑（沙箱重置后
  未同步时自动 skip 而非 fail）；
- ``--only golden_test,pit_audit``：仅跑指定 check（factor.golden 复用本脚本）；
- 输出：逐项 pass/fail + JSON 摘要；任一 fail → 退出码 1。
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path("/workspace/alphapocket")
sys.path.insert(0, str(ROOT))

from applib.ctx import Ctx  # noqa: E402
from applib.runner import VERIFY_SUITE  # noqa: E402

WS = Path("/workspace/alphapocket-web/var/ws/default")


def _probe_ok() -> bool:
    return (WS / "sandbox" / "repo" / "market" / "daily_bars.json").exists() \
        and (WS / "sandbox" / "data" / "ap.duckdb").exists()


def main(argv: list[str]) -> int:
    if "--probe" in argv:
        sys.exit(0 if _probe_ok() else 1)
    only = None
    if "--only" in argv:
        only = set(argv[argv.index("--only") + 1].split(","))

    ctx = Ctx(root=WS)
    results: dict[str, str] = {}
    failed: list[str] = []
    for fn in VERIFY_SUITE:
        name = getattr(fn, "__name__", str(fn))
        if only and name not in only:
            continue
        try:
            out = fn(ctx)
            value = str(next(iter(out.values()))) if isinstance(out, dict) else str(out)
            ok = value.startswith("pass")
        except Exception as exc:  # noqa: BLE001 - 验证函数异常按 fail 记
            value, ok = f"ERROR: {type(exc).__name__}: {exc}", False
        results[name] = value
        mark = "PASS" if ok else "FAIL"
        print(f"  [{mark}] {name}: {value[:150]}")
        if not ok:
            failed.append(name)

    target = sorted(only) if only else None
    print(json.dumps({"ws": str(WS), "checks": target or "six",
                      "status": "fail" if failed else "pass", "failed": failed},
                     ensure_ascii=False))
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
