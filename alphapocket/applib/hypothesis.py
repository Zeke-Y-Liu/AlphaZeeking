"""R3 · 研报→假设登记通道（《研究缺口R1-R4实施计划》§5）

登记表 ``repo/hypothesis/registry.json``（与 ``factors/registry.json`` 同级，入 Git、走同一 CR 链）：
外部研究（书/研报/论文/文档）→ 可检验命题（``statement``）→ 预期方向 → 因子血缘（``linked_factors``）。

状态机（§5.1）：``proposed → testing → supported / refuted → retired``；因子 ``invalid`` 时联动复查。

联动（§5.2）
-----------
- 因子→假设：[registry.json](file:///workspace/alphapocket/remote/repo/factors/registry.json) 因子条目 ``hypothesis_id``；
  **draft→active 门禁强制非空**（无来源不激活，CLI 拒绝，无旁路）。
- 假设→因子：``linked_factors`` 反向可查（``hypothesis.list --factor <id>`` 一键点回来源）。
- 巡检（M5 8.2）：``weakening/invalid`` 因子自动带出假设生成"假设仍成立吗"待答项；healthy 因子按 20% 抽查。
"""
from __future__ import annotations

import json
import time
from pathlib import Path

HYP_REL = Path("hypothesis") / "registry.json"      # 相对 repo 根

# 状态机：合法迁移（register 校验状态合法性；transition 校验迁移合法性）
STATUS_FLOW: dict[str, set[str]] = {
    "proposed": {"testing", "retired"},
    "testing": {"supported", "refuted", "retired"},
    "supported": {"retired"},
    "refuted": {"retired"},
    "retired": set(),
}
DIRECTIONS = ("positive", "negative", "neutral")     # 预期方向
SOURCE_TYPES = ("book", "report", "paper", "doc", "note")
HEALTHY_SAMPLE_EVERY = 5                             # healthy 因子 20% 抽查（每 5 取 1，确定性）
MANDATORY_STATUS = ("weakening", "invalid")          # 触发必答复查的因子状态


def _today() -> str:
    return time.strftime("%Y-%m-%d")


# ── 登记表读写 ─────────────────────────────────────────
def load(repo_dir: Path) -> dict:
    """读取假设登记表；不存在 → 空表（不报错，便于首次 register）。"""
    p = Path(repo_dir) / HYP_REL
    if not p.exists():
        return {"hypotheses": []}
    return json.loads(p.read_text(encoding="utf-8"))


def save(repo_dir: Path, reg: dict) -> Path:
    """写回登记表（按 id 排序，保证 diff 稳定）。"""
    p = Path(repo_dir) / HYP_REL
    p.parent.mkdir(parents=True, exist_ok=True)
    reg["hypotheses"] = sorted(reg.get("hypotheses", []), key=lambda h: h["id"])
    p.write_text(json.dumps(reg, ensure_ascii=False, indent=2), encoding="utf-8")
    return p


def next_id(reg: dict) -> str:
    n = 0
    for h in reg.get("hypotheses", []):
        try:
            n = max(n, int(str(h["id"]).split("-")[-1]))
        except (KeyError, ValueError):
            continue
    return f"H-{n + 1:04d}"


def find(reg: dict, hid: str) -> dict | None:
    return next((h for h in reg.get("hypotheses", []) if h.get("id") == hid), None)


def factor_ref(factor_reg: dict, factor_id: str) -> dict | None:
    return next((f for f in factor_reg.get("factors", []) if f.get("id") == factor_id), None)


# ── 登记（§5.1 模板最小字段）───────────────────────────
def validate(payload: dict) -> dict:
    """最小字段校验：可检验命题 + 预期方向 + 来源（宁缺毋滥）。"""
    errs = []
    if not str(payload.get("title") or "").strip():
        errs.append("title 必填")
    if not str(payload.get("statement") or "").strip():
        errs.append("statement 必填（可检验命题）")
    src = payload.get("source") or {}
    if not str(src.get("type") or "").strip():
        errs.append("source.type 必填")
    elif src["type"] not in SOURCE_TYPES:
        errs.append(f"source.type 须 ∈ {SOURCE_TYPES}")
    if not str(src.get("ref") or "").strip():
        errs.append("source.ref 必填（来源可追溯）")
    if payload.get("expected_direction") not in DIRECTIONS:
        errs.append(f"expected_direction 须 ∈ {DIRECTIONS}")
    return {"ok": not errs, "errors": errs}


