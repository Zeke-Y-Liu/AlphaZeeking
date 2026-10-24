#!/usr/bin/env python3
"""回归编排器（RG-1）：变更后触发，增量 / 全量两种模式。

用法
----
变更后（增量，推荐）::

    python deploy/regression/run.py                 # 变更集自动取 git status
    python deploy/regression/run.py --changed a.py,b.vue

全量::

    python deploy/regression/run.py --mode full     # 四大域全部 suite 项

域过滤 / 显式实时上游 / 清单::

    python deploy/regression/run.py --domain factor --domain data
    python deploy/regression/run.py --mode full --with-live   # 含 data.r2
    python deploy/regression/run.py --list

退出码：0 = 全部通过；1 = 存在 fail；2 = 用法/环境错误。
报告：终端表 + ``deploy/regression/reports/last.json``（结构化，含变更集与计划）。
"""
from __future__ import annotations

import argparse
import fnmatch
import json
import os
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from suite import CORE_FUNCTIONS, MAP, SUITE, TestItem  # noqa: E402

ROOT = Path("/workspace")
REPORT_DIR = Path(__file__).resolve().parent / "reports"
DOMAINS = ("core", "interaction-api", "interaction-web", "interaction-e2e", "data", "factor")


# ── 结果结构 ─────────────────────────────────────────────────────


@dataclass
class ItemResult:
    name: str
    domain: str
    kind: str
    status: str            # pass | fail | skip | timeout
    duration_s: float = 0.0
    detail: str = ""       # 失败/跳过原因或输出尾部
    cmd: list[str] = field(default_factory=list)


# ── 变更集收集 ───────────────────────────────────────────────────


def _collect_changes(explicit: list[str] | None) -> tuple[list[str], list[str]]:
    """返回 (变更路径列表, 采集方式说明)。显式 --changed 优先；否则 git status。"""
    if explicit:
        files: list[str] = []
        for chunk in explicit:
            files.extend(p for p in chunk.split(",") if p.strip())
        return files, "--changed 显式指定"
    out = subprocess.run(
        ["git", "-C", str(ROOT), "status", "--short"],
        capture_output=True, text=True, timeout=30, check=False,
    ).stdout
    seen: dict[str, None] = {}
    for line in out.splitlines():
        path = line[3:].strip().strip('"')
        # 重命名 "a => b" 取 b；仅取 b 即变更后路径
        if " => " in path:
            path = path.split(" => ")[-1]
        if path:
            seen.setdefault(path, None)
    return list(seen), "git status（工作区 + 暂存区，含未跟踪）"


def _candidates(path: str) -> list[str]:
    """归一候选：git status 带仓库前缀（alphapocket-web/…、alphapocket/…）；
    --changed 手传常为相对工作区路径 → 补全两个顶层前缀各试一次。"""
    if path.startswith(("alphapocket", "docs/")):
        return [path]
    return [path, f"alphapocket-web/{path}", f"alphapocket/{path}"]


def _map_changes(changes: list[str]) -> tuple[list[str], list[str]]:
    """变更路径 → suite 项名（去重保序）。返回 (命中项, 未映射路径)。"""
    hits: dict[str, None] = {}
    unmapped: list[str] = []
    for path in changes:
        if not any(fnmatch.fnmatch(c, pattern)
                   for c in _candidates(path)
                   for pattern in MAP):
            unmapped.append(path)
            continue
        for pattern, targets in MAP.items():
            if any(fnmatch.fnmatch(c, pattern) for c in _candidates(path)):
                for t in targets:
                    hits.setdefault(t, None)
    return list(hits), unmapped


# ── 计划构建 ─────────────────────────────────────────────────────


def _check_registry() -> None:
    """注册表健康自检：核心功能清单引用的测试文件必须存在（相对 alphapocket-web）。"""
    alphaweb = ROOT / "alphapocket-web"
    broken = []
    for fn, files in CORE_FUNCTIONS.items():
        for f in files:
            if not (alphaweb / f).exists():
                broken.append(f"{fn} -> {f}")
    if broken:
        print("注册表腐化：CORE_FUNCTIONS 引用了不存在的测试文件：", file=sys.stderr)
        for b in broken:
            print(f"  {b}", file=sys.stderr)
        sys.exit(2)


def _build_plan(mode: str, domains: list[str], with_live: bool,
                changes: list[str]) -> tuple[list[TestItem], list[str]]:
    """返回 (计划项, 说明列表)。"""
    by_name = {t.name: t for t in SUITE}
    plan: dict[str, None] = {}
    notes: list[str] = []

    if mode == "full":
        for t in SUITE:
            plan.setdefault(t.name, None)
        notes.append("全量模式：全部 suite 项")
    else:
        # 恒定冒烟集：任何变更都跑
        for t in SUITE:
            if t.smoke:
                plan.setdefault(t.name, None)
        hits, unmapped = _map_changes(changes)
        for name in hits:
            plan.setdefault(name, None)
        notes.append(f"增量模式：冒烟 {sum(1 for t in SUITE if t.smoke)} 项 + 影响命中"
                     f" {len(hits)} 项")
        if unmapped:
            notes.append(f"WARN 未映射变更（兜底仅冒烟覆盖）: {', '.join(unmapped[:8])}"
                        f"{'…' if len(unmapped) > 8 else ''}")

    if domains:
        plan = {k: None for k in plan if by_name[k].domain in domains}
        notes.append(f"域过滤: {', '.join(domains)}")

    if not with_live:
        dropped = [n for n in plan if by_name[n].live]
        if dropped:
            notes.append(f"live 项未启用（--with-live 开启）: {', '.join(dropped)}")
        plan = {k: None for k in plan if not by_name[k].live}
    return [by_name[n] for n in plan], notes


