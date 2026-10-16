"""ap CLI —— 命令分发（V4-α 骨架：auth/clone/run/schedule/push/checkpoint/serve/status）"""
from __future__ import annotations
import argparse, json, sys, time
from pathlib import Path
from .ctx import Ctx
from . import repo as R
from .hypothesis import DIRECTIONS, SOURCE_TYPES
from .tools import TOOLS, call_tool, checkpoint_list, checkpoint_restore


def cmd_auth(args):
    ctx = Ctx(); ctx.ensure_sandbox()
    print("Device Code Flow（模拟）：云端出码 4F2K-9QXZ → 请在手机 H5 确认 + TOTP")
    time.sleep(0.3)
    ctx.credentials.parent.mkdir(parents=True, exist_ok=True)
    ctx.credentials.write_text(json.dumps({
        "pat": "ap_pat_****-****-3f9a", "scopes": ["read:portfolio", "write:runs", "sync:repo"],
        "issued": time.strftime("%Y-%m-%dT%H:%M:%S"), "expire_days": 90}, ensure_ascii=False, indent=2), encoding="utf-8")
    print("✓ PAT 已颁发（scope: read:portfolio write:runs sync:repo · 90 天轮转）→", ctx.credentials)


def cmd_fetch(args):
    from .datasource import fetch_all
    if args.mode == "refresh" and not args.secid:
        sys.exit("✗ refresh 模式必须提供 secid（--secid 601058.SH 00700.HK …）")
    # T-F10 破坏性重写守卫：--secid 子集 + 缺省 mode → 自动升级 incremental
    # （合并语义，intent 显然；bootstrap/refresh 的 rmtree 重建会丢弃未列标的）
    mode = args.mode or ("incremental" if args.secid else None)
    ctx = Ctx()
    try:
        res = fetch_all(ctx, start=args.start, universe=args.secid, mode=mode)
    except ValueError as e:
        sys.exit(f"✗ {e}")
    print(json.dumps(res, ensure_ascii=False, indent=2))
    print("→ 下一步：ap clone && ap run tasks/review_cn4.yaml")


def cmd_clone(args):
    ctx = Ctx()
    res = R.repo_clone({}, ctx)
    print(json.dumps(res, ensure_ascii=False, indent=2))


def cmd_run(args):
    from .runner import run_task
    ctx = Ctx()
    tp = Path(args.task)
    if not tp.exists():
        sys.exit(f"任务文件不存在：{tp}")
    res = run_task(tp, ctx, as_of=args.as_of)
    print(json.dumps({k: res[k] for k in ("run_id", "status", "checkpoint", "verify") if k in res},
                     ensure_ascii=False, indent=2))
    if "manifest" in res:
        print("artifacts:")
        for a in res["manifest"]["artifacts"]:
            print("  -", a)


def cmd_push(args):
    ctx = Ctx()
    res = R.repo_push({"m": args.message}, ctx)   # 运行证据与验证门禁由 repo_push 统一把关
    print(json.dumps(res, ensure_ascii=False, indent=2))
    if res.get("pushed"):
        print(f"→ CR 已创建（{res['cr']}），merge 命令：ap push --approve {res['cr']}")
    elif res.get("blocked"):
        sys.exit(f"✗ {res['reason']}")


def cmd_push_approve(args):
    ctx = Ctx()
    res = R.cr_approve({"cr": args.cr}, ctx)
    print(json.dumps(res, ensure_ascii=False, indent=2))


def cmd_checkpoint(args):
    ctx = Ctx()
    if args.action == "ls":
        for it in checkpoint_list({}, ctx)["items"]:
            ok = all(str(v).startswith("pass") for v in it.get("verify", {}).values())
            print(f"{it['cp_id']}  {it['run_id']}  {it['status']:<15} as_of={it.get('as_of')}  "
                  f"verify={'✓' if ok else '✗'}")
    elif args.action == "restore":
        print(json.dumps(checkpoint_restore({"cp": args.id}, ctx), ensure_ascii=False, indent=2))


def cmd_exec_plan(args):
    """由最新调仓单生成「可下单执行单」（待人工确认，脚本不自动下单）"""
    ctx = Ctx()
    res = call_tool("portfolio.exec_orders",
                    {"portfolio": args.portfolio, "capital": args.capital, "run_id": args.run}, ctx)
    if "error" in res:
        sys.exit(f"✗ {res['error']}")
    print(json.dumps({k: res[k] for k in ("exec_id", "run_id", "portfolio", "capital_cny", "status")},
                     ensure_ascii=False, indent=2))
    s, c, f = res["summary"], res["cash"], res["fees"]
    print(f"委托 {s['n_orders']} 笔（卖 {s['n_sell']} / 买 {s['n_buy']}）｜换手 ¥{s['turnover_cny']:,.0f}（{s['turnover_pct']}%）"
          f"｜卖出回款 ¥{c['sell_proceeds']:,.0f}｜成本 ¥{f['total_fee_cny']:.2f}")
    for o in res["orders"]:
        print(f"  - {o['direction']} {o['name']} {o['secid']}：{abs(o['order_shares']):,} 份 / {o['lots']} 手"
              f" ≈ ¥{o['order_amount']:,.0f}（现权重 {o['cur_weight']:.0f}% → 目标 {o['tgt_weight']:.0f}%）")
    print("artifacts:")
    for k, v in res["artifacts"].items():
        print(f"  - {k}: {v}")
    print("→ 待人工确认后下单；引擎永不直接动仓位。")


def cmd_backtest(args):
    """组合回测（等权/等风险加权·月度再平衡 vs 沪深300ETF 买入持有）→ HTML+MD+JSON"""
    ctx = Ctx()
    res = call_tool("backtest.run",
                    {"portfolio": args.portfolio, "start": args.start, "end": args.end,
                     "rf": args.rf, "weighting": args.weighting}, ctx)
    if "error" in res:
        sys.exit(f"✗ {res['error']}")
    sm = res["strategy_metrics"]
    bm = res["benchmark"]["metrics"] if res["benchmark"] else None

    def row(lab, k, f):
        return f"  {lab:<12} 组合 {f(sm[k])}   |  基准 {f(bm[k]) if bm else '—'}"

    print(json.dumps({k: res[k] for k in ("run_id", "portfolio", "start", "end",
                                          "n_days", "n_rebalances")}, ensure_ascii=False, indent=2))
    print(f"  区间 {res['start']} ~ {res['end']}（{res['n_days']} 交易日 · 再平衡 {res['n_rebalances']} 次）")
    print(row("累计收益", "total_return", lambda v: f"{v*100:6.1f}%"))
    print(row("年化收益", "cagr", lambda v: f"{v*100:6.1f}%"))
    print(row("年化波动", "ann_vol", lambda v: f"{v*100:6.1f}%"))
    print(row("最大回撤", "max_dd", lambda v: f"{v*100:6.1f}%"))
    print(row("夏普(rf=0)", "sharpe", lambda v: f"{v:6.2f}"))
    print(f"  超额（累计） {res['excess']['total']*100:+.1f}%  ·  再平衡成本拖累 {res['cost']['total']*100:.2f}%")
    print("artifacts:")
    for k, v in res["artifacts"].items():
        print(f"  - {k}: {v}")


def cmd_sensitivity(args):
    """R1 参数敏感性扫描：OAT 曲线 + 双参数热力图 + 尖峰 flag → HTML+MD+JSON"""
    ctx = Ctx()
    res = call_tool("research.sensitivity",
                    {"portfolio": args.portfolio, "start": args.start, "end": args.end}, ctx)
    if "error" in res:
        sys.exit(f"✗ {res['error']}")
    for r in res["results"]:
        b = r["baseline"]
        print(json.dumps({"run_id": r["run_id"], "portfolio": r["portfolio"],
                          "start": r["start"], "end": r["end"]}, ensure_ascii=False, indent=2))
        print(f"  基准绩效：CAGR {b['cagr']*100:6.1f}% · 夏普 {b['sharpe']:5.2f} · "
              f"最大回撤 {b['max_dd']*100:6.1f}% · 累计换手 {b['turnover']:.2f}")
        a, c = r["anchor_check"], r["beta_state_consistency"]
        print(f"  自检：锚点比对 {'✓' if a['ok'] else '✗'}（{a['detail']}）"
              f"｜引擎B状态一致性 {'✓' if c['ok'] else '✗'}（{c['samples']} 抽样）")
        print("  结论：")
        for k, v in r["oat_verdicts"].items():
            print(f"    - {k:<12} {v}")
        if r["flagged"]:
            print(f"  ⚠ 尖峰 {len(r['flagged'])} 处 → 触发评审："
                  + "；".join(f"{f['param']}={f['value']}({f['rel_change']*100:.0f}%)" for f in r["flagged"]))
        else:
            print("  ✓ 无尖峰（全部参数档间绩效差 < 30%）")
        print("artifacts:")
        for k, v in r["artifacts"].items():
            print(f"  - {k}: {v}")