def register(reg: dict, payload: dict) -> dict:
    """新增假设（纯函数）：校验最小字段 + 分配 id；不落盘。"""
    v = validate(payload)
    if not v["ok"]:
        return {"error": "；".join(v["errors"])}
    hid = payload.get("id") or next_id(reg)
    if find(reg, hid) is not None:
        return {"error": f"假设已存在：{hid}"}
    status = payload.get("status") or "proposed"
    if status not in STATUS_FLOW:
        return {"error": f"非法状态：{status}（须 ∈ {tuple(STATUS_FLOW)}）"}
    today = _today()
    item = {
        "id": hid,
        "title": payload["title"].strip(),
        "source": {"type": payload["source"]["type"], "ref": payload["source"]["ref"].strip(),
                   "url": payload["source"].get("url"),
                   "quoted_at": payload["source"].get("quoted_at") or today},
        "statement": payload["statement"].strip(),
        "expected_direction": payload["expected_direction"],
        "universe": payload.get("universe") or ["CN"],
        "status": status,
        "linked_factors": sorted(payload.get("linked_factors") or []),
        "evidence_runs": payload.get("evidence_runs") or [],
        "created_at": today, "updated_at": today,
    }
    reg.setdefault("hypotheses", []).append(item)
    return {"registered": True, "hypothesis": item}


# ── 查询：血缘双向（§5.2）──────────────────────────────
def listing(reg: dict, factor_reg: dict | None = None,
            factor: str | None = None, status: str | None = None) -> dict:
    """列表查询；``factor`` 给定时走反向血缘（该因子归属的假设），并回带因子的 hypothesis_id。"""
    items = reg.get("hypotheses", [])
    if factor:
        items = [h for h in items if factor in (h.get("linked_factors") or [])]
    if status:
        items = [h for h in items if h.get("status") == status]
    out = {"n": len(items), "hypotheses": items}
    if factor and factor_reg is not None:
        f = factor_ref(factor_reg, factor)
        out["factor"] = {"id": factor, "hypothesis_id": (f or {}).get("hypothesis_id"),
                         "status": (f or {}).get("status")}
    return out


def link(reg: dict, factor_reg: dict, hypothesis_id: str, factor_id: str) -> dict:
    """建立因子↔假设血缘（纯函数）：双向写入，冲突拒绝（不设旁路）。"""
    h = find(reg, hypothesis_id)
    if h is None:
        return {"error": f"假设不存在：{hypothesis_id}"}
    f = factor_ref(factor_reg, factor_id)
    if f is None:
        return {"error": f"因子未注册：{factor_id}"}
    bound = f.get("hypothesis_id")
    if bound and bound != hypothesis_id:
        return {"error": f"因子 {factor_id} 已绑定 {bound}，须先解绑（不设旁路）"}
    f["hypothesis_id"] = hypothesis_id
    lf = h.setdefault("linked_factors", [])
    if factor_id not in lf:
        h["linked_factors"] = sorted([*lf, factor_id])
    h["updated_at"] = _today()
    return {"linked": True, "hypothesis_id": hypothesis_id, "factor_id": factor_id}


def transition(reg: dict, hypothesis_id: str, to_status: str) -> dict:
    """状态机迁移（§5.1）：仅允许 STATUS_FLOW 中的合法迁移。"""
    h = find(reg, hypothesis_id)
    if h is None:
        return {"error": f"假设不存在：{hypothesis_id}"}
    src = h.get("status")
    if to_status not in STATUS_FLOW:
        return {"error": f"非法状态：{to_status}（须 ∈ {tuple(STATUS_FLOW)}）"}
    if to_status not in STATUS_FLOW.get(src, set()):
        return {"error": f"非法迁移：{src} → {to_status}"}
    h["status"] = to_status
    h["updated_at"] = _today()
    return {"transitioned": True, "hypothesis_id": hypothesis_id, "from": src, "to": to_status}


