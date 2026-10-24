"""版本面：内容寻址对象库 · commit 链 · clone/push · Change Request（远端=云端替身）"""
from __future__ import annotations

import json
import time
from pathlib import Path

from .ctx import Ctx, sha256_file, sha256_obj


def _repo_files(repo: Path) -> dict[str, str]:
    return {str(p.relative_to(repo)): sha256_file(p)
            for p in sorted(repo.rglob("*.json")) if p.is_file()}


def _store_blob(ctx: Ctx, content: bytes) -> str:
    h = __import__("hashlib").sha256(content).hexdigest()
    d = ctx.objects_dir / h[:2]
    d.mkdir(parents=True, exist_ok=True)
    f = d / h
    if not f.exists():
        f.write_bytes(content)
    return h


def make_commit(ctx: Ctx, repo: Path, msg: str) -> dict:
    """对 repo 当前全部 JSON 文件做快照 commit（tree=路径→哈希）"""
    tree = {}
    for rel, _ in sorted(_repo_files(repo).items()):
        blob_h = _store_blob(ctx, (repo / rel).read_bytes())
        tree[rel] = blob_h
    refs = json.loads(ctx.refs_file.read_text()) if ctx.refs_file.exists() else {}
    parent = refs.get("main")
    commit = {"commit": None, "parent": parent, "tree": tree, "msg": msg,
              "ts": time.strftime("%Y-%m-%dT%H:%M:%S"), "code_version": ctx.code_version}
    commit["commit"] = sha256_obj(commit)
    _store_blob(ctx, json.dumps(commit, ensure_ascii=False).encode())
    refs["main"] = commit["commit"]
    refs.setdefault("log", []).append({"commit": commit["commit"], "msg": msg, "ts": commit["ts"]})
    ctx.refs_file.parent.mkdir(parents=True, exist_ok=True)
    ctx.refs_file.write_text(json.dumps(refs, ensure_ascii=False, indent=2), encoding="utf-8")
    return commit


def repo_clone(args: dict, ctx: Ctx) -> dict:
    """远端 main → sandbox/repo（α：本地目录拷贝；生产：HTTPS+PAT 增量）"""
    from .seed import seed_remote
    seeded = seed_remote(ctx.repo_dir)
    ctx.ensure_sandbox()
    import shutil
    shutil.rmtree(ctx.local_repo, ignore_errors=True)
    shutil.copytree(ctx.repo_dir, ctx.local_repo)
    head = None
    if ctx.refs_file.exists():
        head = json.loads(ctx.refs_file.read_text()).get("main")
    if head is None:  # 首次：为远端 main 建基线 commit
        c = make_commit(ctx, ctx.repo_dir, "init: cloud main baseline")
        head = c["commit"]
    return {"cloned": True, "remote_seeded": seeded, "head": head[:12],
            "files": len(_repo_files(ctx.local_repo))}


def repo_status(args: dict, ctx: Ctx) -> dict:
    local = _repo_files(ctx.local_repo) if ctx.local_repo.exists() else {}
    remote = _repo_files(ctx.repo_dir)
    changed = [k for k in local if k in remote and local[k] != remote[k]]
    added = [k for k in local if k not in remote]
    return {"head": (json.loads(ctx.refs_file.read_text()).get("main", "")[:12]
            if ctx.refs_file.exists() else None),
            "dirty": bool(changed or added), "changed": changed, "added": added}


