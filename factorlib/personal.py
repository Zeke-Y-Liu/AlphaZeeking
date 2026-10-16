"""M7 个人化因子通道（实施计划 §10 任务 10.4）：E3 策略克隆 → 候选因子 → 同一流水线。

设计（SeekAlpha 拆解 §7 E3 ·《个人投资引擎-完整设计方案》个人驱动行）：
- 输入：用户交易账本 trades YAML（secid + entry_date + satisfied 标记）——
  满意交易集合 = 「你的打法」的样本（生产由个人知识层事实层供给，沙箱为演示账本）；
- ① 特征提取：对每笔满意交易的 entry_date，取该 secid 在全部已物化注册因子的
  方向调整秩分位（direction·value 截面排名 → 高分位 = 因子偏好该票）；
- ② 归纳：因子分位中位数 ≥ E3_PCT_THRESHOLD → 共性特征（打法可由既有因子
  解释的部分）；无满足者 → 诚实报「无法克隆」，不生成候选；
- ③ 候选生成：共性特征截面 zscore 方向调整后等权合成 → per_e3_clone
  （family=personal，hypothesis=H-E3-001）YAML 落盘 definitions/personal/，
  注册初始态 draft（注册纪律：初始状态必须 draft）；
- ④ 同一流水线不豁免：物化 → ③ 检验（meta_factor_eval 凭据）→ ④⑤⑥ 治理
  （成分因子入批，⑤ 增量关对克隆共线如实裁决）→ 激活门禁尝试
  （verdict ≠ pass 即 LifecycleError，无旁路）→ 裁决与阻断原因入报告。

E3 证据分级（SeekAlpha 拆解 §10 纪律）：满意交易样本量 ≥ E3_MIN_TRADES 才允许
归纳（不足 → 强制降级 E1 并拒绝归纳）；报告固定标注样本量与检验区间。

诚实口径：克隆因子是既有因子的线性重组，⑤ 增量关预期判「替代通道」——这正是
门禁设计语义：个人打法若只是既有因子重组合，不携带独立增量信息，进入对比通道
而非独立注册；通道交付物 = 可复现的打法画像（共性特征 + 复现分位）+ 门禁裁决。
依赖纪律：纯标准库 + duckdb + yaml（与 registry 同源，不引入 pandas）。

CLI：python -m factorlib.personal --repo <dir> --trades <trades.yaml> [--out <dir>]
"""
from __future__ import annotations

import argparse
import json
import statistics
from datetime import date
from pathlib import Path

import duckdb
import yaml

from .evaluate import (avg_rank, build_universe_db, evaluate_factor, save_eval)
from .governance import LOCKBOX_DEFAULT, WF_ALPHA, run_governance_on_con
from .lifecycle import LifecycleError, activate
from .materialize import materialize
from .pit_audit import audit as pit_audit
from .registry import FactorMeta, load_definitions

# ── 通道常量（E3 分级纪律，冻结口径） ──
E3_MIN_TRADES = 2        # 满意交易样本下限（沙箱口径；生产建议 ≥ 10，诚实标注）
E3_PCT_THRESHOLD = 0.70  # 方向秩分位中位数阈值（≥ 即共性特征）
CANDIDATE_ID = "per_e3_clone"
HYPOTHESIS_ID = "H-E3-001"
FAMILY = "personal"


# ── 输入：交易账本 ──
def load_trades(path: str | Path) -> list[dict]:
    """加载 trades YAML：{trades: [{secid, entry_date, satisfied, note?}]}。

    校验：secid/entry_date 必填（entry_date 须 ISO 日期）；satisfied 缺省 False。
    """
    doc = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}
    rows = doc.get("trades")
    if not isinstance(rows, list) or not rows:
        raise ValueError(f"{path}: trades 须为非空列表")
    out = []
    for i, r in enumerate(rows):
        if not isinstance(r, dict) or not r.get("secid") or not r.get("entry_date"):
            raise ValueError(f"{path}: trades[{i}] 缺 secid/entry_date")
        try:
            dateText = str(r["entry_date"])
            date.fromisoformat(dateText)
        except ValueError as e:
            raise ValueError(f"{path}: trades[{i}] entry_date 非法：{e}") from e
        out.append({"secid": str(r["secid"]), "entry_date": dateText,
                    "satisfied": bool(r.get("satisfied", False)),
                    "note": str(r.get("note") or "")})
    return sorted(out, key=lambda t: (t["entry_date"], t["secid"]))


