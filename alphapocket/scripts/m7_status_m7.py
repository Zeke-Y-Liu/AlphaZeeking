"""M7 T-F6：25 项状态总表 + active_pool 容量核对（只读汇总，禁止重算治理结论）。

前置：``ap fl inspect --all``（M7 小改：draft/active 全含巡检，落 fl_factor_health +
状态文件同步）与 ``ap fl pool``（池快照）已执行。
动作（对落库表与状态文件只读）：
  1) 总表逐项登记：{factor_id, family, origin, lifecycle, health, eval, gates,
     activation, m7_note}——数据源 = registry（真源）+ factor_registry_state.json
     （lifecycle/health）+ fl_factor_eval / fl_factor_gate（治理结论只读汇总）；
  2) 缺口项（gro_earnings_surprise）入表带 gap 态（无 defs 不注册，连假设都不立——
     T-F4 登记三要素）；
  3) summary：n_active/n_rejected/n_gap 计数自洽 + family_coverage 五族覆盖 +
     deviation 逐条（为什么不是 21 个 active——D2 验收锚；25→24 唯一实体口径差）；
  4) 落档 sandbox/artifacts/m7_status/factor_status_m7.{md,json}。

实体口径（诚实声明，deviation 同文落档）：契约 25 = 21 V3 + 4 存量为「身份计」口径——
val_pe_ttm 既是 V3 估值 5 成员又是 M1 存量（双身份）；唯一实体 = 24 注册 + 1 缺口登记
= 25（含 T-F9 personal_clone 通道演练候选 demo_clone_f1——通道验证用，未物化、门禁照拒，
不豁免任何关卡），本表按唯一实体登记，无遗漏项（mom_20d_fl/sig_mom20_rank_fl/
eps_revise_fl 均不在 V3 §6.2 清单）。

运行：``cd alphapocket && /workspace/alphapocket-web/core/.venv/bin/python scripts/m7_status_m7.py``
"""
from __future__ import annotations

import json
import sys
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

SB_REPO = ROOT / "sandbox" / "repo"       # 状态真源（factor_registry_state.json；= Ctx.local_repo）
DB = ROOT / "sandbox" / "data" / "fl.duckdb"
OUT_DIR = ROOT / "sandbox" / "artifacts" / "m7_status"

ORIGIN = {"val_pe_ttm": "M1 存量（M2 冒烟激活；V3 估值 5 成员）",
          "mom_20d_fl": "M1 存量（引擎 A 近似 20 日动量；非 V3 §6.2 清单）",
          "sig_mom20_rank_fl": "M1 存量（引擎 B 信号 rank 投影；非 V3 §6.2 清单）",
          "eps_revise_fl": "M1 存量（事件近似；非 V3 §6.2 清单）"}
FAMILY = {"val_pe_ttm": "估值", "val_pb_lf": "估值", "val_ps_ttm": "估值",
          "val_ev_ebitda": "估值", "val_div_yield": "估值",
          "qua_roe_ttm": "质量", "qua_gross_margin": "质量", "qua_accruals": "质量",
          "qua_ocf_to_np": "质量", "qua_debt_to_asset": "质量",
          "gro_rev_yoy": "成长", "gro_np_yoy": "成长", "gro_rev_cagr_3y": "成长",
          "gro_stability": "成长",
          "mom_20d_fl": "动量", "mom_12_1": "动量", "mom_6_1": "动量",
          "mom_industry_adj": "动量",
          "low_vol_252d": "低波红利", "low_beta": "低波红利",
          "div_payout_streak": "低波红利",
          "sig_mom20_rank_fl": "（五族外·信号）", "eps_revise_fl": "（五族外·事件）",
          "demo_clone_f1": "（五族外·克隆）"}
