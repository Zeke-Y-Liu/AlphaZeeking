"""R2 · Walk-Forward 样本外协议（《研究缺口R1-R4实施计划》§4）

协议（§4.1）
-----------
扩展窗口滚动验证 + 封存样本外（lockbox）：train 起点固定、终点随折扩展；
valid 等长前滚；数据末端保留 ≥ 一个 valid 长度的 lockbox，**每 factor version 至多验证一次**。

    fold     train                     valid
    1        2021-01 ~ 2022-12         2023-01 ~ 2023-06
    ...      （扩展窗口，步长 step_months）
    lockbox  —（封存）                 2026-01 ~ 数据末端

参数可配 ``train_min_months / valid_months / step_months``；α 阶段用 24/6/6 跑通机制，
生产接入 2016 起全量历史后切换 60/12/12（功效更强）。

产物：``sandbox/artifacts/wf_<factor>_<ts>/{validation.json, validation.md}``
纪律：lockbox 内同一 factor_id+version 出现第二份验证报告 → ``audit_split`` FAIL；重切样本须升 version。
"""
from __future__ import annotations

import calendar
import json
import time
from itertools import pairwise
from pathlib import Path

from . import factors as F

# ── 切分配置（§4.1）────────────────────────────────────
DEFAULT_CFG = {"train_min_months": 24, "valid_months": 6, "step_months": 6}
PROD_CFG = {"train_min_months": 60, "valid_months": 12, "step_months": 12}
DEFAULT_HORIZON = 20          # 前瞻收益窗口（交易日）：信号因子取 registry.horizon_days
LOCKBOX_BUFFER = 0.02         # 样本外「有效」判定阈值（|均值 IC|）


# ── 月份算术（月末日边界，§4.4 边界用例）──────────────────
def _add_months(ym: str, n: int) -> str:
    """'YYYY-MM' + n 个月 → 'YYYY-MM'（n 可为负）"""
    y, m = int(ym[:4]), int(ym[5:7])
    tot = y * 12 + (m - 1) + n
    return f"{tot // 12:04d}-{tot % 12 + 1:02d}"


def _month_start(ym: str) -> str:
    return f"{ym}-01"


def _month_end(ym: str) -> str:
    """该月最后一个自然日（闰年 2 月由 calendar 精确给出）"""
    y, m = int(ym[:4]), int(ym[5:7])
    return f"{ym}-{calendar.monthrange(y, m)[1]:02d}"


def _months_between(a: str, b: str) -> int:
    """'YYYY-MM' 的月数差 b-a"""
    return (int(b[:4]) * 12 + int(b[5:7])) - (int(a[:4]) * 12 + int(a[5:7]))


# ── 切分引擎（§4.1）───────────────────────────────────
def make_folds(data_range: tuple[str, str], cfg: dict | None = None) -> dict:
    """给定数据区间与配置 → 扩展窗口滚动 folds + lockbox 区间。

    - train 起点固定 = 数据首月首日；train 终点 = 本折 valid 起始前一月月末；
    - valid 等长 ``valid_months``，自 ``train_min_months`` 起、按 ``step_months`` 前滚；
    - 停折条件：再补一折后剩余数据 < ``valid_months``（保证 lockbox 至少一个 valid 长度）；
    - lockbox = 末折 valid 结束次月首日 ~ 数据末端。
    """
    cfg = {**DEFAULT_CFG, **(cfg or {})}
    start, end = str(data_range[0]), str(data_range[1])
    tm, vm, st = int(cfg["train_min_months"]), int(cfg["valid_months"]), int(cfg["step_months"])
    if min(tm, vm, st) <= 0:
        raise ValueError("train_min_months / valid_months / step_months 必须为正整数")
    y0 = start[:7]
    span = _months_between(y0, end[:7])            # 数据末月相对首月的偏移
    folds, k = [], 0
    while True:
        voff = tm + k * st                         # valid 起始月偏移
        veoff = voff + vm - 1                      # valid 结束月偏移
        if veoff > span or veoff + vm > span:      # valid 越界，或 lockbox 不足一个 valid 长度
            break
        folds.append({
            "fold": len(folds) + 1,
            "train": [_month_start(y0), _month_end(_add_months(y0, voff - 1))],
            "valid": [_month_start(_add_months(y0, voff)), _month_end(_add_months(y0, veoff))],
        })
        k += 1
    if folds:
        last = folds[-1]["valid"][1]
        lock_start = _month_start(_add_months(last[:7], 1))
    else:
        lock_start = _month_start(_add_months(y0, tm))
    return {"config": {"train_min_months": tm, "valid_months": vm, "step_months": st},
            "data_range": [start, end], "n_folds": len(folds), "folds": folds,
            "lockbox": [lock_start, end]}