def cmd_walkforward(args):
    """R2 walk-forward 样本外验证：扩展窗口滚动 + lockbox 封存 → wf_*/validation.json+md"""
    ctx = Ctx()
    res = call_tool("research.walkforward",
                    {"factor": args.factor, "start": args.start, "end": args.end,
                     "train_min_months": args.train_min_months, "valid_months": args.valid_months,
                     "step_months": args.step_months}, ctx)
    if "error" in res:
        sys.exit(f"✗ {res['error']}")
    print(json.dumps({"validated": res["validated"]}, ensure_ascii=False, indent=2))
    for r in res["results"]:
        if "error" in r:
            print(f"  ✗ {r['factor']}：{r['error']}")
            continue
        lb = r["lockbox"]
        print(f"  {r['factor']:<14} folds={r['n_folds']} lockbox={lb[0]}~{lb[1]} → {r['verdict']}")
        print(f"      artifacts: {r['artifacts']['md']}")


def cmd_audit_split(args):
    """R2 audit_split 四查：字段完备 / 取数越界 / lockbox 一次性 / 折叠无重叠"""
    ctx = Ctx()
    res = call_tool("research.audit_split", {}, ctx)
    print(json.dumps({k: res[k] for k in ("audit_split", "n_reports")}, ensure_ascii=False, indent=2))
    for k, c in res["checks"].items():
        print(f"  [{'✓' if c['ok'] else '✗'}] {k}: {c['detail']}")
    if res["audit_split"] != "pass":
        sys.exit(1)


def cmd_hypothesis(args):
    """R3 研报→假设登记通道：list / register / link / set-status / review"""
    ctx = Ctx()
    act = args.action
    if act == "list":
        res = call_tool("hypothesis.list", {"factor": args.factor, "status": args.status}, ctx)
        if "error" in res:
            sys.exit(f"✗ {res['error']}")
        print(json.dumps({"n": res["n"]}, ensure_ascii=False))
        for h in res["hypotheses"]:
            print(f"  {h['id']}  [{h['status']:<9}] {h['title']}")
            print(f"      来源：{h['source']['type']} · {h['source']['ref']}")
            print(f"      命题：{h['statement']}（预期 {h['expected_direction']}）")
            print(f"      因子：{', '.join(h.get('linked_factors') or []) or '—'}")
        if res.get("factor"):
            f = res["factor"]
            print(f"  ▶ 反向血缘：因子 {f['id']}（{f['status']}）→ {f['hypothesis_id'] or '无来源'}")
    elif act == "register":
        payload = {"title": args.title, "statement": args.statement,
                   "expected_direction": args.direction,
                   "source": {"type": args.source_type, "ref": args.source_ref, "url": args.url},
                   "universe": [args.universe] if args.universe else ["CN"],
                   "linked_factors": [x for x in (args.linked or "").split(",") if x]}
        res = call_tool("hypothesis.register", payload, ctx)
        if "error" in res:
            sys.exit(f"✗ {res['error']}")
        h = res["hypothesis"]
        print(f"✓ 已登记 {h['id']}：{h['title']}（{h['status']}）→ {res['path']}")
    elif act == "link":
        res = call_tool("hypothesis.link",
                        {"hypothesis": args.hypothesis, "factor": args.factor}, ctx)
        if res.get("error") or not res.get("linked"):
            sys.exit(f"✗ {res.get('error') or res.get('reason') or 'link 失败'}")
        print(f"✓ 血缘：{res['factor_id']} ↔ {res['hypothesis_id']}（双向写入）")
    elif act == "set-status":
        res = call_tool("hypothesis.set_status",
                        {"hypothesis": args.hypothesis, "status": args.status}, ctx)
        if res.get("error"):
            sys.exit(f"✗ {res['error']}")
        print(f"✓ {res['hypothesis_id']}：{res['from']} → {res['to']}")
    elif act == "review":
        res = call_tool("hypothesis.review", {}, ctx)
        print(json.dumps({k: res[k] for k in ("n_mandatory", "n_sample", "n_active")},
                         ensure_ascii=False, indent=2))
        for it in res["mandatory"]:
            print(f"  ⚠ 必答 {it['factor_id']}（{it['factor_status']}）→ {it['hypothesis_id']} {it['question']}")
        for it in res["sample"]:
            print(f"  · 抽查 {it['factor_id']} → {it['hypothesis_id']} {it['question']}")
        print("artifacts:")
        for k, v in res["artifacts"].items():
            print(f"  - {k}: {v}")


def cmd_activate(args):
    """R3/M5 8.1 draft→active 激活门禁（无 hypothesis_id 拒绝，无旁路）"""
    ctx = Ctx()
    res = call_tool("factor.activate", {"factor": args.factor}, ctx)
    if not res.get("activated"):
        sys.exit(f"✗ {res.get('reason', '激活失败')}")
    print(f"✓ 因子 {res['factor_id']} 已激活（来源 {res['hypothesis_id']}）：{res['reason']}")


def cmd_fl(args):
    """M5 8.x 中长期因子库生命周期：activate/refs/inspect（T-L2~L4）→ pool/review（T-L5）"""
    if args.action == "activate":
        if not args.factor:
            sys.exit("✗ 用法：ap fl activate <factor_id>")
        ctx = Ctx()
        res = call_tool("factor.fl_activate", {"factor": args.factor, "why": args.why}, ctx)
        if args.json:
            print(json.dumps(res, ensure_ascii=False, indent=2))
            return
        if not res.get("activated"):
            sys.exit(f"✗ {res.get('reason', '激活失败')}")
        g = res.get("gate") or {}
        print(f"✓ 因子 {res['factor_id']} 已激活（门禁七查全过）")
        for k, c in (g.get("checks") or {}).items():
            mark = "✓" if c.get("ok") else ("△" if c.get("soft") else "✗")
            print(f"  {mark} {k:<13} {c.get('detail', '')}")
    elif args.action == "refs":
        if not args.factor:
            sys.exit("✗ 用法：ap fl refs <factor_id> [--version 1.0.0]")
        ctx = Ctx()
        from factorlib.lifecycle import impact_report
        # fl_* 表在 fl.duckdb（Ctx.db_path 是 α 主库 ap.duckdb）——与 inspect/activate 同口径
        res = impact_report(ctx.sandbox_dir / "data" / "fl.duckdb", ctx.local_repo,
                            args.factor, version=args.version, out_dir=ctx.artifacts_dir)
        print(res["md"])
        print(f"artifacts: {res['artifact_dir']}")
    elif args.action == "inspect":
        ctx = Ctx()
        from applib.factor_inspect import run_inspection
        all_ids = None
        if getattr(args, "all", False) and not args.factor:
            from factorlib.registry import load_registry
            all_ids = [m.id for m in load_registry(ctx.local_repo)]   # draft/active 全含
        res = run_inspection(ctx, factors=[args.factor] if args.factor else all_ids,
                             as_of=args.as_of, drill=args.drill,
                             inject_decay=[args.inject_decay] if args.inject_decay else None)
        if args.json:
            print(json.dumps(res, ensure_ascii=False, indent=2, default=str))
            return
        tag = "⚠ 演练（不落盘）" if res["drill"] else "✓ 巡检"
        print(f"{tag} {res['as_of']}：{res['n_inspected']} 因子 · "
              f"healthy={res['n_healthy']} weakening={res['n_weakening']} "
              f"invalid={res['n_invalid']} unknown={res['n_unknown']}")
        for f in res["factors"]:
            print(f"  {f['factor_id']:<20} {f['health']:<10} "
                  f"{'；'.join(f['reasons']) or '—'}")
        print(f"active vs 软上限: {res['active_vs_cap']['active']}/{res['active_vs_cap']['cap']}")
        print(f"artifacts: {res['artifacts']['md']}")
    elif args.action == "pool":
        ctx = Ctx()
        from factorlib.lifecycle import active_pool
        res = active_pool(ctx.sandbox_dir / "data" / "fl.duckdb", ctx.local_repo)
        if args.json:
            print(json.dumps(res, ensure_ascii=False, indent=2))
            return
        print(f"合成池（active ∧ health≠invalid）· {res['as_of']} · "
              f"active {res['n_active']}/{res['cap']}（软上限 §11 #6） · "
              f"池内 {len(res['pool'])} · 摘除 {len(res['excluded'])}")
        for p in res["pool"]:
            print(f"  ✓ {p['factor_id']:<20} v{p['version']:<8} {p['health']:<10} "
                  f"{p.get('hypothesis_id') or ''}")
            for w in p["warnings"]:
                print(f"      ⚠ {w}")
        for x in res["excluded"]:
            print(f"  ⛔ {x['factor_id']:<20} v{x['version']:<8} {x['health']:<10} {x['reason']}")
    elif args.action == "review":
        ctx = Ctx()
        from applib.ctx import new_run_id
        from applib.factor_inspect import health_rows_from_state
        from applib.hypothesis import (load as hyp_load, review_items_fl, write_review_fl)
        res = review_items_fl(ctx.local_repo, hyp_load(ctx.local_repo),
                              health_rows_from_state(ctx.local_repo))
        res["artifacts"] = write_review_fl(res, ctx.artifacts_dir / new_run_id("hp_review_fl"))
        if args.json:
            print(json.dumps(res, ensure_ascii=False, indent=2))
            return
        print(f"✓ factorlib 假设复查：healthy 抽查池 {res['n_active']} · "
              f"必答 {res['n_mandatory']} · 抽查 {res['n_sample']}")
        for it in res["mandatory"]:
            print(f"  ⚠ 必答 {it['factor_id']}（{it['health']}）→ "
                  f"{it['hypothesis_id'] or '血缘悬空'} {it['question']}")
        for it in res["sample"]:
            print(f"  · 抽查 {it['factor_id']} → {it['hypothesis_id'] or '—'} {it['question']}")
        print(f"artifacts: {res['artifacts']['md']}")
    elif args.action == "candidate":
        # T-F9 personal_clone 通道：ap fl candidate register --factor <id> --from-strategy <名>
        if args.factor != "register" or not getattr(args, "factor_opt", None):
            sys.exit("✗ 用法：ap fl candidate register --factor <factor_id> "
                     "--from-strategy <策略名> --thesis <拆解依据> [--url <…>]")
        ctx = Ctx()
        from applib.hypothesis import personal_clone_register
        res = personal_clone_register(ctx.local_repo, args.factor_opt,
                                      strategy=args.from_strategy, thesis=args.thesis,
                                      url=args.url, direction=args.direction)
        if "error" in res:
            sys.exit(f"✗ {res['error']}")
        print(f"✓ personal_clone 候选登记：{res['factor_id']}"
              f"（factor status={res['factor_status']}）→ 假设 {res['hypothesis_id']}"
              f"（{res['status']}，血缘绑定 {'✓' if res['bound'] else '✗'}）")
        print(f"  来源：克隆自「{res['source']['ref']}」；拆解依据已入假设 statement")
        print("  纪律：个人化来源只影响来源标注，不豁免任何关卡（同一 ①–⑦ 流水线，计划 10.4）")
        print(f"  登记表：{res['path']}")
    elif args.action == "eval":
        # T-F9 一键重检验触发：CN 段照常 M3 重跑；扩展市场段就绪性披露（不抛异常）
        if not args.factor:
            sys.exit("✗ 用法：ap fl eval <factor_id> [--markets CN,HK]")
        ctx = Ctx()
        from applib.factor_eval_fl import run_market_eval
        markets = [m.strip().upper() for m in (args.markets or "CN").split(",") if m.strip()]
        res = run_market_eval(ctx.sandbox_dir / "data" / "fl.duckdb", ctx.local_repo,
                              args.factor, markets)
        print(f"✓ 重检验触发：{res['factor_id']}（v{res['factor_version']}）"
              f" markets={'/'.join(res['requested_markets'])}")
        for mkt, seg in res["segments"].items():
            if seg.get("rechecked"):
                h1 = seg.get("h1") or {}
                print(f"  [{mkt}] ✓ 重检验：direction={seg['direction']} "
                      f"h1 directed_mean={h1.get('directed_mean')} "
                      f"ic_ir={h1.get('ic_ir')} n={h1.get('n_periods')}")
            else:
                print(f"  [{mkt}] △ 就绪性预检（{'ok' if seg.get('ok') else '有缺口'}，"
                      f"standard={seg.get('standard')}）")
                for g in seg.get("gaps") or []:
                    print(f"      ⚠ {g}")
        if res.get("readiness_note"):
            print(f"  口径：{res['readiness_note']}")
    else:
        sys.exit(f"✗ 未知子动作：{args.action}")


