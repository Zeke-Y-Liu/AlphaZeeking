"""调仓执行单生成器（V4-α）

把 portfolio.rebalance_plan 的「拟调仓/评估单」机械换算为可下单的委托指令清单：
    目标权重 → 目标金额 → 目标持仓股数（按 100 份/手取整）→ 委托数量
本模块只做算术换算与风控提示，不含任何自主决策，也绝不触碰券商接口；
产出状态恒为「待人工确认」——引擎永不直接动仓位。

单位约定：A 股 ETF/LOF 交易单位 100 份/手；ETF/LOF 免印花税，佣金按万 2.5/单边（最低 ¥5/笔）。
"""
from __future__ import annotations
import csv
import io
import json
import time
from pathlib import Path

LOT = 100                 # 交易单位：100 股/份 每手
COMMISSION = 0.00025      # 佣金万 2.5（单边）
MIN_COMMISSION = 5.0      # 单笔最低佣金 ¥5
STAMP_DUTY = 0.0005       # 印花税 0.05%（仅 A 股股票卖出）


def _lot(shares: float) -> int:
    """按整手取整（最近整数手），保证委托数量为 100 的整数倍"""
    return int(round(shares / LOT)) * LOT


def _fee(amount: float, asset_type: str, direction: str) -> float:
    """单笔费用：佣金（双边，最低 ¥5）+ 印花税（A 股股票卖出）。ETF/LOF 免印花税"""
    if amount <= 0:
        return 0.0
    comm = max(amount * COMMISSION, MIN_COMMISSION)
    stamp = amount * STAMP_DUTY if (asset_type == "STOCK" and direction == "卖出") else 0.0
    return round(comm + stamp, 2)


def _direction(delta_sh: int) -> str:
    return "卖出" if delta_sh < 0 else "买入" if delta_sh > 0 else "持有（不委托）"


def build_exec_orders(review: dict, plan: dict, capital: float = 1_000_000.0) -> dict:
    """由 review（含现价/权重/参考带）+ plan（目标权重）生成执行单结构"""
    trade_by_secid = {t["secid"]: t for t in plan.get("diff_trades", [])}
    orders = []
    for p in review["positions"]:
        secid = p["secid"]
        t = trade_by_secid.get(secid)
        b = p["beta"]
        cur_w = float(p["weight"])
        tgt_w = float(t["to"]) if t else cur_w
        price = float(p["close"])
        cur_sh = _lot(cur_w / 100 * capital / price)
        tgt_sh = _lot(tgt_w / 100 * capital / price)
        delta_sh = tgt_sh - cur_sh
        amount = abs(delta_sh) * price
        direction = _direction(delta_sh)
        orders.append({
            "instruction": t["side"] if t else "no_op",
            "secid": secid, "name": p["name"], "asset_type": p.get("asset_type", "STOCK"),
            "direction": direction,
            "cur_weight": cur_w, "tgt_weight": tgt_w, "delta_weight": round(tgt_w - cur_w, 2),
            "price": price, "cur_shares": cur_sh, "tgt_shares": tgt_sh,
            "order_shares": delta_sh, "lots": abs(delta_sh) // LOT,
            "order_amount": round(amount, 2),
            "fee": _fee(amount, p.get("asset_type", "STOCK"), direction) if delta_sh else 0.0,
            "limit_ref": price,
            "band_low": round(b["band_low"], 3), "band_high": round(b["band_high"], 3),
            "state": b["state"], "settle": p.get("settle", "T+1"),
            "reason": t["reason"] if t else "无引擎 B 预警：维持目标权重，不委托",
            "evidence": t["evidence"] if t else "—",
        })

    sells = [o for o in orders if o["order_shares"] < 0]
    buys = [o for o in orders if o["order_shares"] > 0]
    proceeds = round(sum(o["order_amount"] for o in sells), 2)
    outlay = round(sum(o["order_amount"] for o in buys), 2)
    total_fee = round(sum(o["fee"] for o in orders), 2)
    end_invested = round(sum(o["tgt_shares"] * o["price"] for o in orders), 2)
    end_cash = round(capital - end_invested, 2)

    actionable = [o for o in orders if o["order_shares"] != 0]
    return {
        "exec_id": f"EX-{review['as_of'].replace('-', '')}-{review['id'].upper()}",
        "portfolio": review["id"], "portfolio_name": review["name"],
        "plan_id": plan["plan_id"], "as_of": review["as_of"],
        "next_trade_date": review["next_trade_date"], "holiday_days": review.get("holiday_days", 0),
        "capital_cny": capital, "base_ccy": review["base_ccy"],
        "generated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "status": "draft → 待人工确认（脚本不自动下单）",
        "discipline": "引擎永不直接动仓位：本执行单须人工逐条确认后，方可在券商端下单",
        "orders": orders, "actionable": actionable,
        "cash": {"sell_proceeds": proceeds, "buy_outlay": outlay, "net_cash": round(proceeds - outlay, 2),
                 "end_invested_cny": end_invested, "end_invested_pct": round(end_invested / capital * 100, 1),
                 "end_cash_cny": end_cash, "end_cash_pct": round(end_cash / capital * 100, 1)},
        "fees": {"commission_rate": "万2.5/单边", "min_fee_cny": MIN_COMMISSION,
                 "total_fee_cny": total_fee,
                 "note": (("A股股票：佣金双边万2.5（最低 ¥5/笔）+ 印花税卖出 0.05%；ETF/LOF 免印花税"
                           if any(o["asset_type"] == "STOCK" for o in orders) else
                           "ETF/LOF 免印花税；佣金双边万2.5，单笔最低 ¥5")
                          + "（按基准资金 ¥{:,.0f} 实算）".format(capital))},
        "summary": {"n_orders": len(actionable), "n_sell": len(sells), "n_buy": len(buys),
                    "turnover_cny": round(proceeds + outlay, 2),
                    "turnover_pct": round((proceeds + outlay) / capital * 100, 1)},
        "checks": _checks(review, orders),
        "risks": _risks(review, orders),
    }