# ── ① 特征提取：方向秩分位 ──
def extract_features(con: duckdb.DuckDBPyConnection, metas: dict,
                     trades: list[dict],
                     min_cross: int = 2) -> tuple[dict[str, list[float]], list[str]]:
    """每因子：各笔满意交易 entry_date 上该 secid 的方向调整秩分位（0~1]。

    只消费已物化的注册 alpha 因子（factor_value_daily ∩ metas 且 direction=±1）；
    任一笔交易当日截面 < min_cross 或标的无值 → 该因子整体剔除（诚实登记 note）。
    返回 ({fid: [pct per trade]}, drop_notes)。
    """
    avail = {r[0] for r in con.execute(
        "SELECT DISTINCT factor_id FROM factor_value_daily").fetchall()}
    out: dict[str, list[float]] = {}
    notes: list[str] = []
    for fid in sorted(avail):
        meta = metas.get(fid)
        if meta is None or (getattr(meta, "type", "alpha") or "alpha") != "alpha" \
                or getattr(meta, "direction", 1) not in (1, -1):
            continue
        direction = meta.direction
        pcts: list[float] = []
        ok = True
        for t in trades:
            rows = con.execute(
                "SELECT secid, value FROM factor_value_daily"
                " WHERE factor_id = ? AND CAST(dt AS VARCHAR) = ? AND value IS NOT NULL",
                [fid, t["entry_date"]]).fetchall()
            vals = {s: v for s, v in rows}
            if len(vals) < min_cross or t["secid"] not in vals:
                notes.append(f"{fid}: {t['entry_date']} 截面不足或 {t['secid']} 无值"
                             f"（n={len(vals)}）→ 剔除")
                ok = False
                break
            secids = sorted(vals)
            ranks = dict(zip(secids, avg_rank([direction * vals[s] for s in secids])))
            pcts.append(ranks[t["secid"]] / len(secids))
        if ok:
            out[fid] = pcts
    return out, notes


# ── ② 归纳：共性特征 ──
def induce(features: dict[str, list[float]],
           threshold: float = E3_PCT_THRESHOLD) -> dict[str, float]:
    """分位中位数 ≥ threshold 的因子 → {fid: median}（「打法」的因子化画像）。"""
    return {fid: round(statistics.median(pcts), 4)
            for fid, pcts in sorted(features.items())
            if statistics.median(pcts) >= threshold}


# ── ③ 候选生成 ──
def build_candidate_sql(component_ids: list[str], directions: dict[str, int]) -> str:
    """候选体 SQL：成分因子截面 zscore → 方向调整 → 等权平均（高分 = 打法偏好）。"""
    in_list = ", ".join(f"'{f}'" for f in component_ids)
    whens = " ".join(f"WHEN '{f}' THEN zv * ({directions[f]})" for f in component_ids)
    return f"""WITH z AS (
  SELECT dt, secid, factor_id,
         (value - avg(value) OVER (PARTITION BY dt, factor_id))
           / nullif(stddev_samp(value) OVER (PARTITION BY dt, factor_id), 0) AS zv
  FROM factor_value_daily
  WHERE factor_id IN ({in_list})
    AND ($start_dt IS NULL OR dt >= CAST($start_dt AS DATE))
    AND ($end_dt IS NULL OR dt <= CAST($end_dt AS DATE))
),
zd AS (
  SELECT dt, secid, CASE factor_id {whens} END AS z
  FROM z WHERE zv IS NOT NULL
)
SELECT dt, secid, avg(z) AS value,
       CAST(NULL AS DATE) AS used_period, CAST(NULL AS DATE) AS used_announce
FROM zd GROUP BY dt, secid"""