# ── 秩相关（IC）────────────────────────────────────────
def _rank(xs: list[float]) -> list[float]:
    idx = sorted(range(len(xs)), key=lambda i: xs[i])
    r = [0.0] * len(xs)
    i = 0
    while i < len(idx):
        j = i
        while j + 1 < len(idx) and xs[idx[j + 1]] == xs[idx[i]]:
            j += 1
        avg = (i + j) / 2 + 1.0                    # 并列取平均秩
        for m in range(i, j + 1):
            r[idx[m]] = avg
        i = j + 1
    return r


def _spearman(xs: list[float], ys: list[float]) -> float | None:
    """Spearman 秩相关（截面 IC）；样本 < 3 或任一常数列 → None"""
    n = len(xs)
    if n < 3:
        return None
    rx, ry = _rank(xs), _rank(ys)
    mx, my = sum(rx) / n, sum(ry) / n
    cov = sum((rx[i] - mx) * (ry[i] - my) for i in range(n))
    vx = sum((rx[i] - mx) ** 2 for i in range(n))
    vy = sum((ry[i] - my) ** 2 for i in range(n))
    return cov / (vx * vy) ** 0.5 if vx > 0 and vy > 0 else None


# ── 因子取值（按 as_of 重算历史序列，PIT 一致）──────────
def _factor_value(meta: dict, secid: str, bars_s: list[dict], fd: dict, d: str):
    if (meta.get("type") or "alpha") == "signal":
        return F.compute_beta(secid, bars_s, d).get(meta["id"])
    return F.compute_alpha(secid, bars_s, fd.get("reports", []),
                           fd.get("dividends", []), d).get(meta["id"])


def _month_end_dates(days: list[str]) -> list[str]:
    """交易日列表 → 每月最后一个交易日（月末再平衡/截面观测日）"""
    return [d for i, d in enumerate(days)
            if i + 1 == len(days) or days[i + 1][:7] != d[:7]]


def _eval_fold(meta: dict, secids: list[str], bars: dict, fund: dict,
               valid: list[str], horizon: int) -> dict:
    """单折 valid 窗口的截面 IC：月末观测 × 前瞻 horizon 日收益（标签须完整落在 valid 内）"""
    vstart, vend = valid
    all_dates = sorted({r["dt"] for s in secids for r in bars[s]})
    closes = {s: [(r["dt"], r["close"]) for r in bars[s] if r.get("close")] for s in secids}
    cdict = {s: dict(v) for s, v in closes.items()}
    dd = {s: [d for d, _ in v] for s, v in closes.items()}
    pos = {s: {d: i for i, d in enumerate(dd[s])} for s in secids}
    vdays = [d for d in all_dates if vstart <= d <= vend]
    ics, pooled_x, pooled_y, used = [], [], [], []
    for d in _month_end_dates(vdays):
        xs, ys = [], []
        for s in secids:
            v = _factor_value(meta, s, bars[s], fund.get(s, {}), d)
            i = pos[s].get(d)
            if v is None or i is None or i + horizon >= len(dd[s]):
                continue
            ld = dd[s][i + horizon]
            if ld > vend:                          # 标签越出 valid → 弃用，避免触 lockbox
                continue
            xs.append(v)
            ys.append(cdict[s][ld] / cdict[s][d] - 1)
        ic = _spearman(xs, ys)
        if ic is not None:
            ics.append(ic)
            pooled_x += xs
            pooled_y += ys
            used.append(d)
    n_obs = len(pooled_x)
    if not ics:
        return {"n_dates": 0, "n_obs": n_obs, "ic_mean": None, "ic_std": None,
                "ic_ir": None, "ic_pos_rate": None, "ic_pooled": None,
                "data_window": list(valid)}
    mean = sum(ics) / len(ics)
    sd = (sum((x - mean) ** 2 for x in ics) / (len(ics) - 1)) ** 0.5 if len(ics) > 1 else 0.0
    return {"n_dates": len(ics), "n_obs": n_obs, "ic_mean": mean, "ic_std": sd,
            "ic_ir": (mean / sd if sd > 0 else None),
            "ic_pos_rate": sum(1 for x in ics if x > 0) / len(ics),
            "ic_pooled": _spearman(pooled_x, pooled_y),
            "data_window": [used[0], used[-1]]}