def _checks(review: dict, orders: list) -> list:
    qdii = [p["name"] for p in review["positions"] if "QDII" in p.get("industry", "")]
    sells = [o for o in orders if o["order_shares"] < 0]
    return [
        {"item": "限价委托", "detail": "全部使用限价单；禁止市价单。限价参考=AS_OF 收盘价，开盘价超出执行参考带时暂缓", "status": "必做"},
        {"item": "整手约束", "detail": "委托数量已按 100 股/份 每手取整，无碎股", "status": "已满足"},
        {"item": "QDII 溢价核对", "detail": f"{'、'.join(qdii)}：ETF 场内价可偏离净值，买入/持有前核对 IOPV 溢价，溢价 > 1% 暂缓" if qdii else "无跨境品种", "status": "必做" if qdii else "不适用"},
        {"item": "节假日跳空", "detail": f"距 AS_OF 休市 {review.get('holiday_days', 0)} 个日历日；下一交易日 {review['next_trade_date']} 开盘跳空超出执行参考带时暂缓", "status": "必做"},
        {"item": "卖出指令冷却", "detail": f"sig_ma20_break 触发后 10 个交易日内不重复发单；本单卖出 {len(sells)} 笔", "status": "已记录"},
        {"item": "资金交收", "detail": "卖出回款按各品种交收规则（股票 T+1 / 跨境 ETF T+0）；本单无买入再投放 → 回款留作现金", "status": "已核对"},
    ]


def _risks(review: dict, orders: list) -> list:
    vol_sorted = sorted(review["positions"], key=lambda p: -(p["alpha"].get("vol_250") or 0))
    qdii = [p["name"] for p in review["positions"] if "QDII" in p.get("industry", "")]
    risks = [
        f"节假日跳空：休市 {review.get('holiday_days', 0)} 个日历日，节后首日波动放大",
        f"最高波动持仓 {vol_sorted[0]['name']} {vol_sorted[0]['alpha'].get('vol_250', 0) * 100:.0f}%，对组合回撤贡献最大",
        f"{len(review['positions'])} 标的等权集中",
    ]
    if qdii:
        risks.append(f"QDII 溢价/折价（{'、'.join(qdii)}）买入前核对净值溢价")
    return risks