def build_candidate(features: dict[str, float], directions: dict[str, int],
                    n_satisfied: int, window: list[str]) -> tuple[str, str]:
    """生成候选 YAML 文本与 SQL 体（确定性：components 按 id 序）。"""
    import textwrap
    comps = sorted(features)
    sql = build_candidate_sql(comps, directions)
    head = (
        f"# {CANDIDATE_ID} —— E3 策略克隆候选（M7 §10.4 个人化因子通道，自动生成）\n"
        f"# 归纳来源：用户满意交易 {n_satisfied} 笔（区间 {window[0]}..{window[1]}）；\n"
        f"# 共性特征 = 方向秩分位中位数 ≥ {E3_PCT_THRESHOLD} 的因子：{', '.join(comps)}\n"
        f"# 口径：成分因子截面 zscore 方向调整后等权平均；高分 = 打法偏好\n"
        f"# 门禁纪律：与常规候选同一流水线（③④⑤⑥），不豁免任何关卡；\n"
        f"# PIT：读已过 PIT 审计的 factor_value_daily，无独立公告日口径 → 审计面外（诚实登记）\n")
    body = {"id": CANDIDATE_ID, "name": "E3策略克隆（个人打法复现）", "family": FAMILY,
            "type": "alpha", "status": "draft", "version": "1.0.0", "universe": ["CN"],
            "engine": "sql", "lookback_days": 0, "depends": ["factor_value_daily"],
            "direction": 1, "hypothesis_id": HYPOTHESIS_ID, "transform": []}
    y = yaml.safe_dump(body, allow_unicode=True, sort_keys=False,
                       default_flow_style=None, width=4096)
    return head + y + "sql: |\n" + textwrap.indent(sql, "  ") + "\n", sql


def register_candidate(root: str | Path, yaml_text: str,
                       con: duckdb.DuckDBPyConnection | None = None) -> Path:
    """候选落盘 definitions/personal/（注册初始态 draft）；已激活候选禁止重归纳。"""
    if con is not None:
        try:
            row = con.execute(
                "SELECT count(*) FROM meta_factor_archive"
                " WHERE factor_id = ? AND status = 'active'", [CANDIDATE_ID]).fetchone()
        except duckdb.CatalogException:
            row = None          # 沙箱内存库无 archive 表 → 无已激活快照，放行
        if row and row[0]:
            raise LifecycleError(
                f"{CANDIDATE_ID}: 已激活候选不可重归纳覆盖（升 version 走新候选）")
    p = Path(root) / "definitions" / FAMILY / f"{CANDIDATE_ID}.yaml"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(yaml_text, encoding="utf-8")
    return p