# ── 激活门禁（§5.2 / M5 8.1）───────────────────────────
def activation_gate(factor_reg: dict, factor_id: str) -> dict:
    """draft→active 门禁：新因子须带 hypothesis_id（可点回来源），否则拒绝激活。"""
    f = factor_ref(factor_reg, factor_id)
    if f is None:
        return {"ok": False, "reason": f"因子未注册：{factor_id}"}
    if f.get("status") != "draft":
        return {"ok": False, "reason": f"因子 {factor_id} 当前 status={f.get('status')!r}，仅 draft 可激活"}
    if not str(f.get("hypothesis_id") or "").strip():
        return {"ok": False, "factor_id": factor_id,
                "reason": f"拒绝激活：因子 {factor_id} 无 hypothesis_id"
                          "（M5 8.1 门禁：新因子须可点回假设来源，无旁路）"}
    return {"ok": True, "factor_id": factor_id, "hypothesis_id": f["hypothesis_id"],
            "reason": "门禁通过（假设来源已登记）"}


def activate(factor_reg: dict, reg: dict, factor_id: str) -> dict:
    """执行激活（纯函数）：门禁通过 → status=active；并校验假设存在。"""
    g = activation_gate(factor_reg, factor_id)
    if not g["ok"]:
        return {"activated": False, **g}
    if find(reg, g["hypothesis_id"]) is None:
        return {"activated": False, "ok": False, "factor_id": factor_id,
                "reason": f"拒绝激活：hypothesis_id {g['hypothesis_id']} 未登记（血缘悬空）"}
    f = factor_ref(factor_reg, factor_id)
    f["status"] = "active"
    return {"activated": True, "ok": True, "factor_id": factor_id,
            "hypothesis_id": g["hypothesis_id"], "reason": g["reason"]}


# ── 巡检：假设复查（§5.2 / M5 8.2）─────────────────────
def _review_item(kind: str, factor: dict, hyp: dict | None) -> dict:
    return {
        "kind": kind,                                     # mandatory | sample
        "factor_id": factor.get("id"),
        "factor_name": factor.get("name"),
        "factor_status": factor.get("status"),
        "hypothesis_id": (hyp or {}).get("id") or factor.get("hypothesis_id"),
        "hypothesis_title": (hyp or {}).get("title"),
        "hypothesis_status": (hyp or {}).get("status"),
        "question": "假设仍成立吗？",
        "action": ("必答：因子已 weakening/invalid，复核假设并决定升版本/退役"
                   if kind == "mandatory" else "抽查：healthy 因子按 20% 复核假设来源"),
    }


def review_items(factor_reg: dict, reg: dict) -> dict:
    """生成巡检待答项：weakening/invalid 必答 + healthy 20% 抽查（确定性，便于复现）。"""
    hyps = {h.get("id"): h for h in reg.get("hypotheses", [])}
    back = {}
    for h in reg.get("hypotheses", []):
        for fid in h.get("linked_factors") or []:
            back.setdefault(fid, h)

    def hyp_of(f):
        return hyps.get(f.get("hypothesis_id")) or back.get(f.get("id"))

    mandatory, actives = [], []
    for f in factor_reg.get("factors", []):
        if f.get("status") in MANDATORY_STATUS:
            mandatory.append(_review_item("mandatory", f, hyp_of(f)))
        elif f.get("status") == "active":
            actives.append(f)
    samples = [_review_item("sample", f, hyp_of(f))
               for i, f in enumerate(sorted(actives, key=lambda x: str(x.get("id"))))
               if i % HEALTHY_SAMPLE_EVERY == 0]
    return {"generated": _today(), "n_mandatory": len(mandatory), "n_sample": len(samples),
            "n_active": len(actives), "mandatory": mandatory, "sample": samples}