def cmd_strategy(args):
    """M6 T-C1/T-C4/T-C5：validate（查询态校验）/ wf（walk-forward）/ backtest（全量回测+归档）"""
    if getattr(args, "action", None) == "wf":
        return _strategy_wf(args)
    if getattr(args, "action", None) == "backtest":
        return _strategy_backtest(args)
    ctx = Ctx()
    from applib.strategy import StrategyError, apply_universe, load_strategy
    try:
        spec = load_strategy(Path(args.file), ctx.local_repo,
                             ctx.sandbox_dir / "data" / "fl.duckdb")
    except StrategyError as e:
        sys.exit(f"✗ {e}")
    print(f"✓ 策略 {spec['id']}（{spec['name']}）校验通过（含合成池准入）")
    for f in spec["factors"]:
        print(f"  factor {f['id']:<22} {f['direction']:<4} weight={f['weight']}"
              f"{'（' + f['weight_kind'] + '）' if f['weight_kind'] != 'fixed' else ''}")
    print(f"  composite: top_n={spec['top_n']} method={'/'.join(spec['methods'])}")
    print(f"  rebalance={spec['rebalance']} exec={spec['exec']} "
          f"benchmark={spec['benchmark']} factor_version={spec['factor_version']}")
    if set(spec["universe"].get("markets") or ["CN"]) - {"CN"}:
        print(f"  universe markets={'/'.join(spec['universe']['markets'])}"
              f"（T-F9 扩展位·就绪性预检缺口清单）：")
        for g in spec.get("market_gaps") or []:
            print(f"    ⚠ {g}")
    if spec["universe"]["filters"]:
        bars = json.loads((ctx.local_repo / "market" / "daily_bars.json").read_text(encoding="utf-8"))
        dts = sorted({r["dt"] for rows in bars.values() for r in rows})
        month_end = sorted({max(x for x in dts if x[:7] == ym) for ym in {x[:7] for x in dts}})
        uni, snap = apply_universe(spec["universe"]["filters"], bars,
                                   month_end[-3:], repo=ctx.local_repo)
        print(f"  universe {spec['universe']['base']} 过滤器预览（近 3 个月末）：")
        for fname, s in snap.items():
            tag = f"（degraded：{s['degraded']}）" if s.get("degraded") else ""
            print(f"    {fname:<20} {s['n_before']} → {s['n_after']}{tag}")
    print("→ 下一步：ap strategy wf <file>（walk-forward）/ ap strategy backtest <file>（T-C5）")


def _strategy_wf(args):
    """T-C4：策略层 walk-forward（扩展窗口滚动）+ lockbox 一次性验证（+ 可选敏感性网格）。

    一次性协议：lockbox ledger 固定落 artifacts/strategy_wf/（跨运行持久），同策略 id
    二次验证即报错——重跑须升 strategy version（DSL id 后缀 _v2）。
    """
    ctx = Ctx()
    from applib.ctx import new_run_id
    from applib.strategy import StrategyError, load_strategy
    from applib.strategy_wf import StrategyWfError, strategy_walk_forward
    if not args.file:
        sys.exit("✗ wf 需要策略 YAML 路径")
    db_path = ctx.sandbox_dir / "data" / "fl.duckdb"
    try:
        from applib.strategy import assert_cn_only
        spec = load_strategy(Path(args.file), ctx.local_repo, db_path)
        assert_cn_only(spec)                     # T-F9：混合市场回测 M8+ 前显式拒绝
    except StrategyError as e:
        sys.exit(f"✗ {e}")
    cfg = {"train_min_months": args.train_min_months, "valid_months": args.valid_months,
           "step_months": args.step_months, "lockbox_start": args.lockbox_start,
           "sample_start": args.sample_start, "sensitivity": args.sensitivity}
    adir = ctx.artifacts_dir / "strategy_wf"     # 固定目录：ledger 跨运行持久（§11 #4）
    try:
        res = strategy_walk_forward(spec, db_path, ctx.local_repo, cfg, artifact_dir=adir)
    except StrategyWfError as e:
        sys.exit(f"✗ {e}")
    run_dir = ctx.artifacts_dir / new_run_id("strategy_wf")
    run_dir.mkdir(parents=True, exist_ok=True)
    (run_dir / "report.json").write_text(
        json.dumps(res, ensure_ascii=False, indent=2), encoding="utf-8")

    fnum = lambda x, d=4: "—" if x is None else f"{x:.{d}f}"     # noqa: E731
    print(f"✓ 策略 {spec['id']}（{spec['name']}）walk-forward：{res['n_folds']} 折"
          f"（core {res['core_months_n']} 月{('，sample_start=' + res['cfg']['sample_start'])
                if res['cfg']['sample_start'] else ''}；lockbox 封存 {res['n_sealed']} 月 "
          f"= {res['sealed_share']:.0%}）")
    for f in res["folds"]:
        print(f"  fold {f['fold']}  train {f['train_range'][0]}..{f['train_range'][1]}"
              f"（{f['train_months_n']} 月）→ valid {f['valid_range'][0]}..{f['valid_range'][1]}"
              f"｜method={f['method_chosen']}｜is_sharpe={fnum(f['is_sharpe'])}"
              f" oos_sharpe={fnum(f['oos_sharpe'])} oos_ret={fnum(f['oos_return'])}")
    agg = res["aggregate"]
    print(f"  aggregate：oos_mean_sharpe={fnum(agg['oos_mean_sharpe'])}"
          f" oos_ir={fnum(agg['oos_ir'])} decay(is→oos)={fnum(agg['decay'])}")
    print(f"  最终选法（全 core）：{res['final_method']}"
          f"｜各法 is-sharpe {json.dumps({k: fnum(v) for k, v in res['final_method_sharpes'].items()})}")
    lb = res["lockbox"]
    print(f"  lockbox（一次性验证 #{lb['verify_count']}）{lb['lockbox_start']} 起"
          f" {lb['n_sealed']} 月：ret={fnum(lb['oos']['total_return'])}"
          f" sharpe={fnum(lb['oos']['sharpe'])} max_dd={fnum(lb['oos']['max_dd'])}"
          f"｜runid={lb['runid']}")
    print(f"    （重跑须升 strategy version：DSL id 后缀 _v2 —— §11 #4 样本外只验证一次）")
    if res["sensitivity"]:
        s = res["sensitivity"]
        print(f"  敏感性（{s['window'][0]}..{s['window'][1]}）："
              f"rank_stable={s['rank_stable']} 终值带宽 band={fnum(s['band'])}")
        for ax, v in s["axes"].items():
            if "skipped" in v:
                print(f"    {ax}: skip（{v['skipped'][:46]}…）")
            else:
                vs = {k: {m: fnum(x, 4) for m, x in d.items()} if isinstance(d, dict) else d
                      for k, d in v["values"].items()}
                print(f"    {ax}（base={v.get('base')}）: {json.dumps(vs, ensure_ascii=False)}")
    print(f"artifacts: {run_dir / 'report.json'}")
    lb_path = lb.get("artifact") or str(adir / f"lockbox_{spec['id']}_{lb['runid']}" / "report.json")
    print(f"  lockbox 报告: {lb_path}")


