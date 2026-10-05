"""ap CLI —— 命令分发（V4-α 骨架：auth/clone/run/schedule/push/checkpoint/serve/status）"""
from __future__ import annotations
import argparse, json, sys, time
from pathlib import Path
from .ctx import Ctx
from . import repo as R
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
    """组合回测（等权·月度再平衡 vs 沪深300ETF 买入持有）→ HTML+MD+JSON"""
    ctx = Ctx()
    res = call_tool("backtest.run",
                    {"portfolio": args.portfolio, "start": args.start, "end": args.end, "rf": args.rf}, ctx)
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

    bt = sub.add_parser("backtest", help="组合回测（等权·月度再平衡 vs 沪深300ETF）")
    bt.add_argument("--portfolio", default="multi5")
    bt.add_argument("--start", default=None, help="起始日（默认全样本 2021-01-04）")
    bt.add_argument("--end", default=None, help="结束日（默认最新交易日）")
    bt.add_argument("--rf", type=float, default=0.0, help="无风险利率（年化，默认 0）")
    bt.set_defaults(fn=cmd_backtest)

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