ORIGIN_DEFAULT = {"val_pb_lf": "M7 T-F2", "val_ps_ttm": "M7 T-F2",
                  "val_ev_ebitda": "M7 T-F2", "val_div_yield": "M7 T-F2",
                  "qua_roe_ttm": "M7 T-F3", "qua_gross_margin": "M7 T-F3",
                  "qua_accruals": "M7 T-F3", "qua_ocf_to_np": "M7 T-F3",
                  "qua_debt_to_asset": "M7 T-F3",
                  "gro_rev_yoy": "M7 T-F4", "gro_np_yoy": "M7 T-F4",
                  "gro_rev_cagr_3y": "M7 T-F4", "gro_stability": "M7 T-F4",
                  "mom_12_1": "M7 T-F5", "mom_6_1": "M7 T-F5",
                  "mom_industry_adj": "M7 T-F5",
                  "low_vol_252d": "M7 T-F5", "low_beta": "M7 T-F5",
                  "div_payout_streak": "M7 T-F5",
                  "demo_clone_f1": "M7 T-F9（personal_clone 通道演练候选，未物化）"}
GAP_ROWS = [{
    "factor_id": "gro_earnings_surprise", "family": "成长",
    "origin": "V3 §6.2 成长 5（T-F4 缺口登记）",
    "lifecycle": "unregistered",
    "health": "unknown",
    "eval": "—（未物化：业绩预告/快报历史不可得）",
    "gates": "—（未走流水线）",
    "activation": "gap(业绩预告/快报历史数据源缺失，未登记假设——无来源不激活，"
                  "见 sandbox/artifacts/m7_gaps/gro_earnings_surprise.md 三要素)",
    "m7_note": "M8 候选：接入一致预期源后从 ① 假设登记重走；不作为近似实现冒充",
}]


def _fmt_pass(p) -> str:
    return {True: "pass", False: "fail", None: "soft"}[p]


def _gate_summary(con, factor_id: str) -> str:
    """fl_factor_gate 最新 as_of 五关只读摘要（不重算，直接引 detail_json 结论）。"""
    rows = con.execute("""SELECT gate, pass, detail_json FROM fl_factor_gate
                          WHERE factor_id = ?
                          ORDER BY as_of DESC, gate""", [factor_id]).fetchall()
    if not rows:
        return "—（无门禁记录）"
    # 同因子可能多 as_of：取最新 as_of 的关卡集
    as_of = con.execute("SELECT max(as_of) FROM fl_factor_gate WHERE factor_id = ?",
                        [factor_id]).fetchone()[0]
    g: dict = {}
    for gate, p, det in rows:
        if gate not in g:                       # ORDER BY as_of DESC → 首见即最新
            g[gate] = (p, json.loads(det) if det else {})
    parts = [f"time={_fmt_pass(g.get('time_robustness', (None,))[0])}",
             f"param={_fmt_pass(g.get('param_robustness', (None,))[0])}",
             f"env={_fmt_pass(g.get('environment', (None,))[0])}",
             f"rho={_fmt_pass(g.get('incremental_value', (None,))[0])}",
             f"bonf={_fmt_pass(g.get('multiplicity', (None,))[0])}"]
    inc = g.get("incremental_value", (None, {}))[1]
    for r in inc.get("reasons") or []:
        if "|ρ|=" in str(r):
            parts.append(f"（{str(r).split('，')[0].split('；')[0]}）")
            break
    bonf = (g.get("multiplicity", (None, {}))[1].get("bonferroni") or {})
    if bonf.get("p_adj") is not None:
        parts.append(f"p_adj={bonf['p_adj']:.3g}")
    return " ".join(parts) + f" · as_of={as_of}"


def _eval_summary(con, factor_id: str) -> str:
    """fl_factor_eval 最新 as_of h1 只读摘要。"""
    row = con.execute("""SELECT directed_mean, n_periods, ic_ir, nw_t, as_of
                         FROM fl_factor_eval WHERE factor_id = ? AND horizon = 1
                         ORDER BY as_of DESC LIMIT 1""", [factor_id]).fetchone()
    if not row:
        return "—（无 eval 记录）"
    dm, n, icir, nwt, as_of = row
    dm_s = f"{dm:+.4f}" if dm is not None else "NA"
    return f"h1 directed_mean={dm_s} n={n} ic_ir={icir if icir is None else round(icir, 4)} " \
           f"nw_t={nwt if nwt is None else round(nwt, 4)} as_of={as_of}"