def _strategy_backtest(args):
    """T-C5：策略 YAML × 四法并列全量回测 + m6_bt 归档（T-C6 DoD#3 素材）。

    主回测 = 四法并列（method_primary 缺省 equal，双轨成本 + 暴露/换手/分布三节齐全）；
    另每法单独回测一遍（不同选股 → 不同净值轨）→ four_method_summary 注入主结果，
    md/html 渲染层 ⑧/⑨ 节并列呈现（终值/夏普/回撤/卡玛 + 降级/冻结闸标记）。
    """
    ctx = Ctx()
    from applib import backtest as BT
    from applib import report as RP
    from applib.ctx import new_run_id
    from applib.factor_backtest import FactorBacktestError, run_factor_backtest
    from applib.strategy import StrategyError, load_strategy
    if not args.file:
        sys.exit("✗ backtest 需要策略 YAML 路径")
    fnum = lambda x, d=4: "—" if x is None else f"{x:.{d}f}"     # noqa: E731
    pct = lambda v, d=1: "—" if v is None else f"{v * 100:.{d}f}%"   # noqa: E731
    db_path = ctx.sandbox_dir / "data" / "fl.duckdb"
    try:
        from applib.strategy import assert_cn_only
        spec = load_strategy(Path(args.file), ctx.local_repo, db_path)
        assert_cn_only(spec)                     # T-F9：混合市场回测 M8+ 前显式拒绝
    except StrategyError as e:
        sys.exit(f"✗ {e}")
    fnum = lambda x, d=4: "—" if x is None else f"{x:.{d}f}"     # noqa: E731
    try:
        bt = run_factor_backtest(db_path, ctx.local_repo, spec,
                                 start=args.start, end=args.end)
        fm = {}
        for m in spec["methods"]:                # 每法单独轨：不同选股 → 不同净值
            s2 = dict(spec)
            s2["methods"] = [m]
            s2["method_primary"] = m
            b2 = run_factor_backtest(db_path, ctx.local_repo, s2,
                                     start=args.start, end=args.end)
            sm = b2["strategy_metrics"]
            gates = (b2.get("composite") or {}).get("gates") or []
            fm[m] = {"total_return": sm["total_return"], "sharpe": sm["sharpe"],
                     "max_dd": sm["max_dd"], "calmar": sm["calmar"],
                     "n_degraded_days": sum(1 for g in gates if g["degraded"].get(m)),
                     "n_frozen_days": sum(1 for g in gates if (g.get("gate2") or {}).get("frozen"))}
        bt["four_method_summary"] = fm
    except FactorBacktestError as e:
        sys.exit(f"✗ {e}")

    run_dir = ctx.artifacts_dir / f"m6_bt_{spec['id']}_{new_run_id('bt')}"
    run_dir.mkdir(parents=True, exist_ok=True)
    (run_dir / "report.json").write_text(
        json.dumps(bt, ensure_ascii=False, indent=2), encoding="utf-8")
    (run_dir / "report.md").write_text(BT.render_md(bt), encoding="utf-8")
    (run_dir / "report.html").write_text(BT.render_html(bt, RP.CSS), encoding="utf-8")

    sm = bt["strategy_metrics"]
    print(f"✓ 策略 {spec['id']}（{spec['name']}）全量回测 {bt['start']}~{bt['end']}"
          f"（{bt['n_days']} 交易日 · exec={spec['exec']} · 主选法 equal）")
    print(f"  主轨：累计 {pct(sm['total_return'])} 年化 {pct(sm['cagr'])} "
          f"夏普 {fnum(sm['sharpe'])} 回撤 {pct(sm['max_dd'])} 卡玛 {fnum(sm['calmar'])}")
    nl = bt.get("no_lookahead") or {}
    print(f"  审计：pit={bt['pit_audit']['check']} no_lookahead={nl.get('check', 'N/A(close)')}"
          f"｜快照落库 {bt['composite']['snapshot_written']} 行")
    print("  四法并列：")
    for m, v in fm.items():
        print(f"    {m:<7} 终值 {pct(v['total_return'])} 夏普 {fnum(v['sharpe'])} "
              f"回撤 {pct(v['max_dd'])} 卡玛 {fnum(v['calmar'])}"
              f"｜降级 {v['n_degraded_days']} 日 · 冻结 {v['n_frozen_days']} 日")
    exp_ok = all(c["consistent"]
                 for c in (BT._exposure_check(bt) or {}).get("check", {}).values()) \
        if bt.get("factor_exposure") else None
    print(f"  暴露方向一致性：{'—（无暴露数据）' if exp_ok is None else ('✓ 全部一致' if exp_ok else '✗ 存在违规，见报告⑥节')}")
    print(f"artifacts: {run_dir / 'report.md'}（+ .html / .json）")


def cmd_decay(args):
    """R4 时间延迟衰减：引擎 B 信号 0/1/2 日执行衰减表 + registry 降级联动"""
    ctx = Ctx()
    res = call_tool("research.delay_decay",
                    {"signals": args.signals, "horizon": args.horizon,
                     "start": args.start, "end": args.end}, ctx)
    if "error" in res:
        sys.exit(f"✗ {res['error']}")
    print(json.dumps({"run_id": res["run_id"], "n_events": res["n_events"],
                      "n_signals": res["n_signals"]}, ensure_ascii=False, indent=2))
    fl = {"ok": "✓ 稳健", "insufficient": "◻ 待补强", "fast_decay": "⚠ 衰减过快"}
    for s in res["signals"]:
        print(f"  {s['signal_id']:<18} {s['type']:<12} 事件 {s['n_events']:>5}  {fl[s['flag']]}")
    for c in res["registry_changes"]:
        print(f"  registry: {c['registry_id']} → {c.get('status') or '维持'}"
              f"（flag={c.get('flag')} · 事件 {c.get('n_events')}）")
    print("artifacts:")
    for k, v in res["artifacts"].items():
        print(f"  - {k}: {v}")


