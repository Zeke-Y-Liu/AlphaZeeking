"""registry 写保护三语义（组件化 T-R4）：覆盖→merge / 用户改动优先 / orphan 标记。

根治 fetch_all 覆盖式写库（T-R1 盘点 L781 rmtree + _reg 直接覆盖写）：
- 快照-恢复守护：rmtree 前 ``snapshot_guard`` 抢救用户登记面与历史 K 线，
  拉取完成后 ``restore_guard`` 合并回写——fetch_all 主体零改动（架构决策：
  守护层加契约而非重写数据流）。
- 三语义：
  1. **覆盖→merge**：``merge_registry`` / ``merge_portfolio``（条目级字段合并）、
     ``merge_bars``（K 线 union：同 dt 新值覆盖=点位修正，旧 dt 保留=历史不丢）。
  2. **用户改动优先**：old 条目字段与机器基线（本次将写出的 new）不一致 →
     视为用户改动，保 old（冲突时用户胜出，红线 2）；机器维护字段除外
     （MACHINE_FIELDS：version/universe/type/horizon_days）。
  3. **orphan 标记不物理删**：上游消失的条目 → ``orphan: true`` +
     ``orphaned_at``（红线 3，可查询可审计；M5 T-L3 旧版本 orphan 语义承接此机制）。

诚实边界：用户改动与上游真实变更不可区分时保守保旧（不静默覆盖人工字段），
``_merge`` 信息块逐项登记本次合并动作，供审计与人工复核。
"""
from __future__ import annotations

import datetime as _dt
import json
from pathlib import Path

__all__ = ["GUARDED_RELS", "snapshot_guard", "restore_guard",
           "merge_registry", "merge_portfolio", "merge_bars"]

# 机器维护字段（新值胜出）；其余字段用户改动优先
MACHINE_FIELDS = frozenset({"version", "universe", "type", "horizon_days"})

# fetch_all 整体重建会清除、需要快照-恢复守护的文件：
# - 配置面（用户登记）：registry / portfolio 组合定义
# - 数据面（历史 K 线，增量窗重拉时防丢历史）：bars / bars_raw / cross_check_tx
#   （财务/分红/日历/市值/宏观为上游全量重拉，覆盖=修正，不入守护面）
GUARDED_RELS = ("factors/registry.json",
                "portfolio/cn4.json", "portfolio/multi5.json",
                "market/daily_bars.json", "market/daily_bars_raw.json",
                "market/cross_check_tx.json")


def _today() -> str:
    return _dt.date.today().isoformat()


def _read_json(p: Path):
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:                       # noqa: BLE001 - 损坏文件按缺失处理
        return None


def snapshot_guard(repo: Path) -> dict:
    """rmtree 前抢救：读全部守护面文件 → {rel: content}（内存快照）。"""
    snap = {}
    for rel in GUARDED_RELS:
        p = repo / rel
        if p.exists():
            c = _read_json(p)
            if c is not None:
                snap[rel] = c
    return snap


# ── 语义 1+2+3：registry / portfolio 条目级合并 ───────────────────────────
def _merge_items(old_items: list, new_items: list, *, key: str) -> tuple[list, dict]:
    """条目级合并：字段级用户优先（非机器字段）+ 新增 + 消失→orphan 标记。"""
    old_by = {it[key]: it for it in old_items if isinstance(it, dict) and it.get(key)}
    new_by = {it[key]: it for it in new_items if isinstance(it, dict) and it.get(key)}
    merged, info = [], {"orphaned": [], "user_fields_kept": {}}
    for iid, nit in new_by.items():
        oit = old_by.get(iid)
        if oit is None:
            merged.append(dict(nit))
            continue
        m = dict(nit)
        for k, ov in oit.items():
            if k not in m:                              # 用户自定义字段保留
                m[k] = ov
                info["user_fields_kept"].setdefault(iid, []).append(k)
            elif ov != m[k] and k not in MACHINE_FIELDS:  # 冲突 → 用户改动胜出
                m[k] = ov
                info["user_fields_kept"].setdefault(iid, []).append(k)
        merged.append(m)
    for iid, oit in old_by.items():
        if iid not in new_by:                           # 语义 3：orphan 不物理删
            merged.append({**oit, "orphan": True, "orphaned_at": _today()})
            info["orphaned"].append(iid)
    info["orphaned"].sort()
    return merged, info