def run_validation(factor_id: str, bars: dict, fund: dict, registry: dict,
                   cfg: dict | None = None, start: str | None = None,
                   end: str | None = None) -> dict:
    """逐折样本外验证（骨架）：重算因子历史 → 截面 IC → folds 汇总 + lockbox 登记。

    机制已跑通；α 数据仅 2021 起，统计功效待 2016 起全量历史接入后补强（§8 风险）。
    """
    meta = next((f for f in registry.get("factors", []) if f["id"] == factor_id), None)
    if meta is None:
        return {"error": f"因子未注册：{factor_id}"}
    secids = [s for s, arr in bars.items() if arr]
    drange = sorted({r["dt"] for s in secids for r in bars[s]})
    if not drange:
        return {"error": "无行情数据"}
    data_range = (start or drange[0], end or drange[-1])
    split = make_folds(data_range, cfg)
    horizon = int((cfg or {}).get("horizon_days") or meta.get("horizon_days") or DEFAULT_HORIZON)
    folds = []
    for fo in split["folds"]:
        m = _eval_fold(meta, secids, bars, fund, fo["valid"], horizon)
        folds.append({"fold": fo["fold"], "train": fo["train"], "valid": fo["valid"],
                      "data_window": m.pop("data_window"), "metrics": m})
    ic_means = [f["metrics"]["ic_mean"] for f in folds if f["metrics"]["ic_mean"] is not None]
    avg = sum(ic_means) / len(ic_means) if ic_means else None
    if avg is None:
        verdict = "样本不足/无可用观测（待全量历史补强）"
    elif avg > LOCKBOX_BUFFER:
        verdict = f"样本外有效（{len(ic_means)} 折均值 IC {avg:+.3f}）"
    elif avg < -LOCKBOX_BUFFER:
        verdict = f"样本外反向（{len(ic_means)} 折均值 IC {avg:+.3f}）→ 触发评审"
    else:
        verdict = f"样本外近零（{len(ic_means)} 折均值 IC {avg:+.3f}）→ 待评审"
    return {
        "factor_id": factor_id, "name": meta.get("name"), "type": meta.get("type", "alpha"),
        "version": meta.get("version"), "protocol": "walk_forward_v1",
        "generated": time.strftime("%Y-%m-%dT%H:%M:%S"), "code_version": "R2",
        "cfg": split["config"], "horizon_days": horizon,
        "data_range": list(data_range), "n_folds": len(folds), "folds": folds,
        "lockbox": split["lockbox"], "lockbox_used": False,
        "summary": {"avg_ic": avg, "n_folds_valid": len(ic_means),
                    "fold_ic": [{"fold": f["fold"], "valid": f["valid"],
                                 "ic_mean": f["metrics"]["ic_mean"],
                                 "ic_ir": f["metrics"]["ic_ir"]} for f in folds]},
        "verdict": verdict,
        "notes": [
            "扩展窗口滚动验证：train 起点固定、终点随折扩展；valid 等长前滚（§4.1）。",
            "lockbox 封存：同一 factor_id+version 至多验证一次（audit_split ③ 强制）。",
            "截面 IC = 月末观测 × 前瞻收益的 Spearman 秩相关；标签须完整落于 valid 内（不触 lockbox）。",
            f"样本区间 {data_range[0]} ~ {data_range[1]}：机制已验证，统计功效待 2016 起全量历史接入后补强。",
        ],
    }