def render_review_md(res: dict) -> str:
    L = ["# 巡检 · 假设复查（M5 8.2 / R3 §5.2）", "",
         f"生成 `{res['generated']}` · active 因子 {res['n_active']} · "
         + f"必答复查 {res['n_mandatory']} · 抽查 {res['n_sample']}", ""]
    L.append("## 必答复查（weakening / invalid 因子）")
    L.append("")
    if not res["mandatory"]:
        L.append("（无 weakening/invalid 因子，无需必答复查）")
    else:
        L.append("| 因子 | 状态 | 假设 | 待答项 |")
        L.append("| --- | --- | --- | --- |")
        for it in res["mandatory"]:
            L.append(f"| {it['factor_id']}（{it['factor_name']}） | {it['factor_status']} "
                     f"| {it['hypothesis_id']} {it['hypothesis_title'] or '—'} | {it['question']} |")
    L.append("")
    L.append("## 抽查（healthy 因子每 5 取 1 ≈ 20%）")
    L.append("")
    if not res["sample"]:
        L.append("（无 active 因子）")
    else:
        L.append("| 因子 | 状态 | 假设 | 待答项 |")
        L.append("| --- | --- | --- | --- |")
        for it in res["sample"]:
            L.append(f"| {it['factor_id']}（{it['factor_name']}） | {it['factor_status']} "
                     f"| {it['hypothesis_id'] or '—'} {it['hypothesis_title'] or '—'} | {it['question']} |")
    L.append("")
    return "\n".join(L)


def write_review(res: dict, art_dir: Path) -> dict:
    art_dir.mkdir(parents=True, exist_ok=True)
    jp, mp = art_dir / "hypothesis_review.json", art_dir / "hypothesis_review.md"
    jp.write_text(json.dumps(res, ensure_ascii=False, indent=2), encoding="utf-8")
    mp.write_text(render_review_md(res), encoding="utf-8")
    return {"json": str(jp), "md": str(mp)}


# ── 工具入口（tools.py 注册）───────────────────────────
def hypothesis_register(args, ctx):
    """R3：登记研报→假设（来源/可检验命题/预期方向）→ repo/hypothesis/registry.json"""
    reg = load(ctx.local_repo)
    res = register(reg, args)
    if "error" in res:
        return res
    save(ctx.local_repo, reg)
    return {"registered": res["registered"], "hypothesis": res["hypothesis"],
            "path": str(ctx.local_repo / HYP_REL)}


def hypothesis_list(args, ctx):
    """R3：假设列表；--factor 走反向血缘（一键点回因子来源）"""
    reg = load(ctx.local_repo)
    factor_reg = json.loads((ctx.local_repo / "factors" / "registry.json").read_text(encoding="utf-8"))
    return listing(reg, factor_reg, factor=args.get("factor"), status=args.get("status"))


def hypothesis_link(args, ctx):
    """R3：建立因子↔假设血缘（双向写入，冲突拒绝）"""
    reg = load(ctx.local_repo)
    fp = ctx.local_repo / "factors" / "registry.json"
    factor_reg = json.loads(fp.read_text(encoding="utf-8"))
    res = link(reg, factor_reg, str(args.get("hypothesis")), str(args.get("factor")))
    if "error" in res:
        return res
    save(ctx.local_repo, reg)
    fp.write_text(json.dumps(factor_reg, ensure_ascii=False, indent=2), encoding="utf-8")
    return res


def hypothesis_set_status(args, ctx):
    """R3：假设状态机迁移（proposed→testing→supported/refuted→retired）"""
    reg = load(ctx.local_repo)
    res = transition(reg, str(args.get("hypothesis")), str(args.get("status")))
    if "error" in res:
        return res
    save(ctx.local_repo, reg)
    return res


def hypothesis_review(args, ctx):
    """R3：巡检假设复查（weakening/invalid 必答 + healthy 20% 抽查）→ artifacts/hp_review_*"""
    from .ctx import new_run_id
    reg = load(ctx.local_repo)
    factor_reg = json.loads((ctx.local_repo / "factors" / "registry.json").read_text(encoding="utf-8"))
    res = review_items(factor_reg, reg)
    art = ctx.artifacts_dir / new_run_id("hp_review")
    res["artifacts"] = write_review(res, art)
    return res


def factor_activate(args, ctx):
    """R3/M5 8.1：draft→active 激活门禁（无 hypothesis_id 拒绝，无旁路）"""
    fp = ctx.local_repo / "factors" / "registry.json"
    factor_reg = json.loads(fp.read_text(encoding="utf-8"))
    reg = load(ctx.local_repo)
    res = activate(factor_reg, reg, str(args.get("factor")))
    if not res.get("activated"):
        return res
    fp.write_text(json.dumps(factor_reg, ensure_ascii=False, indent=2), encoding="utf-8")
    return res
