"""任务执行器：Task YAML → sandbox 内顺序执行 → verify → checkpoint

V4-α 真实数据验证流水线（§15.2：全绿才允许 push）：
  ① pit_audit        财务因子 PIT 红线（used_announce ≤ as_of 且为最新可用报告期）
  ② golden_test      pandas 独立重算引擎 A/B 因子 vs DuckDB 存值（双实现交叉；
                       引擎A对账清单 registry active 驱动，T-R5）
  ③ cross_source     新浪 vs 腾讯 两独立行情源收盘价一致
  ④ calendar_check   K线日期 ⊆ 交易日历，末根K线 = 最近交易日，无缺失
                     （末根豁免分类：腾讯有更新 → 上游发布滞后豁免；腾讯亦无 → 尾部
                       无行情豁免（停牌）；豁免透明计数，单一标的不连坐全市场）
  ⑤ adjust_check     qfq 前复权 vs raw 不复权涨跌幅一致（除权除息日外）
  ⑥ provenance_check 数据集 sha256 与溯源登记一致（clone 完整性）
"""
from __future__ import annotations

import json
import math
import time
from pathlib import Path

import duckdb
import yaml

from .ctx import Ctx, new_run_id, sha256_file
from .tools import call_tool, checkpoint_save


def _alpha_active_ids(ctx: Ctx) -> set[str]:
    """引擎A对账因子清单（T-R5）：registry active 白名单驱动。

    registry 缺失/损坏/版本不符 → load_registry 透明降级为内置默认（16 因子全
    active，对账面与历史一致）；registry 合法且某因子 inactive → 该因子退出对账
    （预期行为：停用即退出评估面，对账面同步收敛）。临时实例加载，不污染单例。
    """
    from .engines import AlphaEngine
    return AlphaEngine().load(ctx.local_repo).active_ids()


def _pit_audit(ctx: Ctx) -> dict:
    con = duckdb.connect(str(ctx.db_path), read_only=True)
    try:
        n1 = con.execute("SELECT COUNT(*) FROM factor_pit_meta WHERE used_announce > dt").fetchone()[0]
        n2 = con.execute("""
            SELECT COUNT(*) FROM factor_pit_meta m
            WHERE m.used_period <> (
                SELECT r.period FROM dwd_financial_report r
                WHERE r.secid = m.secid AND r.announce_date <= m.dt
                ORDER BY r.period DESC, r.announce_date DESC LIMIT 1)""").fetchone()[0]
    finally:
        con.close()
    ok = n1 == 0 and n2 == 0
    return {"pit_audit": "pass" if ok else f"FAIL(红线{n1}·非最新{n2})"}