# ── 审计（§4.3，四查）──────────────────────────────────
def _overlap(a: list, b: list) -> bool:
    """区间 [a0,a1] 与 [b0,b1] 是否相交（b1=None 视为开区间至无穷）"""
    if not (a and b):
        return False
    a0, a1 = a[0], a[1]
    b0, b1 = b[0], b[1]
    return not ((a1 is not None and a1 < b0) or (b1 is not None and b1 < a0))


def audit_records(registry: dict, reports: list[dict]) -> dict:
    """四查纯函数（不依赖文件系统，便于注入测试）：
    ① 字段完备 ② 取数越界 ③ lockbox 一次性 ④ 折叠无重叠
    """
    factors = registry.get("factors", [])
    checks: dict[str, dict] = {}

    # ① 全部 active 因子带 validation.protocol
    missing = [f["id"] for f in factors
               if f.get("status") == "active" and not (f.get("validation") or {}).get("protocol")]
    checks["fields"] = {"ok": not missing, "missing": missing,
                        "detail": (f"{len(missing)} 个 active 因子缺 validation.protocol：{missing}"
                                   if missing else "全部 active 因子带 validation.protocol")}

    # ② 检验报告声明的数据窗口 ⊆ 对应 fold train/valid；触及 lockbox 须显式标记使用
    viol = []
    for r in reports:
        fid = r.get("factor_id")
        lb = r.get("lockbox")
        for fo in r.get("folds", []):
            dw, tr, va = fo.get("data_window"), fo.get("train"), fo.get("valid")
            if not (dw and tr and va):
                continue
            lo, hi = min(tr[0], va[0]), va[1]
            if dw[0] < lo or dw[1] > hi:
                viol.append(f"{fid}#fold{fo.get('fold')}: 数据窗口 {dw} ⊄ [{lo}, {hi}]")
            elif lb and not r.get("lockbox_used") and _overlap(dw, lb):
                viol.append(f"{fid}#fold{fo.get('fold')}: 触及 lockbox {lb} 但 lockbox_used=false")
    checks["bounds"] = {"ok": not viol, "violations": viol,
                        "detail": (f"{len(viol)} 处取数越界" if viol
                                   else f"全部 {len(reports)} 份报告数据窗口合规")}

    # ③ lockbox 一次性：同 factor_id+version 的 lockbox 验证报告 ≤ 1
    used: dict[tuple, list] = {}
    for r in reports:
        lb = r.get("lockbox")
        touched = bool(r.get("lockbox_used")) or any(
            _overlap(fo.get("data_window"), lb) for fo in r.get("folds", []) if fo.get("data_window"))
        if touched:
            used.setdefault((r.get("factor_id"), r.get("version")), []).append(
                r.get("generated") or r.get("report_id") or "?")
    dup = {f"{k[0]}@{k[1]}": v for k, v in used.items() if len(v) > 1}
    checks["lockbox"] = {"ok": not dup, "duplicates": dup,
                         "detail": (f"lockbox 重复验证：{dup}" if dup
                                    else "lockbox 一次性合规（无同版本二次验证）")}

    # ④ 折叠无重叠：valid 互不重叠且均 < lockbox 起点
    viol4 = []
    for f in factors:
        val = f.get("validation") or {}
        if val.get("protocol") != "walk_forward_v1":
            continue
        vs = sorted((fo["valid"][0], fo["valid"][1]) for fo in (val.get("folds") or []))
        viol4 += _fold_overlaps(f["id"], vs, val.get("lockbox"))
    for r in reports:
        vs = sorted((fo["valid"][0], fo["valid"][1]) for fo in r.get("folds", []) if fo.get("valid"))
        viol4 += _fold_overlaps(r.get("factor_id"), vs, r.get("lockbox"))
    checks["folds"] = {"ok": not viol4, "violations": viol4,
                       "detail": (f"{len(viol4)} 处折叠重叠/越界" if viol4
                                  else "全部 folds 的 valid 互不重叠且不覆盖 lockbox")}

    ok = all(c["ok"] for c in checks.values())
    fails = [k for k, c in checks.items() if not c["ok"]]
    return {"audit_split": "pass" if ok else f"FAIL({'·'.join(fails)})",
            "checks": checks, "n_reports": len(reports)}


