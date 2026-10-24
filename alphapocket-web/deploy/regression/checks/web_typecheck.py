#!/usr/bin/env python3
"""typecheck 基线对比（RG-3）：vue-tsc 新增类型错误才 fail。

存量类型错误（历史遗留）冻结于 ``web/tests/typecheck-baseline.txt``（每行一条
``path(line,col): error TSxxxx: msg``）；运行时解析 vue-tsc 输出，与基线做集合对比：
- 出现基线外的新错误 → fail（列出全部新错误）
- 基线内错误消失 → pass（并在 stderr 提示可收缩基线）
- 基线更新：``python deploy/regression/checks/web_typecheck.py --update-baseline``
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

WEB = Path("/workspace/alphapocket-web/web")
BASELINE = WEB / "tests" / "typecheck-baseline.txt"


def _run_typecheck() -> tuple[int, list[str]]:
    r = subprocess.run(["pnpm", "exec", "vue-tsc", "--noEmit"],
                       cwd=WEB, capture_output=True, text=True, timeout=280, check=False)
    errors = []
    for line in (r.stdout + "\n" + r.stderr).splitlines():
        line = line.strip()
        if "): error TS" in line:
            errors.append(line.split("  ")[0].strip() if "  " in line[:0] else line)
    # 归一：vue-tsc 行格式固定 "path(l,c): error TSxxxx: msg"；取完整行做集合元素
    return r.returncode, errors


def main(argv: list[str] | None = None) -> int:
    if "--update-baseline" in (argv or []):
        _, errors = _run_typecheck()
        BASELINE.parent.mkdir(parents=True, exist_ok=True)
        BASELINE.write_text("\n".join(sorted(errors)) + ("\n" if errors else ""),
                            encoding="utf-8")
        print(f"基线已更新：{len(errors)} 条存量错误 → {BASELINE}")
        return 0

    _, errors = _run_typecheck()
    baseline = set(BASELINE.read_text(encoding="utf-8").splitlines()) if BASELINE.exists() else set()
    current = set(errors)
    new = current - baseline
    fixed = baseline - current

    if new:
        print(f"FAIL：出现 {len(new)} 条基线外新增类型错误")
        for e in sorted(new):
            print(f"  + {e}")
        return 1
    print(f"PASS：0 新增类型错误（当前 {len(current)} 条，基线 {len(baseline)} 条）")
    if fixed:
        print(f"提示：{len(fixed)} 条存量错误已消失，可运行 --update-baseline 收缩基线",
              file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