def _golden_test(ctx: Ctx) -> dict:
    """pandas 独立实现重算（与 factors.py 循环实现为不同代码路径），对账 DuckDB 存值"""
    import pandas as pd
    bars = json.loads((ctx.local_repo / "market" / "daily_bars.json").read_text(encoding="utf-8"))
    fund = json.loads((ctx.local_repo / "factors" / "fundamental_values.json").read_text(encoding="utf-8"))
    con = duckdb.connect(str(ctx.db_path), read_only=True)
    try:
        dbv = {(r[1], r[2]): r[3] for r in con.execute(
            "SELECT dt, secid, factor_id, value FROM factor_value_daily").fetchall()}
    finally:
        con.close()

    bad, checked = [], 0
    active_a = _alpha_active_ids(ctx)          # T-R5：引擎A对账清单 registry 驱动

    def cmp(secid, fid, mine):
        nonlocal checked
        key = (secid, fid)
        if key not in dbv:
            bad.append(f"{secid}.{fid} 缺失")
            return
        if not math.isclose(mine, dbv[key], rel_tol=1e-9, abs_tol=1e-12):
            bad.append(f"{secid}.{fid} {mine:.6g}≠{dbv[key]:.6g}")
        checked += 1

    def cmp_a(secid, fid, mine):
        """引擎A因子对账：registry inactive 因子跳过（停用即退出评估面，对账面同步收敛）"""
        if fid in active_a:
            cmp(secid, fid, mine)

    for secid, arr in bars.items():
        df = pd.DataFrame(arr).dropna(subset=["close"]).reset_index(drop=True)
        c = df["close"]
        # 引擎 B（pandas rolling / pct_change）
        ma20 = c.rolling(20).mean().iloc[-1]
        cmp(secid, "ma20_break", c.iloc[-1] / ma20 - 1)
        cmp(secid, "mom_20d", c.iloc[-1] / c.iloc[-20] - 1)
        cmp(secid, "vol_20d", c.pct_change().iloc[-20:].std(ddof=1) * math.sqrt(252))
        cmp(secid, "high20_touch", 1.0 if c.iloc[-1] >= c.iloc[-20:].max() else 0.0)
        cmp(secid, "dist_high20", c.iloc[-1] / df["high"].iloc[-20:].max() - 1)
        # 引擎 A（逐报告期字典法独立重算：TTM=上年FY+本年YTD−上年同期YTD）
        # 基金/指数类不在 fundamental_values 中（无财报）→ 空报告集，仅价格类因子对账
        # 清单 registry 驱动（T-R5）：inactive 因子跳过对账（预期行为，非缺失）
        fd = fund.get(secid, {})
        reps = sorted(fd.get("reports", []), key=lambda r: r["period"])
        by_p = {r["period"]: r for r in reps}

        def ttm_at(t, reps=reps, by_p=by_p):
            av = [r for r in reps if r["announce_date"] <= t]
            if not av:
                return None
            R = max(av, key=lambda r: r["period"])
            if "eps_ttm" in R:      # 港股/美股：上游直出 TTM（与 factors.ttm_eps 同口径）
                return R["eps_ttm"]
            if R.get("eps_ytd") is None:
                return None
            y, md = R["period"][:4], R["period"][5:]
            if md == "12-31":
                return R["eps_ytd"]
            py = int(y) - 1
            fy, pv = by_p.get(f"{py}-12-31"), by_p.get(f"{py}-{md}")
            ok = fy and pv and fy.get("eps_ytd") is not None and pv.get("eps_ytd") is not None \
                and fy["announce_date"] <= t and pv["announce_date"] <= t
            return fy["eps_ytd"] + R["eps_ytd"] - pv["eps_ytd"] if ok else None

        t = df["dt"].iloc[-1]
        e = ttm_at(t)
        if e and e > 0:
            cmp_a(secid, "val_pe_ttm", c.iloc[-1] / e)
        R_now = max((r for r in reps if r["announce_date"] <= t), key=lambda r: r["period"], default=None)
        if R_now and R_now.get("bps"):
            cmp_a(secid, "val_pb", c.iloc[-1] / R_now["bps"])
        if R_now and R_now.get("rev_g") is not None:
            cmp_a(secid, "grow_rev_yoy", R_now["rev_g"])
        if R_now and R_now.get("np_g") is not None:
            cmp_a(secid, "grow_np_yoy", R_now["np_g"])
        if R_now and R_now.get("margin") is not None:
            cmp_a(secid, "qua_margin", R_now["margin"])
        if R_now and R_now.get("bps"):
            ee = ttm_at(t)
            if ee:
                cmp_a(secid, "qua_roe_ttm", ee / R_now["bps"] * 100)
        cmp_a(secid, "mom_12_1", c.iloc[-21] / c.iloc[-252] - 1)
        cmp_a(secid, "vol_250", c.pct_change().iloc[-250:].std(ddof=1) * math.sqrt(252))
        dvd = fd.get("dividends", [])
        lo = (pd.Timestamp(t) - pd.Timedelta(days=365)).strftime("%Y-%m-%d")
        s = sum(d["dps"] for d in dvd if lo <= d["ex_date"] <= t)
        if s:
            cmp_a(secid, "div_yield_ttm", s / c.iloc[-1])
        # PE/PB 五年分位（独立窗口重算；P0-4 补齐 val_pe_pct/val_pb_pct）
        dts = pd.to_datetime(df["dt"])
        pe_series, pb_series = [], []
        for i in range(len(df)):
            ds = dts.iloc[i].strftime("%Y-%m-%d")
            ee = ttm_at(ds)
            if ee and ee > 0:
                pe_series.append((str(dts.iloc[i])[:10], c.iloc[i] / ee))
            av = [r for r in reps if r["announce_date"] <= ds]
            if av:
                Rb = max(av, key=lambda r: r["period"])
                if Rb.get("bps") and Rb["bps"] > 0:
                    pb_series.append((str(dts.iloc[i])[:10], c.iloc[i] / Rb["bps"]))
        w5 = (pd.Timestamp(t) - pd.Timedelta(days=365 * 5)).strftime("%Y-%m-%d")
        if e and e > 0:
            vals = [v for d, v in pe_series if d >= w5]
            cur = c.iloc[-1] / e
            if vals:
                cmp_a(secid, "val_pe_pct", (sum(1 for v in vals if v < cur) + 0.5 * sum(1 for v in vals if v == cur)) / len(vals))
        if R_now and R_now.get("bps") and R_now["bps"] > 0:
            pb_vals = [v for d, v in pb_series if d >= w5]
            cur_pb = c.iloc[-1] / R_now["bps"]
            if pb_vals:
                cmp_a(secid, "val_pb_pct", (sum(1 for v in pb_vals if v < cur_pb) + 0.5 * sum(1 for v in pb_vals if v == cur_pb)) / len(pb_vals))
    return {"golden_test": f"pass({checked} 项对账)" if not bad else f"FAIL({'; '.join(bad[:5])})"}