def _fold_overlaps(tag, vs: list[tuple[str, str]], lockbox: list | None) -> list[str]:
    out = []
    for (a0, a1), (b0, _b1) in pairwise(vs):
        if b0 <= a1:
            out.append(f"{tag}: fold valid [{a0}, {a1}] 与 [{b0}, …] 重叠")
    if lockbox:
        for a0, a1 in vs:
            if a1 >= lockbox[0]:
                out.append(f"{tag}: fold valid [{a0}, {a1}] 覆盖/进入 lockbox {lockbox[0]}")
    return out


def load_reports(artifacts_dir: Path) -> list[dict]:
    """扫描 wf_<factor>_<ts>/validation.json 产物"""
    out = []
    for p in sorted(Path(artifacts_dir).glob("wf_*/validation.json")):
        try:
            out.append(json.loads(p.read_text(encoding="utf-8")))
        except (OSError, json.JSONDecodeError):
            continue
    return out


def audit_split(ctx, registry: dict | None = None, reports: list[dict] | None = None) -> dict:
    """工具入口：registry（local_repo）+ 产物登记（artifacts/wf_*）双查"""
    if registry is None:
        registry = json.loads((ctx.local_repo / "factors" / "registry.json").read_text(encoding="utf-8"))
    if reports is None:
        reports = load_reports(ctx.artifacts_dir)
    return audit_records(registry, reports)


# ── 渲染 ───────────────────────────────────────────────
def _num(v, d=3):
    return "—" if v is None else f"{v:.{d}f}"


def render_md(rep: dict) -> str:
    L = [f"# Walk-Forward 验证报告 · {rep['name']}（{rep['factor_id']}@{rep['version']}）", ""]
    L.append(f"协议 `{rep['protocol']}` · 区间 `{rep['data_range'][0]} ~ {rep['data_range'][1]}` · "
             f"配置 `{json.dumps(rep['cfg'], ensure_ascii=False)}` · 前瞻 {rep['horizon_days']} 交易日 · "
             f"生成 `{rep['generated']}`")
    L.append("")
    L.append(f"**结论**：{rep['verdict']}")
    L.append("")
    L.append("| fold | train | valid | 观测日 | 截面 IC 均值 | IC_IR | IC>0 | 数据窗口 |")
    L.append("| --- | --- | --- | --- | --- | --- | --- | --- |")
    for f in rep["folds"]:
        m = f["metrics"]
        pr = "—" if m["ic_pos_rate"] is None else f"{m['ic_pos_rate'] * 100:.0f}%"
        L.append(f"| {f['fold']} | {f['train'][0]} ~ {f['train'][1]} | {f['valid'][0]} ~ {f['valid'][1]} "
                 f"| {m['n_dates']} | {_num(m['ic_mean'])} | {_num(m['ic_ir'])} | {pr} "
                 f"| {f['data_window'][0]} ~ {f['data_window'][1]} |")
    L.append("")
    L.append(f"**lockbox**：`{rep['lockbox'][0]} ~ {rep['lockbox'][1]}`"
             f"（{'已使用' if rep['lockbox_used'] else '封存未使用'}；同 version 至多验证一次）")
    L.append("")
    L.append("## 说明与边界")
    L.append("")
    for n in rep["notes"]:
        L.append(f"- {n}")
    L.append("")
    return "\n".join(L)


def write_all(rep: dict, art_dir: Path) -> dict:
    art_dir.mkdir(parents=True, exist_ok=True)
    jp, mp = art_dir / "validation.json", art_dir / "validation.md"
    jp.write_text(json.dumps(rep, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    mp.write_text(render_md(rep), encoding="utf-8")
    return {"json": str(jp), "md": str(mp)}
