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
    ctx = Ctx()
    res = fetch_all(ctx, start=args.start)
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


def _fmt(v, d=3):
    return "—" if v is None else f"{v:+.{d}f}"


def cmd_schedule(args):
    if args.action == "ls":
        print("（α 打桩）自动化定制任务由 IDE agent headless 模式承载：")
        print("  opencode -p \"执行 tasks/review_global.yaml\"   # 单次非交互")
        print("  codex exec \"ap run tasks/review_global.yaml\"  # 单次非交互")
        print("  cron: 30 20 * * 1-5  → ap daemon（V4-RC 实现）")
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

    cp = sub.add_parser("checkpoint", help="检查点管理")
    cp.add_argument("action", choices=["ls", "restore"]); cp.add_argument("id", nargs="?")
    cp.set_defaults(fn=cmd_checkpoint)

    sc = sub.add_parser("schedule", help="自动化定制任务（α 打桩）")
    sc.add_argument("action", nargs="?", default="ls", choices=["ls", "add", "rm"])
    sc.set_defaults(fn=cmd_schedule)

    sub.add_parser("serve", help="启动 ap-mcp（stdio，供 IDE 挂载）").set_defaults(fn=cmd_serve)
    sub.add_parser("status", help="本地仓库状态 + 工具面").set_defaults(fn=cmd_status)

    args = p.parse_args()
    args.fn(args)


if __name__ == "__main__":
    main()