def cmd_risk(args):
    """P0 风险模型最小集：0.1 波动目标 / 0.2 等风险 / 0.3 敞口 / 0.4 限额 → HTML+MD+JSON"""
    ctx = Ctx()
    res = call_tool("research.risk_model",
                    {"portfolio": args.portfolio, "start": args.start, "end": args.end}, ctx)
    if "error" in res:
        sys.exit(f"✗ {res['error']}")
    d = res["dod"]
    print(json.dumps({"run_id": res["run_id"], "portfolio": res["portfolio"],
                      "start": res["start"], "end": res["end"], "n_days": res["n_days"]},
                     ensure_ascii=False, indent=2))
    print("  DoD：" + " · ".join(f"P0.{i+1} {'✅' if d[f'p0_{i+1}'] else '❌'}" for i in range(4)))
    b, v = res["p0_1"]["base_metrics"], res["p0_1"]["vt_metrics"]
    print(f"  P0.1 波动目标：年化波动 {b['ann_vol']*100:.1f}% → {v['ann_vol']*100:.1f}%"
          f"｜最大回撤 {b['max_dd']*100:.1f}% → {v['max_dd']*100:.1f}%"
          f"｜{res['p0_1']['dod']['detail']}")
    p2 = res["p0_2"]
    fnum = lambda x, d=2: "—" if x is None else f"{x:.{d}f}"     # noqa: E731
    print(f"  P0.2 等风险：夏普 {fnum(p2['equal']['sharpe'])} → {fnum(p2['equal_risk']['sharpe'])}"
          f"｜风险贡献方差 {fnum(p2['equal']['rc_var'], 4)} → {fnum(p2['equal_risk']['rc_var'], 4)}")
    p3 = res["p0_3"]
    print(f"  P0.3 敞口：{p3['dod']['detail']}（as_of {p3['current']['as_of']}）")
    for f in p3["flags"]:
        print(f"      ⚠ {f['dim']}/{f['key']}：偏离 {f['dev']:.3f}（{f['z']:.1f}σ）")
    p4 = res["p0_4"]
    print(f"  P0.4 限额：{p4['dod']['detail']}")
    print("artifacts:")
    for k, v in res["artifacts"].items():
        print(f"  - {k}: {v}")


def cmd_p1_eval(args):
    """P1 因子补齐验收：增量相关（vs mom_12_1，|ρ|<0.6）+ A/B 条件门对照 → HTML+MD+JSON"""
    ctx = Ctx()
    res = call_tool("research.p1_eval",
                    {"portfolio": args.portfolio, "start": args.start, "end": args.end,
                     "gate_damp": args.gate_damp, "gate_redistribute": args.gate_redistribute}, ctx)
    if "error" in res:
        sys.exit(f"✗ {res['error']}")
    inc = res["incremental"]
    d = res["dod"]
    print(json.dumps({"run_id": res["run_id"], "dod": d}, ensure_ascii=False, indent=2))
    print(f"  ① 增量价值关（vs {inc['base']}，阈值 |ρ| < {inc['threshold']}）："
          f"{inc['n_pass']}/{inc['n']} 通过")
    for r in inc["rows"]:
        if r.get("error"):
            print(f"      ✗ {r['factor']}：{r['error']}")
            continue
        print(f"      {'✓' if r['pass'] else '✗'} {r['factor']:<14} ρ={_fmt(r['mean_rho'])} "
              f"（{r['n_dates']} 月）")
    gc = res.get("gate_config") or {}
    print(f"  ② A/B 条件门对照（引擎A 买入需引擎B 不持卖出信号；衰减×{gc.get('gate_damp')}"
          f"{'，再分配' if gc.get('gate_redistribute') else '，释放额度留现金'}）：")
    for a in res["ab"]:
        if "error" in a:
            print(f"      ✗ {a['portfolio']}：{a['error']}")
            continue
        g = a.get("gate") or {}
        print(f"      {'✓' if a['dod']['ok'] else '✗'} {a['portfolio']:<8} "
              f"累计收益增强 {a['gain_total_return']*100:+.2f}pct ｜ 回撤 "
              f"{'改善' if a['dd_improvement'] > 0 else '持平' if abs(a['dd_improvement']) < 1e-9 else '恶化'} "
              f"{abs(a['dd_improvement'])*100:.2f}pct"
              + (f" ｜ 门事件 {g.get('n_rebalances')} 时点（平均持仓 {g.get('avg_invested_ratio'):.2f}）" if g else ""))
    print("artifacts:")
    for k, v in res["artifacts"].items():
        print(f"  - {k}: {v}")


def cmd_regime(args):
    """H-0006 港美利率/汇率 regime：carry_hk_us / peg_pressure + 市场级择时状态"""
    ctx = Ctx()
    res = call_tool("factor.regime", {"as_of": args.as_of, "chg_win": args.chg_win}, ctx)
    if "error" in res:
        sys.exit(f"✗ {res['error']}")
    d = res["detail"]
    print(json.dumps({"as_of": res["as_of"], "state": d["state"], "chg_win": res["chg_win"]},
                     ensure_ascii=False, indent=2))
    print(f"  港美息差 carry_hk_us = {_fmt(d['carry_hk_us'])} pct"
          f"（HIBOR1M {d['hibor1m']} − Fed {d['fed']}）｜{res['chg_win']} 日变化 {_fmt(d['carry_chg20'])} pct")
    print(f"  弱方保证压力 peg_pressure = {_fmt(d['peg_pressure'])}（USDHKD {d['usdhkd']}）"
          f"｜触弱方 {'是' if d['peg_weak_side'] else '否'}｜息差急扩 {'是' if d['carry_widen'] else '否'}")
    print(f"  状态：{d['state']}（outflow_pressure=港资外流压力偏高，市场级择时信号）")


def cmd_macro(args):
    """引擎C 宏观流动性 regime：A/HK/US 三市场温度计 + 维度分 + 全局极端态（市场级条件变量）"""
    ctx = Ctx()
    res = call_tool("factor.macro", {"as_of": args.as_of}, ctx)
    if "error" in res:
        sys.exit(f"✗ {res['error']}")
    g = res["global"]
    print(json.dumps({"as_of": res["as_of"], "global": g}, ensure_ascii=False, indent=2))
    label = {-2: "深度紧缩", -1: "偏紧", 0: "中性", 1: "偏宽松", 2: "深度宽松"}
    for m in ("A", "HK", "US"):
        mk = res["markets"].get(m) or {}
        if mk.get("status") == "unavailable":
            print(f"  {m}: 数据不可用")
        else:
            dims_txt = " ｜ ".join(f"{d} {v['score']:+.2f}({v['n_used']}/{v['n_total']})"
                                   for d, v in mk["dims"].items() if v["score"] is not None)
            print(f"  {m}: score={mk['score']:+.2f} level={mk['level']:+d}（{label.get(mk['level'], '?')}）"
                  f" ｜ {dims_txt}")
    for n in res["notes"]:
        print(f"  · {n}")
    print(f"  状态：{g['state']}（tight_stress=全局紧缩压力 / easy_froth=宽松过热，市场级择时参考）")