def _cross_source(ctx: Ctx) -> dict:
    raws = json.loads((ctx.local_repo / "market" / "daily_bars_raw.json").read_text(encoding="utf-8"))
    bars = json.loads((ctx.local_repo / "market" / "daily_bars.json").read_text(encoding="utf-8"))
    txs = json.loads((ctx.local_repo / "market" / "cross_check_tx.json").read_text(encoding="utf-8"))
    bad, n = [], 0
    for secid, arr in txs.items():
        # 股票：raw 不复权对照腾讯；基金（ETF/LOF）：无 raw 文件，回退主序列（新浪不复权）
        src = raws.get(secid) or bars.get(secid)
        if src is None:
            bad.append(f"{secid} 无主序列")
            continue
        raw = {r["dt"]: r["close"] for r in src}
        common = [(r["dt"], r["close"]) for r in arr[-10:] if r["dt"] in raw]
        for d, tc in common:
            n += 1
            if abs(raw[d] - tc) > 0.011:
                bad.append(f"{secid}@{d} 新浪{raw[d]} vs 腾讯{tc}")
    return {"cross_source": f"pass({n} 点对账)" if not bad else f"FAIL({'; '.join(bad[:4])})"}


def _calendar_check(ctx: Ctx) -> dict:
    bars = json.loads((ctx.local_repo / "market" / "daily_bars.json").read_text(encoding="utf-8"))
    cal = json.loads((ctx.local_repo / "market" / "trade_calendar.json").read_text(encoding="utf-8"))
    txs = json.loads((ctx.local_repo / "market" / "cross_check_tx.json").read_text(encoding="utf-8"))
    today = time.strftime("%Y-%m-%d")

    # V4-β 多市场：按 secid 后缀分组，各市场用自己的日历校验（.HK/.US 日历由 K 线日期并集
    # 推导，见 datasource.fetch_all ②''；无对应日历文件的市场回退 A 股日历，保持旧行为）
    from applib.datasource import classify_secid
    cal_by_mkt = {"A": cal}
    for mkt in ("HK", "US"):
        p = ctx.local_repo / "market" / f"trade_calendar_{mkt.lower()}.json"
        if p.exists():
            try:
                cal_by_mkt[mkt] = json.loads(p.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                pass

    bad = []
    last_by_secid: dict[str, str | None] = {}
    susp_n = 0
    for secid, arr in bars.items():
        mkt = classify_secid(secid) or "A"
        mcal = cal_by_mkt.get(mkt) or cal
        calset = set(mcal)
        dts = [r["dt"] for r in arr]
        if len(dts) != len(set(dts)):
            bad.append(f"{secid} 日期重复")
        off = [d for d in dts if d not in calset]
        if off:
            bad.append(f"{secid} 非交易日K线{off[:3]}")
        missing = [d for d in mcal if dts[0] < d < dts[-1] and d not in set(dts)]
        if missing:
            # 缺失分类：腾讯（独立源）同日有行情 → 新浪真缺失（FAIL）；
            # 腾讯亦无/窗口外 → 该品种当日无行情（停牌/份额折算），豁免并透明计数
            tx_dts = {r["dt"] for r in txs.get(secid, [])}
            real_missing = [d for d in missing if d in tx_dts]
            if real_missing:
                bad.append(f"{secid} 缺交易日{len(real_missing)}根（腾讯有而新浪缺）{real_missing[:3]}")
            susp_n += len(missing) - len(real_missing)
        last_by_secid[secid] = dts[-1] if dts else None
    # 末根校验（T-fix 尾部豁免）：末根 < 本市场最近交易日的标的先按腾讯交叉源分类——
    # 腾讯有更新行情且仅落后 1 个交易日 → 上游发布滞后（新浪港股个别标的收盘后延迟
    # 发布，实测可达数小时），豁免并透明登记；腾讯亦无更新 → 尾部无行情（停牌），豁免
    # 计数；落后 ≥2 个交易日且腾讯有更新 → 真缺失（持续上游故障），不豁免。
    # 末根一致性仅对非豁免标的校验，单一标的滞后不再连坐全市场。
    _sorted_cal = {m: sorted(d for d in (cal_by_mkt.get(m) or cal) if d <= today)
                   for m in {classify_secid(s) or "A" for s in bars}}
    last_td_by_mkt = {m: (c[-1] if c else None) for m, c in _sorted_cal.items()}
    prev_td_by_mkt = {m: (c[-2] if len(c) >= 2 else None) for m, c in _sorted_cal.items()}
    lag_exempt: list[str] = []
    susp_tail_n = 0
    lasts_by_mkt: dict[str, set] = {}
    for secid, last in last_by_secid.items():
        mkt = classify_secid(secid) or "A"
        last_td = last_td_by_mkt.get(mkt)
        if last and last_td and last < last_td:
            tx_last = max((r["dt"] for r in txs.get(secid, [])), default=None)
            prev_td = prev_td_by_mkt.get(mkt)
            if tx_last and tx_last > last and (not prev_td or last >= prev_td):
                lag_exempt.append(f"{secid}@{last}")
            elif not tx_last or tx_last <= last:
                susp_tail_n += 1
            else:
                lasts_by_mkt.setdefault(mkt, set()).add(last)   # 真缺失：不豁免
        else:
            lasts_by_mkt.setdefault(mkt, set()).add(last)
    for mkt, lasts in lasts_by_mkt.items():
        last_td = last_td_by_mkt.get(mkt)
        if lasts != {last_td}:
            bad.append(f"{mkt} 末根K线{sorted(lasts)} ≠ 最近交易日{last_td}")
    susp_note = f"·无行情豁免{susp_n}日(停牌/份额折算)" if susp_n else ""
    lag_note = f"·发布滞后豁免{len(lag_exempt)}只({','.join(lag_exempt[:3])})" if lag_exempt else ""
    susp_tail_note = f"·尾部无行情豁免{susp_tail_n}只" if susp_tail_n else ""
    cal_total = sum(len(cal_by_mkt.get(m) or cal) for m in lasts_by_mkt)
    mkt_note = "·" + "+".join(f"{m}:{len(c)}日" for m, c in sorted(cal_by_mkt.items()))
    last_note = "{" + ", ".join(
        f"{m}:{max(x for x in v if x)}" for m, v in sorted(lasts_by_mkt.items())) + "}"
    msg = f"pass(末根={last_note}·日历{cal_total}日{mkt_note}{susp_note}{lag_note}{susp_tail_note})" if not bad \
        else f"FAIL({'; '.join(bad[:4])})"
    return {"calendar_check": msg}


def _adjust_check(ctx: Ctx) -> dict:
    bars = json.loads((ctx.local_repo / "market" / "daily_bars.json").read_text(encoding="utf-8"))
    raws = json.loads((ctx.local_repo / "market" / "daily_bars_raw.json").read_text(encoding="utf-8"))
    fund = json.loads((ctx.local_repo / "factors" / "fundamental_values.json").read_text(encoding="utf-8"))
    # V4-β：港股/美股除权除息日（腾讯事件元数据，见 datasource ②''/②'''）；
    # 港股主序列为新浪 qfq，除息日豁免必需；美股主序列为腾讯 raw 口径（与新浪 raw
    # 日涨跌幅全窗口一致），豁免名单仅作元数据登记、不影响对账结果。
    ex_path = ctx.local_repo / "market" / "adjust_ex_dates.json"
    hkus_ex = json.loads(ex_path.read_text(encoding="utf-8")) if ex_path.exists() else {}
    from applib.datasource import classify_secid
    bad, max_diff, n, fund_n, hkus_n, glitch_n = [], 0.0, 0, 0, 0, 0
    for secid, arr in bars.items():
        if secid not in raws:
            fund_n += 1     # ETF/LOF：sina 无复权接口，无 qfq/raw 对（价格真实性由 cross_source 双源覆盖）
            continue
        mkt = classify_secid(secid)
        if mkt == "HK" and not hkus_ex.get(secid):
            # 港股除息日元数据缺失（腾讯上游故障）→ 豁免并透明计数，价格真实性由 cross_source 兜底
            fund_n += 1
            continue
        if mkt in ("HK", "US"):
            hkus_n += 1
        raw = {r["dt"]: r["close"] for r in raws[secid]}
        ex = ({d["ex_date"] for d in fund.get(secid, {}).get("dividends", [])
               if d["ex_date"] >= arr[0]["dt"]}
              | {d for d in hkus_ex.get(secid, []) if d >= arr[0]["dt"]})
        prev_dt = None
        glitch_prev = False  # 前一共同交易日为双源毛刺日（价差 >1¢）→ 当日点是其回声
        for i, r in enumerate(arr):
            # 双源当日价差 ≤1¢（与末日锚定同常数）视为一致；>1¢ 为毛刺日
            agree = r["dt"] in raw and abs(r["close"] - raw[r["dt"]]) <= 0.011
            if prev_dt and r["dt"] not in ex and r.get("pct") is not None and prev_dt in raw and r["dt"] in raw:
                rp = raw[r["dt"]] / raw[prev_dt] - 1
                d = abs(r["pct"] - rp)
                # V4-β 价格感知容差：双源收盘价一个最小报价单位的舍入差（≤1¢）在低价区
                # 会放大成大 pct 偏差（DAVE.US 2022 年 <$1 区间半美分差 → 0.5–0.9%），
                # 固定 0.5% 容差失真 → 容差取 max(0.5%, 0.011/前收)。
                tol = max(0.005, 0.011 / raw[prev_dt])
                exempt = False
                if d > tol:
                    # 双源孤立毛刺豁免（仅越界点启用）：当日价差 >1¢ 且下一共同交易日
                    # 收敛 ≤1¢ → 单源单日数据毛刺（复权因子错误会持续发散，不会自愈）；
                    # 毛刺日的次日点因 rp 用了错误前收而越界（回声），一并豁免。
                    nxt = next((x for x in arr[i + 1:] if x["dt"] in raw), None)
                    recon = nxt is not None and abs(nxt["close"] - raw[nxt["dt"]]) <= 0.011
                    exempt = (not agree and recon) or (glitch_prev and agree)
                if exempt:
                    glitch_n += 1  # 移出对账总体，透明计数
                else:
                    n += 1
                    max_diff = max(max_diff, d)
                    if d > tol:
                        bad.append(f"{secid}@{r['dt']} qfq{r['pct']:.4f} vs raw{rp:.4f}")
            if r["dt"] in raw:
                prev_dt = r["dt"]
                glitch_prev = not agree
        # 末日锚定：主序列末根在 raw 有同日数据时须相等（qfq 前复权锚定当下；美股腾讯
        # 序列含当日盘中K线而新浪 raw 收盘后更新 → 取最后共同交易日比较）
        common = [r["dt"] for r in arr if r["dt"] in raw]
        if common:
            last_dt = common[-1]
            last_close = next(r["close"] for r in arr if r["dt"] == last_dt)
            if abs(last_close - raw[last_dt]) > 0.011:
                bad.append(f"{secid} 末日({last_dt}) qfq {last_close} ≠ raw {raw[last_dt]}（复权因子≠1）")
    fund_note = f"·无复权对豁免{fund_n}只" if fund_n else ""
    hkus_note = f"·港美对账{hkus_n}只" if hkus_n else ""
    glitch_note = f"·毛刺豁免{glitch_n}点" if glitch_n else ""
    return {"adjust_check": f"pass({n} 点·最大偏差{max_diff:.4%}{hkus_note}{fund_note}{glitch_note})" if not bad else f"FAIL({'; '.join(bad[:4])})"}


def _provenance_check(ctx: Ctx) -> dict:
    prov = json.loads((ctx.local_repo / "market" / "provenance.json").read_text(encoding="utf-8"))
    bad = []
    for s in prov["sources"]:
        p = ctx.local_repo / s["file"]
        if not p.exists():
            bad.append(f"{s['file']} 缺失")
        elif sha256_file(p) != s["sha256"]:
            bad.append(f"{s['file']} 哈希不符")
    return {"provenance_check": f"pass({len(prov['sources'])} 数据集)" if not bad else f"FAIL({'; '.join(bad)})"}


VERIFY_SUITE = [_pit_audit, _golden_test, _cross_source, _calendar_check, _adjust_check, _provenance_check]


def run_task(task_path: Path, ctx: Ctx, as_of: str | None = None) -> dict:
    t0 = time.time()
    task = yaml.safe_load(task_path.read_text(encoding="utf-8"))
    run_id = new_run_id()
    ctx.ensure_sandbox()
    log = []

    print(f"▶ run {run_id} · task={task.get('id')} · runtime={task.get('runtime', {}).get('sandbox', 'quant-std')}")
    steps = list(task.get("steps", []))
    # report.generate 延后至验证之后执行：报告需嵌入六项验证结果
    deferred = []
    if steps and steps[-1]["tool"] == "report.generate":
        deferred = [steps.pop()]
    for i, step in enumerate(steps, 1):
        name, args = step["tool"], step.get("args", {})
        print(f"  [{i}/{len(steps) + len(deferred)}] {name} {json.dumps(args, ensure_ascii=False) if args else ''}")
        res = call_tool(name, args, ctx, run={"run_id": run_id})
        log.append({"step": i, "tool": name, "ok": "error" not in res,
                    "summary": {k: v for k, v in res.items() if not isinstance(v, (list, dict)) or k in ("synced",)}})
        if "error" in res:
            print(f"    ✗ {res['error']}（中止）")
            return {"run_id": run_id, "status": "failed", "log": log}

    # ── 本地验证流水线（§15.2：全绿才允许 push）──
    verify = {}
    for fn in VERIFY_SUITE:
        verify.update(fn(ctx))
    ok = all(v == "pass" or v.startswith("pass(") for v in verify.values())
    print(f"  verify: {' · '.join(f'{k}={v}' for k, v in verify.items())}")
    print(f"  {'✓ 全绿（允许 push）' if ok else '✗ 禁止 push'}")

    # ── 报告生成（含验证结果）──
    for j, step in enumerate(deferred, len(steps) + 1):
        name, args = step["tool"], step.get("args", {})
        print(f"  [{j}/{len(steps) + len(deferred)}] {name} {json.dumps(args, ensure_ascii=False) if args else ''}")
        res = call_tool(name, args, ctx, run={"run_id": run_id, "verify": verify})
        log.append({"step": j, "tool": name, "ok": "error" not in res, "summary": res})
        if "error" in res:
            print(f"    ✗ {res['error']}（中止）")
            return {"run_id": run_id, "status": "failed", "log": log}

    # 数据集指纹 = 溯源文件哈希
    ds_commit = ("ds@" + sha256_file(ctx.local_repo / "market" / "provenance.json")[:12]) \
        if (ctx.local_repo / "market" / "provenance.json").exists() else "ds@unknown"
    reg = {"factor_versions": "见 registry", "n_factors": 16}
    art = ctx.artifacts_dir / run_id
    manifest = {
        "task": task.get("id"), "status": "succeeded" if ok else "verify_failed",
        "as_of": as_of or "auto", "code_version": ctx.code_version,
        "factor_versions": reg, "strategy_version": "cn4_quality_v1",
        "portfolio_ref": f"cn4@{time.strftime('%Y-%m-%d')}", "dataset_commits": ds_commit,
        "verify": verify, "data_source": "AKShare（新浪行情/财务/分红 + 腾讯交叉源 + 交易日历）",
        "artifacts": [str(p) for p in sorted(art.glob("*"))] if art.exists() else [],
        "elapsed_s": round(time.time() - t0, 2), "steps": len(task.get("steps", [])),
    }
    (ctx.sandbox_dir / "logs" / f"{run_id}.json").write_text(
        json.dumps({"run_id": run_id, "log": log, "manifest": manifest}, ensure_ascii=False, indent=2, default=str),
        encoding="utf-8")
    cp_id = checkpoint_save(ctx, run_id, manifest)
    print(f"  checkpoint: {cp_id} · artifacts → {art}")
    return {"run_id": run_id, "status": manifest["status"], "checkpoint": cp_id,
            "verify": verify, "manifest": manifest}