# ── ④ 同一流水线（不豁免任何关卡） ──
def run_channel_on_con(con: duckdb.DuckDBPyConnection, root: str | Path,
                       trades: list[dict], metas: dict | None = None,
                       threshold: float = E3_PCT_THRESHOLD,
                       wf: tuple[int, int] = WF_ALPHA,
                       lockbox_months: int = LOCKBOX_DEFAULT,
                       out_dir: str | Path | None = None,
                       repo: str = "", trades_file: str = "") -> dict:
    """E3 通道核心（con 已备好 dwd 表；metas 缺省 = 注册中心全量加载）。"""
    root = Path(root)
    if metas is None:
        metas = {m.id: m for m in load_definitions(root)}
    sat = [t for t in trades if t["satisfied"]]
    n_sat = len(sat)
    rep: dict = {"repo": repo, "trades_file": trades_file,
                 "n_trades": len(trades), "n_satisfied": n_sat,
                 "threshold": threshold, "candidate_id": CANDIDATE_ID}

    # E3 证据分级：样本不足 → 强制降级 E1，拒绝归纳（不生成候选）
    if n_sat < E3_MIN_TRADES:
        rep.update(evidence="E1",
                   window=[t["entry_date"] for t in sat] if sat else [],
                   candidate=None, verdict=None, activation=None,
                   conclusion=(f"满意交易 {n_sat} 笔 < {E3_MIN_TRADES} → 证据强制降级 E1"
                               f"（样本不足），不生成候选（诚实拒绝归纳）"))
        _flush(rep, out_dir)
        return rep

    have = {r[0] for r in con.execute("SELECT secid FROM dim_security").fetchall()}
    unknown = sorted({t["secid"] for t in sat} - have)
    if unknown:
        raise ValueError(f"满意交易含库外 secid：{unknown}（账本须与 universe 对齐）")
    window = [min(t["entry_date"] for t in sat), max(t["entry_date"] for t in sat)]
    rep["evidence"], rep["window"] = "E3", window

    # 前置（同一流水线的数据前提）：注册 alpha 因子全量物化，特征提取不消费未物化值
    reg_alphas = [m for m in metas.values() if isinstance(m, FactorMeta)
                  and (m.type or "alpha") == "alpha" and m.direction in (1, -1)]
    for m in reg_alphas:
        materialize(con, m, mode="full")
    rep["n_registered_materialized"] = len(reg_alphas)

    feats_pcts, notes = extract_features(con, metas, sat)
    rep["features"] = {fid: round(statistics.median(p), 4)
                       for fid, p in sorted(feats_pcts.items())}
    rep["drop_notes"] = notes
    feats = induce(feats_pcts, threshold)
    if not feats:
        rep.update(components=[], candidate=None, verdict=None, activation=None,
                   conclusion=("无因子分位中位数 ≥ 阈值 → 打法与既有因子库无共性特征，"
                               "诚实不克隆（可另立新因子假设走常规 ①–⑦）"))
        _flush(rep, out_dir)
        return rep
    rep["components"] = feats

    # 候选注册（draft）+ 成分因子物化（同一流水线：通道内不消费未物化值）
    yaml_text, _ = build_candidate(
        feats, {fid: metas[fid].direction for fid in feats}, n_sat, window)
    yaml_path = register_candidate(root, yaml_text, con)
    clone_meta = next(m for m in load_definitions(root) if m.id == CANDIDATE_ID)
    for fid in sorted(feats):
        if isinstance(metas.get(fid), FactorMeta):
            materialize(con, metas[fid], mode="full")
    mat = materialize(con, clone_meta, mode="full")
    pit = pit_audit(con, CANDIDATE_ID)

    # ③ 检验凭据（meta_factor_eval 1M 行）→ ④⑤⑥ 治理（成分因子入批）
    lo, hi = con.execute("SELECT min(CAST(dt AS VARCHAR)), max(CAST(dt AS VARCHAR))"
                         " FROM dwd_daily_bar").fetchone()
    eval_rep = evaluate_factor(con, clone_meta, lo, hi)
    n_eval = save_eval(con, eval_rep)
    batch = {**metas, CANDIDATE_ID: clone_meta}
    gov = run_governance_on_con(
        con, batch, factor_ids=[CANDIDATE_ID] + sorted(feats),
        start=lo, end=hi, wf=wf, lockbox_months=lockbox_months,
        materialize_values=False)
    v = next(x for x in gov["verdicts"] if x["factor_id"] == CANDIDATE_ID)
    rep["candidate"] = {"yaml_path": str(yaml_path), "materialized_rows": mat["rows"],
                        "pit": {"checked": pit["checked"], "violations": pit["violations"],
                                "note": "克隆因子读已审计的 factor_value_daily，"
                                        "无独立公告日口径 → 审计面外（诚实登记）"},
                        "eval_1m_ic": next(
                            (hh["ic_mean"] for h, hh in eval_rep["horizons"].items()
                             if h == 1), None),
                        "eval_saved": n_eval}
    rep["verdict"] = {k: v[k] for k in
                      ("overall", "main_ic", "time_gate", "param_gate",
                       "regime_gate", "incremental", "pit")}

    # E3 复现度：候选因子在满意交易 entry_date 上的方向秩分位均值 + 样本量 + 区间
    clone_pcts, _ = extract_features(con, {CANDIDATE_ID: clone_meta}, sat)
    repro = (round(sum(clone_pcts[CANDIDATE_ID]) / len(clone_pcts[CANDIDATE_ID]), 4)
             if CANDIDATE_ID in clone_pcts else None)
    rep["reproduction"] = {"mean_pct": repro, "sample_size": n_sat, "window": window,
                           "note": "E3 置信度 = 打法被候选规则复现的程度（秩分位均值）；"
                                   "样本量与检验区间强制标注"}

    # 激活门禁尝试（verdict ≠ pass 即拒，无旁路）
    try:
        act = activate(con, clone_meta, gov, root=root)
        rep["activation"] = {"attempted": True, "activated": True,
                             "activated_at": act.get("activated_at")}
    except LifecycleError as e:
        rep["activation"] = {"attempted": True, "activated": False,
                             "blocked_by": str(e)}

    rep["conclusion"] = (
        f"候选 {CANDIDATE_ID} 走完同一流水线（物化→③→④⑤⑥→激活门禁），"
        f"治理裁决 {v['overall']}，激活{'通过' if rep['activation']['activated'] else '被拒（无旁路）'}；"
        f"通道交付 = 打法画像（{len(feats)} 个共性特征）+ 复现分位 {repro} + 门禁裁决")
    _flush(rep, out_dir)
    return rep


def run_channel(repo_dir: str | Path, trades_path: str | Path,
                out_dir: str | Path | None = None,
                threshold: float = E3_PCT_THRESHOLD) -> dict:
    """E3 通道一条命令：建库 → 账本 → 归纳 → 候选 → 同一流水线 → 报告落盘。"""
    con = build_universe_db(repo_dir)
    root = Path(__file__).resolve().parent
    return run_channel_on_con(con, root, load_trades(trades_path),
                              threshold=threshold, out_dir=out_dir,
                              repo=str(repo_dir), trades_file=str(trades_path))