def cmd_metric(args):
    """指标全生命周期管理（T-R7/T-R8）：list/show/add/update/remove + test/diff 调试闭环。

    管理动作写 sandbox 注册表（ctx.local_repo/factors/registry.json），经 `ap push`
    门禁回流 remote；test/diff 只读引擎、只写 artifacts（人决策红线）。
    """
    from .engines import debug as DBG
    ctx = Ctx()
    if args.action in ("test", "diff"):
        if getattr(args, "ctx", None):               # --ctx 指向 sandbox 目录（α 单仓习惯）
            ctx.sandbox_dir = Path(args.ctx)
        if args.action == "test":
            try:
                overrides = json.loads(args.overrides_json) if args.overrides_json else None
            except ValueError:
                sys.exit(f"✗ --overrides-json 不是合法 JSON：{args.overrides_json!r}")
            res = DBG.metric_test(ctx, args.id, tool=args.tool, grid=args.grid,
                                  overrides=overrides, portfolio=args.portfolio)
            if "error" in res:
                sys.exit(f"✗ {res['error']}")
            print(json.dumps({k: res[k] for k in ("metric_id", "engine", "tool", "summary")},
                             ensure_ascii=False, indent=2))
            print(f"  artifacts: {res['file']}")
            print("  verdict 仅提示；采纳/否决由人决策（改注册表走 ap metric update）")
        else:
            res = DBG.metric_diff(ctx, args.id,
                                  baseline=args.baseline, changelog_idx=args.changelog_idx)
            if "error" in res:
                sys.exit(f"✗ {res['error']}")
            print(json.dumps({k: res[k] for k in ("metric_id", "engine", "baseline_version",
                                                  "current_version", "config_delta",
                                                  "output_delta")},
                             ensure_ascii=False, indent=2))
            for f in res["files"]:
                print(f"  artifacts: {f}")
        return

    reg_path = ctx.local_repo / "factors" / "registry.json"
    from .engines.base import apply_metric_change, load_registry, validate_registry
    reg = load_registry(ctx.local_repo)          # 缺失/损坏 → 内置默认（透明降级）

    def _die(msg):
        sys.exit(f"✗ {msg}")

    def _dump():
        reg_path.parent.mkdir(parents=True, exist_ok=True)
        reg_path.write_text(json.dumps(reg, ensure_ascii=False, indent=2), encoding="utf-8")

    def _loc(mid):
        hits = [eid for eid, sec in reg["engines"].items()
                if (not args.engine or eid == args.engine)
                and any(m.get("id") == mid for m in sec.get("metrics", []))]
        if not hits:
            _die(f"指标不存在：{mid}")
        if len(hits) > 1:
            _die(f"指标 {mid} 在多个引擎存在（{hits}），请加 --engine")
        return hits[0]

    def _apply(eid, change):
        nonlocal reg
        try:
            reg, rec = apply_metric_change(reg, eid, change)
        except ValueError as e:
            _die(str(e))
        return rec

    if args.action == "list":
        rows = []
        for eid, sec in reg["engines"].items():
            if args.engine and eid != args.engine:
                continue
            for m in sec.get("metrics", []):
                if args.status and m.get("status", "active") != args.status:
                    continue
                rows.append((eid, m))
        if args.as_json:
            print(json.dumps({"metrics": [dict(m, engine=eid) for eid, m in rows],
                              "changelog_n": len(reg.get("changelog") or [])},
                             ensure_ascii=False, indent=2))
            return
        print(f"{'引擎':<7}{'id':<20}{'dim':<9}{'状态':<14}{'ver':<5}formula")
        for eid, m in rows:
            print(f"{eid:<7}{m['id']:<20}{str(m.get('dim') or '—'):<9}"
                  f"{m.get('status', 'active'):<14}{m.get('version', 1):<5}{m.get('formula', '')}")
        print(f"共 {len(rows)} 条 ｜ 注册表 {reg_path}（--json 机器可读）")
        return

    if args.action == "show":
        if not args.id:
            _die("show 需要指标 id")
        eid = _loc(args.id)
        m = next(m for m in reg["engines"][eid]["metrics"] if m["id"] == args.id)
        print(json.dumps(dict(m, engine=eid), ensure_ascii=False, indent=2))
        return

    if args.action == "add":
        if not (args.engine and args.id and args.formula and args.hypothesis_id):
            _die("add 需要 --engine --id --formula --hypothesis-id")
        spec = {"formula": args.formula}
        if args.dim:
            spec["dim"] = args.dim
        if args.market:
            spec["market"] = args.market
        if args.scorer_type:
            spec["scorer"] = {"type": args.scorer_type, "arg": args.scorer_arg}
        if args.params_json:
            try:
                spec["params"] = json.loads(args.params_json)
            except ValueError:
                _die(f"--params-json 不是合法 JSON：{args.params_json!r}")
        rec = _apply(args.engine, {"op": "add", "id": args.id, "patch": spec,
                                   "hypothesis_id": args.hypothesis_id,
                                   "reason": args.reason or ""})
        _dump()
        if args.as_json:
            print(json.dumps(rec["after"], ensure_ascii=False, indent=2))
        else:
            a = rec["after"]
            print(f"✓ 已新增 {args.engine}.{args.id}"
                  f"（status={a['status']}，v{a['version']}，formula={a['formula']}）")
        print("已写入 sandbox 注册表；`ap push` 提交变更（需六项验证全绿）")
        return

    if args.action == "update":
        patch = {}
        for kv in args.set_kv or []:
            if "=" not in kv:
                _die(f"--set 需要 k=v 形式（实际 {kv!r}）")
            k, v = kv.split("=", 1)
            node = patch
            for part in k.split(".")[:-1]:        # 点路径赋值：params.win=25
                node = node.setdefault(part, {})
            try:
                val = json.loads(v)               # 值按 JSON 解析（失败回退字符串）
            except ValueError:
                val = v
            node[k.split(".")[-1]] = val
        weights_changed = False
        if args.weights_json:
            if not args.engine:
                _die("--weights-json 需要同时指定 --engine（引擎级权重表）")
            if args.engine == "beta":
                _die("beta 无维度权重表（dim_weights）；可改 --set defaults.* 或指标 params")
            try:
                w = json.loads(Path(args.weights_json).read_text(encoding="utf-8"))
            except (OSError, ValueError) as e:
                _die(f"权重表文件不可读：{e}")
            before_w = reg["engines"][args.engine].get("dim_weights")
            reg["engines"][args.engine]["dim_weights"] = w
            errs = validate_registry(reg)
            if errs:
                _die("权重表未通过注册表校验：" + "；".join(errs[:2]))
            reg.setdefault("changelog", []).append(
                {"ts": time.strftime("%Y-%m-%dT%H:%M:%S"), "op": "update",
                 "engine": args.engine, "id": "(engine:dim_weights)",
                 "before": before_w, "after": w,
                 "hypothesis_id": args.hypothesis_id, "reason": args.reason or ""})
            weights_changed = True
        if args.id:
            if not patch and weights_changed:
                _dump()                            # 仅权重表变更
                print(f"✓ 已更新 {args.engine} 维度权重表（--weights-json）")
                print("已写入 sandbox 注册表；`ap push` 提交变更（需六项验证全绿）")
                return
            rec = _apply(_loc(args.id), {"op": "update", "id": args.id, "patch": patch,
                                         "hypothesis_id": args.hypothesis_id,
                                         "reason": args.reason or ""})
            _dump()
            if args.as_json:
                print(json.dumps({"before": rec["before"], "after": rec["after"]},
                                 ensure_ascii=False, indent=2))
            else:
                a = rec["after"]
                print(f"✓ 已更新 {rec['engine']}.{args.id}"
                      f"（status={a['status']}，v{a['version']}，params={a.get('params')}）")
        elif not weights_changed:
            _die("update 需要指标 id 或 --weights-json")
        else:
            _dump()
            print(f"✓ 已更新 {args.engine} 维度权重表（--weights-json）")
        print("已写入 sandbox 注册表；`ap push` 提交变更（需六项验证全绿）")
        return

    # remove
    if not args.id:
        _die("remove 需要指标 id")
    rec = _apply(_loc(args.id), {"op": "remove", "id": args.id,
                                 "hypothesis_id": args.hypothesis_id,
                                 "reason": args.reason or ""})
    _dump()
    print(f"✓ 已软删 {rec['engine']}.{args.id}（status=inactive，条目保留；update 可复活）")
    print("已写入 sandbox 注册表；`ap push` 提交变更（需六项验证全绿）")


def cmd_altdata_sync(args):
    """二线源同步：申万行业分类变迁史（PIT，消除行业前视偏差）→ dim/industry_sw.json"""
    ctx = Ctx()
    res = call_tool("altdata.sync", {"strict_ssl": args.strict_ssl}, ctx)
    if "error" in res:
        sys.exit(f"✗ {res['error']}")
    print(json.dumps(res, ensure_ascii=False, indent=2))
    if res.get("insecure_ssl_used"):
        print("  ⚠ TLS 已降级（verify=False）：本机存在中间人代理；provenance 已登记 ssl_verify=false")


def cmd_industry_asof(args):
    """PIT 行业查询：申万行业变迁史取 as_of 之前最后一次调整（无前视）"""
    ctx = Ctx()
    res = call_tool("altdata.industry_asof",
                    {"codes": args.codes, "as_of": args.as_of, "level": args.level}, ctx)
    if "error" in res:
        sys.exit(f"✗ {res['error']}")
    print(f"as_of={res['as_of']}  层级={res['level']}  源={res['source']}")
    print(f"  {'secid':<12}{'申万(PIT)':<12}{'自':<12}主源静态标注")
    for r in res["rows"]:
        print(f"  {r['secid']:<12}{str(r['industry_sw'] or '—'):<12}"
              f"{str(r['since'] or '—'):<12}{r['industry_static'] or '—'}")
    if res.get("n_no_record"):
        print(f"  注：{res['n_no_record']}/{res['n']} 个标的在该日无申万归属记录（未上市/未收录）")
    print("  中性化映射 industries =", json.dumps(res["industries"], ensure_ascii=False))


def cmd_research_source(args):
    """R3 研报来源：东财研报（评级/目标价/EPS 预测）+ 同花顺一致预期"""
    ctx = Ctx()
    res = call_tool("research.source",
                    {"code": args.code, "max_pages": args.max_pages,
                     "strict_ssl": args.strict_ssl}, ctx)
    if "error" in res:
        sys.exit(f"✗ {res['error']}")
    print(f"{res['code']}  源={res['source']}  研报 {res['n_reports']} 条")
    for r in res["reports"][:5]:
        print(f"  {r['date']} | {r['org'][:12]:<12} | {r['rating'] or '—':<6} | "
              f"EPS次年={r['eps_next_year']} | {r['title'][:34]}")
    if res["eps_consensus"]:
        print("  同花顺一致预期 EPS：")
        for e in res["eps_consensus"]:
            print(f"    {e['year']} | 机构 {e['n_org']} | 均值 {e['mean']} | "
                  f"区间 {e['min']}~{e['max']}")
    for e in res["errors"]:
        print(f"  ⚠ {e}")


def _fmt(v, d=3):
    return "—" if v is None else f"{v:+.{d}f}"


def cmd_schedule(args):
    if args.action == "ls":
        print("（α 打桩）自动化定制任务由 IDE agent headless 模式承载：")
        print("  opencode -p \"执行 tasks/review_global.yaml\"   # 单次非交互")
        print("  codex exec \"ap run tasks/review_global.yaml\"  # 单次非交互")
        print("  cron: 30 20 * * 1-5  → ap daemon（V4-RC 实现）")
        print("  M5 季度巡检（sandbox 无常驻 daemon → cron 模板登记；--as-of 可补跑重放）：")
        print("    0 20 1 1,4,7,10 *  cd /workspace/alphapocket && \\")
        print("      $VENV/bin/python ap fl inspect && $VENV/bin/python ap fl review")
    else:
        print("（α 打桩）ap schedule add/rm 于 V4-RC 随 ap daemon 实现")