def merge_registry(old: dict, new: dict) -> dict:
    """factors/registry.json 合并：factors 按 id 字段级合并（机器字段新胜）。"""
    items, info = _merge_items(old.get("factors", []), new.get("factors", []), key="id")
    return {**new, "factors": items, "_merge": info}


def merge_portfolio(old: dict, new: dict) -> dict:
    """portfolio/*.json 合并：顶层标量 + positions 按 secid 字段级合并。"""
    out, info = {}, {"orphaned": [], "user_fields_kept": {}}
    for k, nv in new.items():
        ov = old.get(k)
        if k == "positions" or not isinstance(nv, (str, int, float, bool, type(None))):
            continue
        if ov is not None and ov != nv:                 # 冲突 → 用户改动胜出
            out[k] = ov
            info["user_fields_kept"].setdefault("$top", []).append(k)
    op, np_ = old.get("positions") or [], new.get("positions") or []
    pos, pinfo = _merge_items(op, np_, key="secid")
    out["positions"] = pos
    info["orphaned"] += pinfo["orphaned"]
    for iid, ks in pinfo["user_fields_kept"].items():
        info["user_fields_kept"].setdefault(iid, []).extend(ks)
    return {**out, **{k: v for k, v in new.items()
                      if k not in out and k != "positions"},
            "_merge": info}


# ── 语义 1：K 线 union（历史不丢）─────────────────────────────────────────
def merge_bars(old: dict, new: dict) -> tuple[dict, dict]:
    """daily_bars 合并：secid 级 union by dt；同 dt 新覆盖旧；old 独有 secid 整段保留。

    增量窗首行 pct 接链：new 首日若可衔接 old 前一交易日 close → 重算该行 pct
    （qfq 同源口径），并在 info.resume 登记不静默。
    """
    out, info = {}, {"resume": [], "kept_secids": []}
    for s, rows in new.items():
        oldmap = {r["dt"]: r for r in (old.get(s) or []) if r.get("dt")}
        seq = {r["dt"]: r for r in rows if r.get("dt")}
        if rows and oldmap:
            first_dt = rows[0]["dt"]
            prev_dts = sorted(d for d in oldmap if d < first_dt)
            if prev_dts and rows[0].get("pct") is None:
                pc = oldmap[prev_dts[-1]].get("close")
                c = rows[0].get("close")
                if pc and c:
                    rows[0]["pct"] = round(c / pc - 1, 6)   # 接链重算（防跨窗断链）
                    info["resume"].append(s)
        seq.update({d: r for d, r in oldmap.items() if d not in seq})   # 只补缺失 dt（同 dt 新胜）
        out[s] = [seq[d] for d in sorted(seq)]
    for s, rows in old.items():                         # 本次未拉的 secid → 旧数据保留
        if s not in out and rows:
            out[s] = rows
            info["kept_secids"].append(s)
    info["kept_secids"].sort()
    return out, info


# ── 恢复回写 ─────────────────────────────────────────────────────────────
def restore_guard(repo: Path, snap: dict) -> dict:
    """fetch_all 写库完成后调用：守护面逐文件合并回写 → 返回合并信息（审计）。"""
    summary: dict[str, dict] = {}
    for rel, old in snap.items():
        p = repo / rel
        new = _read_json(p)
        if new is None or not p.exists():
            continue                                    # 本次未产出该文件 → 旧内容保持
        if rel == "factors/registry.json":
            merged = merge_registry(old, new)
        elif rel.startswith("portfolio/"):
            merged = merge_portfolio(old, new)
        else:
            merged, binfo = merge_bars(old, new)
            summary[rel] = {**binfo, "dt_total": sum(len(v) for v in merged.values())}
            p.write_text(json.dumps(merged, ensure_ascii=False), encoding="utf-8")
            continue
        info = merged.pop("_merge", {})
        summary[rel] = info
        p.write_text(json.dumps(merged, ensure_ascii=False, indent=2), encoding="utf-8")
    return summary