def _factor_gate(ctx: Ctx, changed: list[str]) -> str:
    """§15.2 CR 审查 · 因子门禁证据（T-R6 v2 语义）：本地 registry 相对远端的条目 diff。

    比对 metric 条目（新增/删除/状态翻转/参数·归属·版本变化）与引擎级配置
    （权重表/锚点/regime/defaults/信号）。**新增指标或 status 翻转**须带
    ``hypothesis_id``（无来源不放行）；纯 orphan 标记（T-R6 同步对账产物）与
    updated_at 变化豁免；参数调优计入 touched 但不强制来源（评审项，非来源项）。
    远端可为 v1 旧格式（按 factors[].id 对照）。保持既有错误码与消息风格。
    """
    if "factors/registry.json" not in changed:
        return "n/a（未改动因子注册表）"
    try:
        local = json.loads((ctx.local_repo / "factors" / "registry.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return "n/a（因子注册表不可读）"
    rp = ctx.repo_dir / "factors" / "registry.json"
    remote = json.loads(rp.read_text(encoding="utf-8")) if rp.exists() else {}

    def _index(reg: dict) -> dict:                       # v2 engines.*.metrics ⊕ v1 factors[]
        out = {}
        for sec in (reg.get("engines") or {}).values():
            for m in (sec or {}).get("metrics") or []:
                if isinstance(m, dict) and m.get("id"):
                    out[m["id"]] = m
        for f in reg.get("factors") or []:
            if isinstance(f, dict) and f.get("id"):
                out.setdefault(f["id"], f)
        return out

    rem, loc = _index(remote), _index(local)
    touched, unbound = [], []
    for mid, m in loc.items():
        old = rem.get(mid)
        if m.get("orphan"):
            continue                                     # 纯 orphan 标记豁免（同步对账产物）
        if old is None:                                  # 新增 → 须带来源
            touched.append(f"{mid}(new→{m.get('status', 'active')})")
            if not str(m.get("hypothesis_id") or "").strip():
                unbound.append(mid)
            continue
        ost, nst = old.get("status", "active"), m.get("status", "active")
        if ost != nst:                                   # 状态翻转 → 须带来源
            touched.append(f"{mid}({ost}→{nst})")
            if not str(m.get("hypothesis_id") or "").strip():
                unbound.append(mid)
        elif any(m.get(k) != old.get(k) for k in ("params", "scorer", "dim", "version")):
            touched.append(f"{mid}(参数/归属变更)")       # 参数调优：评审项，不强制来源
    for mid in rem.keys() - loc.keys():
        touched.append(f"{mid}(removed)")                # 删除：报告放行（不删数据由 orphan 语义保障）
    for eid in ("alpha", "beta", "macro"):               # 引擎级配置：报告不设来源门禁
        lo = (local.get("engines") or {}).get(eid) or {}
        ro = (remote.get("engines") or {}).get(eid) or {}
        for key in ("dim_weights", "anchors", "defaults", "regime"):
            if (lo.get(key) or ro.get(key)) and lo.get(key) != ro.get(key):
                touched.append(f"{eid}.{key}(变更)")
        if (lo.get("signals") or ro.get("signals")) and lo.get("signals") != ro.get("signals"):
            touched.append(f"{eid}.signals(变更)")
    if not touched:
        return "pass（因子注册表无实质变更）"
    if unbound:
        return f"FAIL（新增/激活因子无 hypothesis_id：{','.join(unbound)}）"
    return f"pass（因子变更 {len(touched)}：{'；'.join(touched)}）"


def repo_push(args: dict, ctx: Ctx, run_manifest: dict | None = None) -> dict:
    """本地变更 → 远端 CR（Change Request）：§15.2 验证门禁（全绿才允许 push）→ pending_human"""
    st = repo_status({}, ctx)
    if not st["dirty"]:
        return {"pushed": False, "reason": "无变更（clean）"}

    # 运行证据：未显式携带时取最近 checkpoint（CLI 与 MCP 共用同一门禁）
    if run_manifest is None:
        from .tools import (
            checkpoint_list,  # 延迟导入（tools 顶层已 import repo，避免环）
        )
        cps = checkpoint_list({}, ctx)["items"]
        if cps:
            last = cps[-1]
            run_manifest = {"run_id": last["run_id"], "status": last.get("status"),
                            "verify": last.get("verify", {})}

    # ── 验证门禁（P0-1：任一红 → 禁止 push，仅存本地；不再虚构 pass）──
    if not run_manifest:
        return {"pushed": False, "blocked": True,
                "reason": "禁止 push：无运行证据（先 ap run <task.yaml> 产生全绿 checkpoint）"}
    if run_manifest.get("status") != "succeeded":
        return {"pushed": False, "blocked": True,
                "reason": f"禁止 push：最近运行 status={run_manifest.get('status')!r}，须 succeeded（六项验证全绿）"}
    verify = run_manifest.get("verify") or {}
    failed = {k: v for k, v in verify.items() if not str(v).startswith("pass")}
    if not verify or failed:
        return {"pushed": False, "blocked": True,
                "reason": f"禁止 push：验证未全绿 {failed if failed else '（verify 为空）'}"}

    changed = st["changed"] + st["added"]
    # CR 审查证据 = 本次运行的真实验证结果（生产为纪律官 pipeline 全量重跑）
    review = dict(verify)
    review["run_evidence"] = run_manifest["run_id"]
    review["backtest_compare"] = "n/a（无策略绩效变更）"
    review["factor_gate"] = _factor_gate(ctx, changed)

    crs = json.loads(ctx.crs_file.read_text()) if ctx.crs_file.exists() else {"next": 1, "items": []}
    cr = {"cr_id": f"CR-{crs['next']:04d}", "msg": args.get("m", "update"), "files": changed,
          "review": review, "status": "pending_human",
          "from_commit": st["head"], "ts": time.strftime("%Y-%m-%dT%H:%M:%S")}
    crs["next"] += 1
    crs["items"].append(cr)
    ctx.crs_file.parent.mkdir(parents=True, exist_ok=True)
    ctx.crs_file.write_text(json.dumps(crs, ensure_ascii=False, indent=2), encoding="utf-8")
    return {"pushed": True, "cr": cr["cr_id"], "status": cr["status"], "files": changed, "review": review}


def cr_approve(args: dict, ctx: Ctx) -> dict:
    """模拟人工确认 merge（生产：Web PR 队列点确认）"""
    crs = json.loads(ctx.crs_file.read_text())
    cid = args.get("cr")
    for it in crs["items"]:
        if it["cr_id"] == cid and it["status"] == "pending_human":
            # 防御性复查（P0-1）：CR 审查含未通过项 → 禁止 merge
            bad = {k: v for k, v in it.get("review", {}).items()
                   if k != "run_evidence" and not str(v).startswith(("pass", "n/a"))}
            if bad:
                return {"merged": False, "cr": cid, "reason": f"CR 审查未全绿，禁止 merge：{bad}"}
            # merge：本地变更文件 → 远端 repo，再打 commit
            import shutil
            for f in it["files"]:
                src, dst = ctx.local_repo / f, ctx.repo_dir / f
                dst.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(src, dst)
            c = make_commit(ctx, ctx.repo_dir, f"{cid}: {it['msg']}")
            it["status"] = "merged"
            it["merged_commit"] = c["commit"][:12]
            ctx.crs_file.write_text(json.dumps(crs, ensure_ascii=False, indent=2), encoding="utf-8")
            return {"merged": True, "cr": cid, "commit": c["commit"][:12]}
    return {"merged": False, "reason": "CR 不存在或状态不可合并"}