def cmd_serve(args):
    from .mcp_server import serve
    serve()


def cmd_status(args):
    ctx = Ctx()
    print(json.dumps(R.repo_status({}, ctx), ensure_ascii=False, indent=2))
    print("tools:", len(TOOLS), "|", ", ".join(sorted(TOOLS)))


def main():
    p = argparse.ArgumentParser(prog="ap", description="AlphaPocket 本地运行时 CLI（V4-α）")
    sub = p.add_subparsers(dest="cmd", required=True)

    sub.add_parser("auth", help="PAT 登录（Device Code Flow + TOTP）").set_defaults(fn=cmd_auth)

    f = sub.add_parser("fetch", help="AKShare 真实数据 → remote/repo（含溯源+PIT规则）")
    f.add_argument("--start", default="2021-01-01", help="行情起始日（默认 2021-01-01，覆盖五年分位+250日因子）")
    f.add_argument("--mode", default=None, help="拉取模式：bootstrap（默认，重建+全量）/ "
                                                "incremental（快照+按 secid 增量合并）/ refresh（重建+窗口重拉，须 --secid）")
    f.add_argument("--secid", nargs="+", default=None, help="仅拉取这些 secid（如 --secid 601058.SH 00700.HK）")
    f.set_defaults(fn=cmd_fetch)

    sub.add_parser("clone", help="远端 main → sandbox（AKShare 真实数据）").set_defaults(fn=cmd_clone)

    r = sub.add_parser("run", help="在 sandbox 内执行任务（Task YAML）")
    r.add_argument("task"); r.add_argument("--as-of", default=None)
    r.set_defaults(fn=cmd_run)

    pu = sub.add_parser("push", help="提交变更 → 远端 CR")
    pu.add_argument("-m", "--message", default="update from local run")
    pu.add_argument("--approve", default=None, metavar="CR", help="人工确认 merge（模拟 H5 PR 队列）")
    pu.set_defaults(fn=lambda a: cmd_push_approve(type("A", (), {"cr": a.approve})()) if a.approve else cmd_push(a))

    ep = sub.add_parser("exec-plan", help="由调仓单生成可下单执行单（待人工确认，不自动下单）")
    ep.add_argument("--portfolio", default="multi5")
    ep.add_argument("--capital", type=float, default=1_000_000, help="基准资金（元），默认 1,000,000")
    ep.add_argument("--run", default=None, help="指定 run_id（默认取该组合最近一次调仓单）")
    ep.set_defaults(fn=cmd_exec_plan)

    bt = sub.add_parser("backtest", help="组合回测（等权/等风险加权·月度再平衡 vs 沪深300ETF）")
    bt.add_argument("--portfolio", default="multi5")
    bt.add_argument("--start", default=None, help="起始日（默认全样本 2021-01-04）")
    bt.add_argument("--end", default=None, help="结束日（默认最新交易日）")
    bt.add_argument("--rf", type=float, default=0.0, help="无风险利率（年化，默认 0）")
    bt.add_argument("--weighting", default="equal", choices=["equal", "equal_risk"],
                    help="加权方案（默认 equal；equal_risk=250 日波动倒数，P0.2）")
    bt.set_defaults(fn=cmd_backtest)

    sen = sub.add_parser("sensitivity", help="R1 参数敏感性扫描（OAT + 双参数热力图 + 尖峰 flag）")
    sen.add_argument("--portfolio", default="cn4", help="组合 id 或 all（cn4+multi5），默认 cn4")
    sen.add_argument("--start", default=None); sen.add_argument("--end", default=None)
    sen.set_defaults(fn=cmd_sensitivity)

    wf = sub.add_parser("walkforward", help="R2 walk-forward 样本外验证（扩展窗口滚动 + lockbox 封存）")
    wf.add_argument("--factor", default="all", help="因子 id 或 all（默认全部已注册因子）")
    wf.add_argument("--start", default=None); wf.add_argument("--end", default=None)
    wf.add_argument("--train-min-months", type=int, default=24, help="首个 train 窗口长度（月），默认 24")
    wf.add_argument("--valid-months", type=int, default=6, help="valid 窗口长度（月），默认 6")
    wf.add_argument("--step-months", type=int, default=6, help="fold 前滚步长（月），默认 6")
    wf.set_defaults(fn=cmd_walkforward)

    au = sub.add_parser("audit-split", help="R2 audit_split 四查（字段完备/取数越界/lockbox 一次性/折叠无重叠）")
    au.set_defaults(fn=cmd_audit_split)

    hy = sub.add_parser("hypothesis", help="R3 研报→假设登记通道（list/register/link/set-status/review）")
    hy.add_argument("action", choices=["list", "register", "link", "set-status", "review"])
    hy.add_argument("--factor", default=None, help="按因子反向查血缘（list/link）")
    hy.add_argument("--status", default=None, help="状态过滤（list）或目标状态（set-status）")
    hy.add_argument("--hypothesis", default=None, help="假设 id（link/set-status）")
    hy.add_argument("--title", default=None, help="假设标题（register）")
    hy.add_argument("--statement", default=None, help="可检验命题（register）")
    hy.add_argument("--direction", default="positive", choices=list(DIRECTIONS), help="预期方向（register）")
    hy.add_argument("--source-type", default="book", choices=list(SOURCE_TYPES), help="来源类型（register）")
    hy.add_argument("--source-ref", default=None, help="来源出处（register，可追溯）")
    hy.add_argument("--url", default=None, help="来源链接（register）")
    hy.add_argument("--universe", default="CN", help="universe（register，默认 CN）")
    hy.add_argument("--linked", default=None, help="关联因子 id，逗号分隔（register）")
    hy.set_defaults(fn=cmd_hypothesis)

    ac = sub.add_parser("activate", help="R3/M5 8.1 draft→active 激活门禁（无 hypothesis_id 拒绝）")
    ac.add_argument("--factor", required=True, help="待激活因子 id（须为 draft 且带 hypothesis_id）")
    ac.set_defaults(fn=cmd_activate)

    dc = sub.add_parser("decay", help="R4 时间延迟衰减（引擎B 0/1/2 日执行衰减表 + 降级联动）")
    dc.add_argument("--signals", default="all", help="信号 id 或 all（默认全部引擎B信号）")
    dc.add_argument("--horizon", type=int, default=20, help="前向窗口（交易日），默认 20")
    dc.add_argument("--start", default=None); dc.add_argument("--end", default=None)
    dc.set_defaults(fn=cmd_decay)

    rk = sub.add_parser("risk", help="P0 风险模型（波动目标/等风险/敞口/限额）")
    rk.add_argument("--portfolio", default="cn4", help="组合 id（默认 cn4）")
    rk.add_argument("--start", default=None); rk.add_argument("--end", default=None)
    rk.set_defaults(fn=cmd_risk)

    p1 = sub.add_parser("p1-eval", help="P1 因子补齐验收（增量相关 |ρ|<0.6 + A/B 条件门对照）")
    p1.add_argument("--portfolio", default="all", help="组合 id 或 all（cn4+multi5），默认 all")
    p1.add_argument("--start", default=None); p1.add_argument("--end", default=None)
    p1.add_argument("--gate-damp", dest="gate_damp", type=float, default=None,
                    help="条件门软衰减系数（默认 0.5；0=硬剔除）")
    p1.add_argument("--gate-redistribute", dest="gate_redistribute", action="store_true",
                    help="释放额度再分配给未触发标的（默认留现金降敞口）")
    p1.set_defaults(fn=cmd_p1_eval)

    rg = sub.add_parser("regime", help="H-0006：港美利率/汇率 regime（carry_hk_us/peg_pressure，市场级择时）")
    rg.add_argument("--as-of", dest="as_of", default=None, help="日期 YYYY-MM-DD（默认最新）")
    rg.add_argument("--chg-win", dest="chg_win", type=int, default=20,
                    help="息差变化窗口（交易日），默认 20")
    rg.set_defaults(fn=cmd_regime)

    mc = sub.add_parser("macro", help="引擎C：宏观流动性 regime（A/HK/US 三市场温度计，市场级条件变量）")
    mc.add_argument("--as-of", dest="as_of", default=None, help="日期 YYYY-MM-DD（默认最新）")
    mc.set_defaults(fn=cmd_macro)

    mt = sub.add_parser("metric", help="指标全生命周期管理（list/show/add/update/remove + test/diff 调试闭环）")
    mt.add_argument("action", choices=["list", "show", "add", "update", "remove",
                                       "test", "diff"])
    mt.add_argument("id", nargs="?", default=None, help="指标 id（list 免）")
    mt.add_argument("--engine", default=None, choices=["alpha", "beta", "macro"], help="引擎归属")
    mt.add_argument("--status", default=None, choices=["active", "inactive", "experimental"],
                    help="状态过滤（list）")
    mt.add_argument("--formula", default=None, help="builtin:<公式id>（add；须在 KNOWN_FORMULAS 清单）")
    mt.add_argument("--dim", default=None, help="维度（alpha 五维 / macro D1..D4）")
    mt.add_argument("--market", default=None, choices=["A", "HK", "US"], help="市场（仅 macro）")
    mt.add_argument("--scorer-type", dest="scorer_type", default=None,
                    choices=["quant", "mom", "step", "clip", "pulse"], help="打分器类型（macro）")
    mt.add_argument("--scorer-arg", dest="scorer_arg", type=float, default=None, help="打分器参数（macro）")
    mt.add_argument("--params-json", dest="params_json", default=None, help="参数 JSON（add）")
    mt.add_argument("--set", dest="set_kv", action="append", default=[], metavar="K=V",
                    help="部分更新（点路径赋值如 params.win=25；值按 JSON 解析；可多次）")
    mt.add_argument("--weights-json", dest="weights_json", default=None, metavar="FILE",
                    help="引擎级权重表 JSON 文件（alpha/macro dim_weights 整表替换）")
    mt.add_argument("--hypothesis-id", dest="hypothesis_id", default=None,
                    help="来源假设 id（add/remove 必填；update 启停必填）")
    mt.add_argument("--reason", default=None, help="变更原因（记入 changelog）")
    mt.add_argument("--json", dest="as_json", action="store_true", help="机器可读输出")
    # T-R8 调试闭环（test/diff 专用；只读引擎只写 artifacts）
    mt.add_argument("--tool", default="walkforward", choices=["walkforward", "sensitivity"],
                    help="test 模式（默认 walkforward 滚动验证）")
    mt.add_argument("--grid", default=None, help="sensitivity 单参数网格：参数名=v1,v2,…（如 win=20,25,30）")
    mt.add_argument("--overrides-json", dest="overrides_json", default=None,
                    help="调试覆盖 JSON（walkforward: 滚动配置如 horizon_days；sensitivity: 策略参数）")
    mt.add_argument("--portfolio", default="cn4", help="sensitivity 回测组合（默认 cn4）")
    mt.add_argument("--baseline", type=int, default=None, help="diff 基线版本号（metrics version）")
    mt.add_argument("--changelog-idx", dest="changelog_idx", type=int, default=None,
                    help="diff 基线：changelog 第 k 条（0 起）")
    mt.add_argument("--ctx", default=None, metavar="SANDBOX_DIR",
                    help="工作区 sandbox 目录（默认沿用当前工作区；web 侧不接）")
    mt.set_defaults(fn=cmd_metric)

    ads = sub.add_parser("altdata-sync", help="二线源：同步申万行业分类变迁史（PIT，消除行业前视偏差）")
    ads.add_argument("--strict-ssl", dest="strict_ssl", action="store_true",
                     help="禁用 TLS 降级（中间人代理环境下会握手失败）")
    ads.set_defaults(fn=cmd_altdata_sync)

    ia = sub.add_parser("industry-asof", help="PIT 行业查询（申万，取 as_of 之前最后一次调整）")
    ia.add_argument("--codes", default=None, help="逗号分隔 secid（默认取全部组合持仓）")
    ia.add_argument("--as-of", dest="as_of", default=None, help="日期 YYYY-MM-DD（默认今天）")
    ia.add_argument("--level", default="l1", choices=["l1", "l2", "l3"], help="申万层级（默认 l1）")
    ia.set_defaults(fn=cmd_industry_asof)

    rs = sub.add_parser("research-source", help="R3 研报来源（东财研报 + 同花顺一致预期）")
    rs.add_argument("--code", required=True, help="标的代码（601058 / 601058.SH）")
    rs.add_argument("--max-pages", dest="max_pages", type=int, default=1, help="东财研报翻页数（默认 1）")
    rs.add_argument("--strict-ssl", dest="strict_ssl", action="store_true")
    rs.set_defaults(fn=cmd_research_source)

    cp = sub.add_parser("checkpoint", help="检查点管理")
    cp.add_argument("action", choices=["ls", "restore"]); cp.add_argument("id", nargs="?")
    cp.set_defaults(fn=cmd_checkpoint)

    sc = sub.add_parser("schedule", help="自动化定制任务（α 打桩）")
    sc.add_argument("action", nargs="?", default="ls", choices=["ls", "add", "rm"])
    sc.set_defaults(fn=cmd_schedule)

    sub.add_parser("serve", help="启动 ap-mcp（stdio，供 IDE 挂载）").set_defaults(fn=cmd_serve)
    sub.add_parser("status", help="本地仓库状态 + 工具面").set_defaults(fn=cmd_status)

    fl = sub.add_parser("fl", help="M5 中长期因子库生命周期（activate/refs/inspect/pool/review"
                                   "/candidate/eval）")
    fl.add_argument("action", help="子动作：activate（draft→active 激活门禁）| refs（被引用关系/影响面）"
                                   "| inspect（季度巡检三指标三态）| pool（合成池快照）"
                                   "| review（factorlib 假设复查必答/抽查）"
                                   "| candidate register（T-F9 personal_clone 候选登记）"
                                   "| eval（T-F9 一键重检验触发 + 扩展市场就绪性披露）")
    fl.add_argument("factor", nargs="?", help="因子 id（activate/refs/eval 必填；inspect 可选，"
                                              "缺省全部 active；candidate 固定为 register）")
    fl.add_argument("--factor", dest="factor_opt", default=None,
                    help="candidate register 的目标因子 id（--factor <factor_id>）")
    fl.add_argument("--why", default="", help="激活原因（记录进状态文件 history）")
    fl.add_argument("--from-strategy", dest="from_strategy", default=None,
                    help="candidate register：来源策略名（克隆自哪个外部策略）")
    fl.add_argument("--thesis", default=None,
                    help="candidate register：拆解依据（策略的因子化逻辑，写成可检验命题）")
    fl.add_argument("--url", default=None, help="candidate register：来源 URL（可选）")
    fl.add_argument("--direction", default="neutral", choices=["positive", "negative", "neutral"],
                    help="candidate register：假设预期方向（缺省 neutral，待检验）")
    fl.add_argument("--markets", default=None,
                    help="eval：逗号分隔市场列表（缺省 CN；如 CN,HK——扩展市场段为就绪性披露）")
    fl.add_argument("--version", default=None, help="refs 显式钉版（缺省=全版本聚合）")
    fl.add_argument("--as-of", default=None, help="inspect 巡检日（缺省今天；序列按 dt ≤ as_of 截断）")
    fl.add_argument("--drill", action="store_true", help="inspect 演练态：不落库不写状态文件")
    fl.add_argument("--all", action="store_true",
                    help="inspect 扩展到全部注册因子（M7 T-F6：draft/active 全含，health 三态如实）")
    fl.add_argument("--inject-decay", default=None,
                    help="inspect 演练：对指定因子 IC 序列尾部 24 月 ×0.3 再判（隐含 --drill）")
    fl.add_argument("--json", action="store_true", help="输出 JSON（门禁明细/巡检全量，调试/CI 用）")
    fl.set_defaults(fn=cmd_fl)

    st = sub.add_parser("strategy", help="M6 策略 DSL：validate（校验/池准入）/ wf（walk-forward+lockbox）/ backtest（全量回测+归档）")
    st.add_argument("action", nargs="?", default="validate", choices=["validate", "wf", "backtest"])
    st.add_argument("file", nargs="?", help="策略 YAML 路径（validate/wf/backtest 必填）")
    st.add_argument("--start", default=None, help="backtest 起始日（YYYY-MM-DD，缺省全样本）")
    st.add_argument("--end", default=None, help="backtest 结束日（YYYY-MM-DD，缺省全样本）")
    st.add_argument("--lockbox-start", default="2022-01",
                    help="lockbox 封存起点月（缺省 2022-01，计划口径「2022–至今」；样本不足时显式推迟）")
    st.add_argument("--sample-start", default=None,
                    help="样本前端截断月（价差类因子历史预热；缺省不截断）")
    st.add_argument("--train-min-months", type=int, default=24, help="首个 train 窗口长度（月），默认 24")
    st.add_argument("--valid-months", type=int, default=6, help="valid 窗口长度（月），默认 6")
    st.add_argument("--step-months", type=int, default=6, help="fold 前滚步长（月），默认 6")
    st.add_argument("--sensitivity", action="store_true",
                    help="附参数 ±30% 敏感性网格（top_n/slippage/method；slippage 轴需 exec=next_day_vwap）")
    st.set_defaults(fn=cmd_strategy)

    args = p.parse_args()
    args.fn(args)


if __name__ == "__main__":
    main()
