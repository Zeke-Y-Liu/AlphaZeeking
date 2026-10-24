"""M7 T-F7：v1/v2 对照报告（universe 扩展 + lockbox 口径恢复的双重变化，诚实口径）。

数据源（全部只读归档，不重跑 v1——M6 历史归档冻结红线）：
- v1：M6 backtest 归档（m6_bt_*-v1_bt_*/report.json）+ M6 wf 归档（lockbox 2025-10 实际
  封存口径）+ v1 YAML 因子清单 + M6 数据底座 universe=9（M6 总览 §2 事实引用）；
- v2：本轮 backtest 归档（bt_2*，strategy=<v2 id>）+ 本轮 wf（lockbox 2022-01）+ v2 YAML
  因子清单 + T-F1 数据底座 universe 实测（sandbox clone daily_bars 标的数）。

不可比红线：v1（universe 9）与 v2（universe 56）的 universe 不同 → 绝对收益不可直接比较；
报告呈现结构差异（因子组合/覆盖/封存口径），不构成排名优劣；v1 归档不重跑（数据底座已扩，
重跑无意义）；v2 为新基线。

运行：``cd alphapocket && /workspace/alphapocket-web/core/.venv/bin/python scripts/m7_v2_compare.py``
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

SB_REPO = ROOT / "sandbox" / "repo"
ART = ROOT / "sandbox" / "artifacts"
OUT_DIR = ART / "m7_v2_compare"
M6_WF = ART / "strategy_wf_20261011_074758_5b1ea7" / "report.json"   # M6 lockbox 口径锚

NAMES = ("lowvol-dividend", "pe-roe", "multi-resonance")
_V1_ARCH = {
    "lowvol-dividend": ("lowvol-dividend-approx-v1", "lowvol-dividend-approx-v1.yaml"),
    "pe-roe": ("pe-roe-approx-v1", "pe-roe-approx-v1.yaml"),
    "multi-resonance": ("multi-resonance-v1", "multi-resonance-v1.yaml"),
}
_COMPARABILITY = ("universe 不同（v1: M6 数据底座 9 标的 → v2: T-F1 底座 {uni} 标的）且 "
                  "lockbox 口径不同（v1: 2025-10 被迫 → v2: 2022-01 计划口径恢复）→ "
                  "绝对收益不可直接比较；本报告呈现结构差异（因子组合/覆盖/封存口径），"
                  "不构成 v2 优于 v1 的排名结论。")


def _m6_bt_report(v1_id: str) -> dict:
    hits = sorted(ART.glob(f"m6_bt_{v1_id}_bt_*/report.json"))
    assert hits, f"M6 v1 归档缺失：{v1_id}"
    return json.loads(hits[-1].read_text(encoding="utf-8"))


def _v2_bt_report(v2_id: str) -> dict:
    # 归属以 run 目录名（m6_bt_<id>_bt_*）为准：report.strategy 是成交模式描述非策略 id
    hits = sorted(ART.glob(f"m6_bt_{v2_id}_bt_*/report.json"))
    assert hits, f"v2 回测归档缺失：{v2_id}"
    return json.loads(hits[-1].read_text(encoding="utf-8"))


def _lockbox_start_v1() -> str:
    rep = json.loads(M6_WF.read_text(encoding="utf-8"))
    return rep["cfg"]["lockbox_start"]


def _factors_of(yaml_path: Path) -> list[str]:
    import yaml
    raw = yaml.safe_load(yaml_path.read_text(encoding="utf-8"))
    return [f["id"] for f in raw.get("factors") or []]


def _perf(rep: dict) -> dict:
    sm = rep.get("strategy_metrics") or {}
    return {"ret": sm.get("total_return"), "sharpe": sm.get("sharpe"),
            "dd": sm.get("max_dd"), "start": rep.get("start"), "end": rep.get("end")}


def main() -> int:
    import duckdb

    m6_lb = _lockbox_start_v1()
    con = duckdb.connect(str(ROOT / "sandbox" / "data" / "fl.duckdb"), read_only=True)
    try:
        uni_v2 = con.execute("SELECT count(DISTINCT secid) FROM fl_daily_bar").fetchone()[0]
    finally:
        con.close()

    strategies: dict = {}
    for name in NAMES:
        v1_id, v1_yaml = _V1_ARCH[name]
        v2_id = f"{name}-v2"
        strategies[name] = {
            "v1": {"id": v1_id, "factors": _factors_of(SB_REPO / "strategies" / v1_yaml),
                   "universe": 9, "lockbox": m6_lb, **_perf(_m6_bt_report(v1_id)),
                   "note": "M6 近似版归档（active 池近似腿；v1 归档冻结不重跑）"},
            "v2": {"id": v2_id, "factors": _factors_of(SB_REPO / "strategies" / f"{v2_id}.yaml"),
                   "universe": uni_v2, "lockbox": "2022-01",
                   **_perf(_v2_bt_report(v2_id)),
                   "note": "T-F7 新基线（因子组合与 v1 相同：五族新因子门禁未过全 draft，"
                           "近似版纪律延续；变化 = universe 底池扩展 + lockbox 恢复计划口径）"},
            "comparability": _COMPARABILITY.format(uni=uni_v2),
        }
    doc = {"strategies": strategies,
           "notes": "v1 历史归档不重跑（数据底座已扩，重跑无意义）；v2 为新基线。"
                    "因子替换未发生（v2 因子 = v1 近似腿）：五族 19 个新因子经 T-F2~T-F5 "
                    "①–⑦ 全流程后全部未过激活门禁（T-F6 总表 active 4/24），红线禁止拿 "
                    "draft 凑五族——v2 升版实际内容 = universe 扩展（9→56）+ lockbox 恢复 "
                    "2022-01 + 缺腿/缺族结构化披露。multi-resonance-v2 因 mom_20d_fl v1.1.0 "
                    "演练段（2021-02 起）不覆盖 core 区间，显式 pin v1.0.0 全段（M5 T-L3 钉版）。"}

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    (OUT_DIR / "compare_v1_v2.json").write_text(
        json.dumps(doc, ensure_ascii=False, indent=1), encoding="utf-8")

    L = ["# M7 T-F7 · v1/v2 对照报告（结构差异呈现，非排名优劣）", "",
         ("不可比口径：universe 不同（9 → 56）+ lockbox 口径不同（2025-10 → 2022-01）"
         "→ 绝对收益不可直接比较。"), ""]
    for name, row in strategies.items():
        v1, v2 = row["v1"], row["v2"]
        L += [f"## {name}", "",
              "| 侧 | id | 因子 | universe | lockbox | ret | sharpe | max_dd | 区间 |",
              "| --- | --- | --- | --- | --- | --- | --- | --- | --- |",
              (f"| v1 | {v1['id']} | {', '.join(v1['factors'])} | {v1['universe']} | "
              f"{v1['lockbox']} | {v1['ret']:.4f} | {v1['sharpe']:.4f} | {v1['dd']:.4f} | "
              f"{v1['start']}..{v1['end']} |"),
              (f"| v2 | {v2['id']} | {', '.join(v2['factors'])} | {v2['universe']} | "
              f"{v2['lockbox']} | {v2['ret']:.4f} | {v2['sharpe']:.4f} | {v2['dd']:.4f} | "
              f"{v2['start']}..{v2['end']} |"),
              "", f"comparability：{row['comparability']}", ""]
    L += ["## notes", "", doc["notes"], ""]
    (OUT_DIR / "compare_v1_v2.md").write_text("\n".join(L), encoding="utf-8")

    print(f"对照报告落档：{OUT_DIR / 'compare_v1_v2.md'}")
    for name, row in strategies.items():
        print(f"  {name:<16} v1 ret={row['v1']['ret']:.4f} → v2 ret={row['v2']['ret']:.4f}"
              f"（不可比：universe {row['v1']['universe']}→{row['v2']['universe']}）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