def _flush(rep: dict, out_dir: str | Path | None) -> None:
    if out_dir is None:
        return
    d = Path(out_dir)
    d.mkdir(parents=True, exist_ok=True)
    (d / "personal_channel.json").write_text(
        json.dumps(rep, ensure_ascii=False, indent=1), encoding="utf-8")
    (d / "personal_channel.md").write_text(render_md(rep), encoding="utf-8")


def render_md(rep: dict) -> str:
    """E3 通道报告：输入 → 归纳 → 候选与门禁 → 激活 → 置信度 → 结论。"""
    L = ["# M7 个人化因子通道 · E3 策略克隆", "",
         f"- repo：{rep['repo']} · 账本：{rep['trades_file']}",
         f"- 交易 {rep['n_trades']} 笔（满意 {rep['n_satisfied']}）· "
         f"证据分级：**{rep['evidence']}** · 阈值：分位中位数 ≥ {rep['threshold']}", ""]
    if rep.get("candidate") is None:
        L += [f"## 结论", "", f"- {rep['conclusion']}"]
        return "\n".join(L) + "\n"
    L += ["## ① 共性特征归纳（方向秩分位中位数）", "",
          "| 因子 | 中位分位 | 入选 |", "|---|---|---|"]
    for fid, med in rep["features"].items():
        L.append(f"| {fid} | {med:.4f} | {'✅' if fid in rep['components'] else '—'} |")
    for n in rep.get("drop_notes", []):
        L.append(f"- ⚠ {n}")
    L += ["", "## ② 候选与同一流水线（不豁免任何关卡）", "",
          f"- 候选：`{rep['candidate_id']}`（family=personal，draft）· "
          f"物化 {rep['candidate']['materialized_rows']} 行 · "
          f"③ 检验 1M IC = {rep['candidate']['eval_1m_ic']}",
          f"- PIT：checked={rep['candidate']['pit']['checked']} "
          f"violations={rep['candidate']['pit']['violations']}"
          f"（{rep['candidate']['pit']['note']}）", "",
          "| 关卡 | 通过 | 明细 |", "|---|---|---|"]
    vd = rep["verdict"]
    gates = [("④a 时间", "time_gate"), ("④b 参数", "param_gate"),
             ("④c 环境", "regime_gate"), ("⑤ 增量", "incremental")]
    for label, key in gates:
        g = vd[key]
        det = g.get("reason") or g.get("note") or ""
        L.append(f"| {label} | {'✅' if g['pass'] else '❌'} | {det} |")
    L += ["", f"## ③ 激活门禁（无旁路）", "",
          f"- 裁决：**{vd['overall']}** · 激活："
          f"{'✅ 通过' if rep['activation']['activated'] else '❌ 被拒'}",
          f"- {rep['activation'].get('blocked_by', '')}", "",
          "## ④ E3 置信度", "",
          f"- 复现分位均值：{rep['reproduction']['mean_pct']} · "
          f"样本量：{rep['reproduction']['sample_size']} 笔 · "
          f"区间：{rep['reproduction']['window'][0]}..{rep['reproduction']['window'][1]}",
          f"- {rep['reproduction']['note']}", "",
          "## 结论", "", f"- {rep['conclusion']}"]
    return "\n".join(L) + "\n"


def main() -> None:
    """CLI：python -m factorlib.personal --repo <dir> --trades <trades.yaml> [--out <dir>]"""
    ap = argparse.ArgumentParser(description="M7 个人化因子通道：E3 策略克隆 → 候选 → 同一流水线")
    ap.add_argument("--repo", default="alphapocket/remote/repo")
    ap.add_argument("--trades", required=True, help="交易账本 YAML")
    ap.add_argument("--threshold", type=float, default=E3_PCT_THRESHOLD)
    ap.add_argument("--out", default=None)
    args = ap.parse_args()
    rep = run_channel(args.repo, args.trades, out_dir=args.out, threshold=args.threshold)
    print(f"evidence={rep['evidence']} satisfied={rep['n_satisfied']}")
    if rep.get("components"):
        print("components:", rep["components"])
        print("verdict:", rep["verdict"]["overall"],
              "| activated:", rep["activation"]["activated"])
        print("reproduction:", rep["reproduction"]["mean_pct"])
    print("conclusion:", rep["conclusion"])


if __name__ == "__main__":
    main()