def _activation(m, health: str, note_n: int) -> tuple[str, str]:
    """（activation, m7_note）：active/rejected/gap 三态 + 拒因摘要（引用门禁 reasons）。"""
    if m.status == "active":
        act = f"active@v{m.version}"
        if health == "invalid":
            act += "（health=invalid → 合成池摘除，两维分离不迁 lifecycle）"
        return act, ORIGIN.get(m.id, ORIGIN_DEFAULT.get(m.id, ""))
    return (f"rejected（门禁未过保持 draft@v{m.version}，拒因见 gates/七查明细）",
            ORIGIN_DEFAULT.get(m.id, ""))


def main() -> int:
    import duckdb

    from factorlib.lifecycle import active_pool
    from factorlib.registry import get_health, load_registry

    con = duckdb.connect(str(DB), read_only=True)
    try:
        pool = active_pool(None, SB_REPO)
        metas = {m.id: m for m in load_registry(SB_REPO)}   # 状态真源（sandbox/repo）
        state_health = {fid: get_health(SB_REPO, fid)[0] for fid in metas}

        factors = []
        for m in load_registry(SB_REPO):
            health = state_health.get(m.id, "unknown")
            eval_s = _eval_summary(con, m.id)
            gates_s = _gate_summary(con, m.id)
            act, note = _activation(m, health, 0)
            factors.append({
                "factor_id": m.id, "family": FAMILY[m.id],
                "origin": ORIGIN.get(m.id) or ORIGIN_DEFAULT.get(m.id, "—"),
                "lifecycle": m.status, "health": health,
                "eval": eval_s, "gates": gates_s, "activation": act,
                "m7_note": note,
            })
        factors.extend(json.loads(json.dumps(GAP_ROWS)))

        n_active = sum(1 for f in factors if f["lifecycle"] == "active")
        n_rejected = sum(1 for f in factors if f["lifecycle"] == "draft")
        n_gap = sum(1 for f in factors if f["lifecycle"] == "unregistered")
        fam_total = {}
        fam_active = {}
        for f in factors:
            fam = f["family"]
            if fam.startswith("（"):                # 五族外存量不进族覆盖
                continue
            fam_total[fam] = fam_total.get(fam, 0) + 1
            if f["lifecycle"] == "active":
                fam_active[fam] = fam_active.get(fam, 0) + 1
        family_coverage = {k: f"{fam_active.get(k, 0)}/{v}"
                           for k, v in fam_total.items()}

        n_pool = len(pool["pool"])
        deviation = [
            ("实体口径：契约 25 = 21 V3 + 4 存量为「身份计」口径——val_pe_ttm 既是 V3 估值 5 "
            "成员又是 M1 存量（双身份）；唯一实体 = 24 注册 + 1 缺口登记 = 25（含 T-F9 "
            "personal_clone 通道演练候选 demo_clone_f1），本表按唯一实体登记，无遗漏项"
            "（mom_20d_fl / sig_mom20_rank_fl / eps_revise_fl 均不在 V3 §6.2 清单）。"),
            ("T-F9 个人化因子通道（计划 10.4 E3 策略克隆）：demo_clone_f1 经 "
            "`ap fl candidate register --from-strategy` 登记（来源=克隆自外部策略，"
            "拆解依据在案），source.type=personal_clone——「个人化」只影响来源标注，"
            "七查门禁照走照拒（未物化 → 拒），不豁免任何关卡；单测锁定无豁免。"),
            ("D2 诚实口径——为什么 active=4 而非 21（逐因子拒因引用本表 gates 列与 "
            "fl_factor_gate 明细）："),
            ("  · 估值 1/5：val_pe_ttm 为唯一 active（M2 冒烟激活，T-F2 升版 v1.1.0）；"
            "pb_lf/ps_ttm/ev_ebitda/div_yield 门禁未过保持 draft（时间稳健/增量关拒因在档）；"),
            "  · 质量 0/5：T-F3 五因子时间稳健未过（qua_ocf_to_np 增量关亦未过）保持 draft；",
            ("  · 成长 0/5：T-F4 四因子时间稳健未过保持 draft；gro_earnings_surprise 缺口登记"
            "（一致预期源缺失，未登记假设）；"),
            ("  · 动量 1/4：mom_20d_fl（存量）active；V3 动量 3 因子 A 股正向命题被样本证伪"
            "（12-1/6-1/行业调整定向 IC 均为负 → 反转证据）且族内/对存量 |ρ|>0.6 → 替代通道；"),
            ("  · 低波红利 0/3：方向为正（low_vol +0.051 / streak +0.032）但时间稳健未过，"
            "low_vol 与 val_div_yield |ρ|=0.63 → 替代通道（宁缺毋滥）。"),
            ("五族覆盖（multi-resonance-v2 消费锚）：动量 ✓ / 估值 ✓ / 质量 ✗ / 成长 ✗ / "
            "低波红利 ✗ →「五族全量」不成立，T-F7 策略映射按族降级（近似版纪律延续）。"),
            (f"池容量：active {pool['n_active']}/{pool['cap']}（软上限），合成池内 {n_pool}"
            f"（eps_revise_fl、sig_mom20_rank_fl 均 health=invalid 摘除——后者 2026-10-15 "
            f"巡检对 T-F7 数据面同步后的 bars 如实重算：近24月 directed IC -0.046 ≤ 0 且"
            f"全段失效，healthy→invalid 合法迁移；v2 引用它被池准入拒绝=门禁正确行为，"
            f"恢复路径=摘除 sig 升 v3 重走 lockbox，M8 待办）——计划 M5 入口「≥6 个 active」"
            f"未达成（池内 {n_pool} < 6），如实登记；池不因凑数放宽门禁（§11 #6 红线）。"),
        ]
        summary = {"n_total": len(factors), "n_active": n_active,
                   "n_rejected": n_rejected, "n_gap": n_gap,
                   "n_pool": n_pool, "cap": pool["cap"],
                   "family_coverage": family_coverage,
                   "deviation": deviation}
    finally:
        con.close()

    doc = {"as_of": date.today().isoformat(), "generated_by": "T-F6",
           "factors": factors, "summary": summary}

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    (OUT_DIR / "factor_status_m7.json").write_text(
        json.dumps(doc, ensure_ascii=False, indent=1), encoding="utf-8")

    lines = ["# M7 T-F6 · 因子状态总表（25 项身份计 / 25 唯一实体，诚实口径）", "",
             (f"as_of={doc['as_of']} · 生成=T-F6（只读汇总，治理结论以 fl_factor_eval/"
             "fl_factor_gate/状态文件为准，本表不重算）"), "",
             "| 因子 | 族 | 来源 | lifecycle | health | eval(h1) | 三关/增量/多重 | 激活结论 |",
             "| --- | --- | --- | --- | --- | --- | --- | --- |"]
    for f in factors:
        lines.append(f"| {f['factor_id']} | {f['family']} | {f['origin']} | "
                     f"{f['lifecycle']} | {f['health']} | {f['eval']} | "
                     f"{f['gates']} | {f['activation']} |")
    lines += ["", "## summary", "",
              (f"- n_total={summary['n_total']}（active {summary['n_active']} + "
              f"rejected {summary['n_rejected']} + gap {summary['n_gap']} = "
              f"{summary['n_active'] + summary['n_rejected'] + summary['n_gap']}，处置完备）"),
              f"- 合成池：{summary['n_pool']}/{summary['cap']}（软上限；≥6 硬条件见 deviation）",
              f"- family_coverage：{json.dumps(summary['family_coverage'], ensure_ascii=False)}",
              "", "## deviation（D2 验收锚）", ""]
    lines += [f"- {d}" for d in deviation]
    (OUT_DIR / "factor_status_m7.md").write_text("\n".join(lines) + "\n", encoding="utf-8")

    print(f"总表落档：{OUT_DIR / 'factor_status_m7.md'}")
    print(f"  n_total={summary['n_total']} active={summary['n_active']} "
          f"rejected={summary['n_rejected']} gap={summary['n_gap']} 池内={n_pool}")
    print(f"  family_coverage={family_coverage}")
    for f in factors:
        print(f"  {f['factor_id']:<20} {f['family']:<10} {f['lifecycle']:<12} "
              f"{f['health']:<10} {f['activation'][:60]}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
