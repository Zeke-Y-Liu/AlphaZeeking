"""M7 T-F2 ②：估值族 4 因子 M3 检验 + M4 三关/增量/多重 + 激活门禁（契约 ⑤-⑦）。

前置：m7_val_factors_m7.py 已完成物化（fl_factor_value_daily / lineage / 定义指纹）。
动作（对 4 估值因子逐个）：
  1) M3 run_factor_eval（月度截面 IC/分层/多头超额）→ save_eval_report（fl_factor_eval）
  2) M4 run_full_gauntlet：时间关（walk-forward 样本外）+ 参数关（transform 变体网格）
     + 环境关（regime 分段）+ 增量关（vs 存量 4 因子 |ρ|<0.6）+ 多重检验（Bonferroni/DSR）
     → save_gate_report（fl_factor_gate）
  3) 激活门禁七查 → activate_factor（sandbox/repo 状态真源；未过则如实拒绝并落档拒因）
  4) 汇总：门禁结论 + ρ>0.6 处置（转替代/拒绝通道，宁缺毋滥）

运行：``cd alphapocket && /workspace/alphapocket-web/core/.venv/bin/python scripts/m7_val_gate_m7.py``
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

REPO = ROOT / "remote" / "repo"           # 行情 closes（eval 数据面）
SB_REPO = ROOT / "sandbox" / "repo"       # 状态真源 + hypothesis/registry
DB = ROOT / "sandbox" / "data" / "fl.duckdb"
VAL4 = ("val_pb_lf", "val_ps_ttm", "val_ev_ebitda", "val_div_yield")
DIRECTION = {"val_pb_lf": "asc", "val_ps_ttm": "asc",
             "val_ev_ebitda": "asc", "val_div_yield": "desc"}
EXISTING = ("val_pe_ttm", "mom_20d_fl", "sig_mom20_rank_fl", "eps_revise_fl")
VARIANTS = [{"label": "rank_pct", "transform": ["rank_pct"]},
            {"label": "winsor_mad_z", "transform": ["winsorize_mad", "zscore"]}]


def main() -> int:
    from applib.factor_eval_fl import run_factor_eval, save_eval_report
    from applib.factor_governance import run_full_gauntlet, save_gate_report
    from factorlib.lifecycle import activation_gate, activate_factor
    from factorlib.registry import load_factor

    # argv：因子子集（续跑）；"--gate-only" 只跑门禁+激活（eval/gate 报告已在档时用）
    argv = sys.argv[1:]
    gate_only = "--gate-only" in argv
    fids = [a for a in argv if a in VAL4] or list(VAL4)

    print("== M3/M4 + 激活门禁（4 估值因子）==")
    for fid in fids:
        meta = load_factor(REPO, fid)          # 包内 defs（draft/1.0.0，运行时状态在 SB_REPO）
        print(f"\n── {fid}（direction={meta.direction} → eval {DIRECTION[fid]}）──")

        if not gate_only:
            rep = run_factor_eval(DB, REPO, fid, DIRECTION[fid])
            save_eval_report(rep, DB)
            h1 = rep["reports"].get(1) or next(iter(rep["reports"].values()))
            ic = h1["ic"]
            print(f"  h1 directed_mean={ic['directed_mean']} ic_ir={ic['ic_ir']} "
                  f"nw_t={ic['nw_t']} n={ic['n']}")

            res = run_full_gauntlet(DB, REPO, meta, variants=VARIANTS, existing=list(EXISTING))
            save_gate_report(res, DB)
            for name, g in res["gates"].items():
                if name == "multiplicity":
                    if g:
                        b = g.get("bonferroni") or {}
                        print(f"  多重检验 bonferroni reject={b.get('reject')} "
                              f"p={b.get('p_adj')} n_trials={b.get('n_trials')}（演练口径）")
                    continue
                print(f"  gate {name}: pass={g.get('pass')} reasons={g.get('reasons')}")
            corr = res.get("corr") or {}
            ids = corr.get("factor_ids") or []
            i = ids.index(fid) if fid in ids else 0
            rhos = {other: corr["matrix"][i][j] for j, other in enumerate(ids)
                    if other != fid and corr["matrix"][i][j] is not None}
            print(f"  增量相关 ρ vs 既有: {json.dumps(rhos, ensure_ascii=False)}")

        gate = activation_gate(DB, SB_REPO, fid)
        print(f"  七查 gate ok={gate['ok']}")
        for k, v in gate["checks"].items():
            print(f"    {k}: ok={v['ok']} {v['detail']}")
        act = activate_factor(DB, SB_REPO, fid, who="m7-tf2",
                              why="T-F2 估值族：M3/M4 全链路通过，契约①-⑦落档")
        print(f"  activate: {act['activated']} {act.get('reason', '')}")

    print("\n== 终态 ==")
    con_sb = None
    from factorlib.registry import load_factor as _lf
    for fid in VAL4:
        m = _lf(SB_REPO, fid)
        print(f"  {fid:16s} status={m.status} v{m.version} hyp={m.hypothesis_id}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