# ── 执行 ─────────────────────────────────────────────────────────


def _probe(item: TestItem) -> str | None:
    """requires 探测：非零返回 skip 原因；空 requires 恒可跑。"""
    if not item.requires:
        return None
    try:
        r = subprocess.run(item.requires, capture_output=True, text=True,
                           timeout=60, cwd=item.cwd, check=False)
    except (OSError, subprocess.TimeoutExpired) as exc:
        return f"环境探测失败: {type(exc).__name__}: {exc}"
    if r.returncode != 0:
        tail = (r.stdout + r.stderr).strip().splitlines()[-1:] or ["(无输出)"]
        return f"环境不满足（requires 非零）: {tail[0][:120]}"
    return None


def _run_item(item: TestItem) -> ItemResult:
    res = ItemResult(item.name, item.domain, item.kind, "pass", cmd=item.cmd)
    reason = _probe(item)
    if reason:
        res.status, res.detail = "skip", reason
        return res
    env = {**os.environ, **item.env}
    t0 = time.monotonic()
    try:
        r = subprocess.run(item.cmd, cwd=item.cwd, env=env, capture_output=True,
                           text=True, timeout=item.timeout)
        res.duration_s = round(time.monotonic() - t0, 1)
        if r.returncode != 0:
            res.status = "fail"
            merged = (r.stdout + "\n" + r.stderr).strip().splitlines() or ["(无输出)"]
            res.detail = " | ".join(merged[-6:])[:400]
    except subprocess.TimeoutExpired:
        res.duration_s = item.timeout
        res.status, res.detail = "timeout", f"超过 {item.timeout}s"
    except OSError as exc:
        res.status, res.detail = "fail", f"无法执行: {exc}"
    return res


# ── 报告 ─────────────────────────────────────────────────────────


def _print_report(results: list[ItemResult], notes: list[str], mode: str,
                  changes: list[str], change_src: str, took_s: float) -> int:
    width = max([len(r.name) for r in results] + [12])
    print(f"\n回归报告 · mode={mode} · 变更集={len(changes)}（{change_src}）· {took_s:.0f}s")
    for n in notes:
        print(f"  · {n}")
    print(f"\n  {'SUITE':<{width}}  {'DOMAIN':<18} {'STATUS':<7} {'TIME':>6}  DETAIL")
    print("  " + "-" * (width + 60))
    for r in results:
        print(f"  {r.name:<{width}}  {r.domain:<18} {r.status:<7} {r.duration_s:>5}s  "
              f"{r.detail[:90]}")
    counts = {s: sum(1 for r in results if r.status == s)
              for s in ("pass", "fail", "skip", "timeout")}
    print("\n  合计: pass={pass} fail={fail} skip={skip} timeout={timeout}".format(**counts))
    return 1 if (counts["fail"] or counts["timeout"]) else 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="AlphaPocket 回归编排器")
    ap.add_argument("--mode", choices=("incremental", "full"), default="incremental")
    ap.add_argument("--changed", help="显式变更文件（逗号分隔）；缺省取 git status")
    ap.add_argument("--domain", action="append", default=[],
                    help=f"域过滤（可多次）: {', '.join(DOMAINS)}")
    ap.add_argument("--with-live", action="store_true",
                    help="纳入实时上游项（data.r2，触外网）")
    ap.add_argument("--list", action="store_true", help="列出 suite 与映射，不执行")
    args = ap.parse_args(argv)

    _check_registry()
    if args.list:
        for t in SUITE:
            tag = ("[smoke]" if t.smoke else "") + ("[live]" if t.live else "")
            print(f"  {t.name:<18} {t.domain:<18} {t.kind:<12} {tag}  {t.desc}")
        print("\n影响映射:")
        for k, v in MAP.items():
            print(f"  {k:<48} -> {', '.join(v) or '(仅冒烟兜底)'}")
        return 0

    changes, change_src = _collect_changes(
        args.changed.split(",") if args.changed else None)
    if args.mode == "incremental" and not changes:
        print("工作区无变更（git status 干净）：仅执行恒定冒烟集。")

    plan, notes = _build_plan(args.mode, args.domain, args.with_live, changes)
    if not plan:
        print("计划为空（域过滤后无匹配项）。")
        return 0
    print(f"计划 {len(plan)} 项: {', '.join(t.name for t in plan)}")

    t0 = time.monotonic()
    results = [_run_item(t) for t in plan]
    took = time.monotonic() - t0

    REPORT_DIR.mkdir(parents=True, exist_ok=True)
    report = {
        "mode": args.mode,
        "with_live": args.with_live,
        "domains": args.domain,
        "changes": changes,
        "changes_source": change_src,
        "notes": notes,
        "started_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "duration_s": round(took, 1),
        "items": [vars(r) | {"cmd": r.cmd} for r in results],
        "summary": {s: sum(1 for r in results if r.status == s)
                    for s in ("pass", "fail", "skip", "timeout")},
    }
    (REPORT_DIR / "last.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"报告已写: {REPORT_DIR / 'last.json'}")
    return _print_report(results, notes, args.mode, changes, change_src, took)


if __name__ == "__main__":
    sys.exit(main())