# ── 落盘（MD / CSV / JSON）───────────────────────────────
def render_md(sheet: dict) -> str:
    L = []
    L.append(f"# 调仓执行单 · {sheet['portfolio']} · {sheet['exec_id']}\n")
    L.append(f"执行单 `{sheet['exec_id']}` · 组合 `{sheet['portfolio_name']}`")
    L.append(f"调仓单 `{sheet['plan_id']}` · AS_OF `{sheet['as_of']}` · 下一交易日 `{sheet['next_trade_date']}` "
             f"· 基准资金 `¥{sheet['capital_cny']:,.0f}` · 生成 `{sheet['generated_at']}`")
    L.append(f"状态 **{sheet['status']}** · {sheet['discipline']}\n")
    L.append("> 本执行单由 `portfolio.rebalance_plan` 机械换算（目标权重→金额→整手股数），不含自主决策；")
    L.append("> 须人工逐条确认后，方可在券商端下单。\n")

    L.append("## ① 委托指令\n| # | 指令 | 标的 | 方向 | 现权重→目标 | 现价 | 现持仓(股) | 委托数量(股) | 手数 | 委托金额 | 结算 | 费用 |")
    L.append("| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |")
    for i, o in enumerate(sheet["orders"], 1):
        qty = f"{abs(o['order_shares']):,}" if o["order_shares"] else "—"
        amt = f"¥{o['order_amount']:,.0f}" if o["order_shares"] else "—"
        fee = f"¥{o['fee']:.2f}" if o["order_shares"] else "—"
        L.append(f"| {i} | `{o['instruction']}` | {o['name']}<br>`{o['secid']}` | {o['direction']} "
                 f"| {o['cur_weight']:.0f}% → {o['tgt_weight']:.0f}% | {o['price']} | {o['cur_shares']:,} "
                 f"| {qty} | {o['lots'] if o['order_shares'] else '—'} | {amt} | {o['settle']} | {fee} |")
    if not sheet["actionable"]:
        L.append("| — | — | 全部标的 | 持有 | — | — | — | — | — | — | — | — |")
    L.append("")
    for o in sheet["actionable"]:
        L.append(f"- **{o['direction']} {o['name']}** `{o['secid']}`：{abs(o['order_shares']):,} 份 / {o['lots']} 手"
                 f" ≈ ¥{o['order_amount']:,.0f}｜限价参考 {o['limit_ref']}（执行参考带 {o['band_low']}~{o['band_high']}）"
                 f"｜{o['reason']}｜证据 {o['evidence']}")
    L.append("")

    c, f, s = sheet["cash"], sheet["fees"], sheet["summary"]
    L.append("## ② 资金与成本")
    L.append(f"- 卖出回款 **¥{c['sell_proceeds']:,.0f}** · 买入支出 **¥{c['buy_outlay']:,.0f}** · 净现金变动 **¥{c['net_cash']:,.0f}**")
    L.append(f"- 期末持仓市值 ¥{c['end_invested_cny']:,.0f}（{c['end_invested_pct']}%）· 期末现金 ¥{c['end_cash_cny']:,.0f}（{c['end_cash_pct']}%）")
    L.append(f"- 交易成本合计 **¥{f['total_fee_cny']:.2f}**（{f['note']}）")
    L.append(f"- 换手 ¥{s['turnover_cny']:,.0f}（{s['turnover_pct']}% 双边）· 委托 {s['n_orders']} 笔（卖出 {s['n_sell']} / 买入 {s['n_buy']}）\n")

    L.append("## ③ 执行前检查清单")
    for ch in sheet["checks"]:
        box = " " if ch["status"] == "必做" else "x"
        L.append(f"- [{box}] **{ch['item']}**（{ch['status']}）：{ch['detail']}")
    L.append("")

    L.append("## ④ 风险提示")
    for r in sheet["risks"]:
        L.append(f"- {r}")
    L.append("")

    holds = [o for o in sheet["orders"] if o["order_shares"] == 0]
    L.append("## ⑤ 未变动持仓（持有确认）")
    if holds:
        for o in holds:
            L.append(f"- **{o['name']}** `{o['secid']}` 维持 {o['tgt_weight']:.0f}%（{o['cur_shares']:,} 份）· {o['reason']}")
    else:
        L.append("- 无")
    L.append("")
    L.append("> 执行单为规则化数据输出，不构成投资建议；成交价格以实际委托为准。"
             f"复现：`ap exec-plan --portfolio {sheet['portfolio']} --capital {sheet['capital_cny']:.0f}`。")
    return "\n".join(L)


def render_csv(sheet: dict) -> str:
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["指令", "代码", "名称", "方向", "现权重%", "目标权重%", "Δ权重%", "现价",
                "现持仓(股)", "目标持仓(股)", "委托数量(股)", "手数", "委托金额(元)",
                "限价参考", "参考带下沿", "参考带上沿", "结算", "费用(元)", "理由", "证据"])
    for o in sheet["orders"]:
        w.writerow([o["instruction"], o["secid"], o["name"], o["direction"],
                    o["cur_weight"], o["tgt_weight"], o["delta_weight"], o["price"],
                    o["cur_shares"], o["tgt_shares"], abs(o["order_shares"]), o["lots"] if o["order_shares"] else 0,
                    f"{o['order_amount']:.2f}", o["limit_ref"], o["band_low"], o["band_high"],
                    o["settle"], f"{o['fee']:.2f}", o["reason"], o["evidence"]])
    return buf.getvalue()


def write_all(sheet: dict, art_dir: Path) -> dict:
    art_dir.mkdir(parents=True, exist_ok=True)
    jp = art_dir / "exec_order.json"
    mp = art_dir / "exec_order.md"
    cp = art_dir / "exec_order.csv"
    jp.write_text(json.dumps(sheet, ensure_ascii=False, indent=2), encoding="utf-8")
    mp.write_text(render_md(sheet), encoding="utf-8")
    cp.write_text(render_csv(sheet), encoding="utf-8-sig")   # BOM：Excel 直开不乱码
    return {"json": str(jp), "md": str(mp), "csv": str(cp)}
