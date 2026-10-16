"""M7 T-F1：universe 扩展构成（确定性脚本化选股，决策 D1：扩至 ~40-60 只 A 股）。

规则（重跑一致，非手挑）：
1. 10 个东财行业板块（贴近申万一级行业分布，全部经 stock_board_industry_name_em 存在性校验）；
2. 每板块成分按当日成交额降序取前 PER_BOARD_CAND 只候选；
3. 过滤：名称含 ST/*ST/退市/次新标记（N/C 打头为上市首日/次日，直接剔除）；
4. 上市 ≥5 年：行情数据驱动判定——2017-01 小窗首根 K 线 ≤ 2017-02-01 才算
   （2017 年中后上市的股票小窗内无 K 线，自然淘汰）；
5. 每板块按候选序取前 TARGET_PICK 只 → 终版 picked；
6. 既有 5 只 ETF（multi5 组合回测依赖）原样保留。

产出 dim/universe_m7.json：{generated_at, rule, boards, candidates, picked, etf_keep}。
picked 供 fetch_all(universe=[...]) 直接消费；candidates 留档淘汰透明。
"""
from __future__ import annotations

import datetime as dt
import json
from pathlib import Path


# ── 板块配置（东财行业板块名，2026-10-11 实测存在）─────────────────────
M7_BOARDS = [
    "化学制品",   # 基础化工代表
    "通用设备",   # 机械设备代表
    "医药生物",
    "食品饮料",
    "银行",       # 金融代表（财报科目特殊，因子层 None 口径如实处理）
    "半导体",     # 电子/科技成长代表
    "汽车",
    "电力",       # 公用事业代表
    "白色家电",
    "煤炭开采",   # 能源/高股息代表
]
PER_BOARD_CAND = 8      # 每板块候选数（含淘汰余量）
TARGET_PICK = 6         # 每板块终选数（10×6=60 上限，淘汰后预期 40-55）
KEEP_ETFS = ["510300.SH", "501018.SH", "518880.SH", "513100.SH", "511260.SH"]

_NAME_BAD = ("ST", "*ST", "退")        # 风险警示/退市整理
_NAME_PREFIX_BAD = ("N", "C")          # 上市首日/次日（次新直接剔除）


def _board_candidates(ak, board: str) -> list[dict]:
    """东财板块成分 → 过滤后按当日成交额降序的候选列表。"""
    df = ak.stock_board_industry_cons_em(symbol=board)
    rows = []
    for _, r in df.iterrows():
        name = str(r.get("名称") or "")
        if any(b in name for b in _NAME_BAD) or name[:1] in _NAME_PREFIX_BAD:
            continue
        code = str(r.get("代码") or "")
        if len(code) != 6 or not code.isdigit():
            continue
        rows.append({"code": code,
                     "secid": f"{code}.{'SH' if code[0] in '69' else 'SZ'}",
                     "name": name,
                     "board": board,
                     "amount": float(r.get("成交额") or 0)})
    rows.sort(key=lambda x: -x["amount"])
    return rows[:PER_BOARD_CAND]


def build_universe(repo_dir, out_path=None) -> dict:
    """拉取板块成分 → 候选池 + 上市时间验证 → 终版 picked → universe_m7.json。

    out_path：真源落盘位置（缺省 ``repo_dir/dim/universe_m7.json``）。T-F1 流程应传
    **稳定目录**（sandbox/artifacts/...）——fetch bootstrap 会 rmtree remote/repo，
    构建产物先进易变层会随重建丢失（2026-10-11 实测教训），落盘后由驱动脚本拷入消费。
    """
    import akshare as ak

    names = set(ak.stock_board_industry_name_em()["板块名称"].tolist())
    missing = [b for b in M7_BOARDS if b not in names]
    if missing:
        raise RuntimeError(f"东财板块名变更/缺失（上游改名可感知）：{missing}")

    candidates: list[dict] = []
    for b in M7_BOARDS:
        cands = _board_candidates(ak, b)
        if len(cands) < TARGET_PICK:
            raise RuntimeError(f"板块 {b} 候选仅 {len(cands)} 只 < 目标 {TARGET_PICK}（规则不可满足，如实报错）")
        candidates.extend(cands)

    # 上市 ≥5 年行情驱动验证：2017-01 小窗首根 K 线（新浪日K，单次轻量请求）。
    # 新浪接口对带日期窗口的请求偶发返回空 DataFrame（如 603986 小窗空但全量首K 2016-08-18，
    # 2026-10-11 实测）→ 小窗为空不等于未上市：追加一次全量首K 复判（透明防误杀）。
    def _first_kline(sina_sym: str) -> str | None:
        try:
            df = ak.stock_zh_a_daily(symbol=sina_sym, start_date="20170101",
                                     end_date="20170301", adjust="")
            if len(df):
                return str(df["date"].min())[:10]
        except Exception:                       # noqa: BLE001 - 单点失败走全量复判
            pass
        try:
            df = ak.stock_zh_a_daily(symbol=sina_sym, adjust="")
            return str(df["date"].min())[:10] if len(df) else None
        except Exception:                       # noqa: BLE001 - 上游失败 → 该标的按未上市处理淘汰
            return None

    listed: dict[str, str | None] = {}
    for c in candidates:
        listed[c["secid"]] = _first_kline(f"{'sh' if c['secid'].endswith('.SH') else 'sz'}{c['code']}")

    picked: list[dict] = []
    dropped: list[dict] = []
    per_board_n: dict[str, int] = {}
    for c in candidates:                        # 候选序即板块内成交额序
        first = listed.get(c["secid"])
        ok = bool(first) and first <= "2017-02-01"
        rec = {**{k: c[k] for k in ("secid", "code", "name", "board")},
               "first_kline": first, "passed": ok}
        if ok and per_board_n.get(c["board"], 0) < TARGET_PICK:
            per_board_n[c["board"]] = per_board_n.get(c["board"], 0) + 1
            picked.append(rec)
        else:
            dropped.append({**rec, "drop_reason": "new_listing" if not ok else "board_full"})

    out = {
        "generated_at": dt.datetime.now().isoformat(timespec="seconds"),
        "rule": {"boards": M7_BOARDS, "per_board_cand": PER_BOARD_CAND,
                 "target_pick": TARGET_PICK,
                 "listed_cut": "2017-02-01（上市≥5年行情驱动判定）",
                 "filter": "名称 ST/*ST/退/N/C 剔除 + 板块内当日成交额降序"},
        "candidates_n": len(candidates),
        "picked": picked,
        "dropped": dropped,
        "etf_keep": KEEP_ETFS,
    }
    p = Path(out_path) if out_path is not None else repo_dir / "dim" / "universe_m7.json"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(out, ensure_ascii=False, indent=1), encoding="utf-8")
    return out


def picked_secids(u: dict) -> list[str]:
    """终版 universe → fetch_all 可消费的 secid 列表（A 股 + 保留 ETF）。"""
    return [r["secid"] for r in u["picked"]] + list(u.get("etf_keep") or [])


def board_of(u: dict, secid: str) -> str | None:
    """secid → 所属板块（ETF/未收录 → None）。"""
    for r in u.get("picked") or []:
        if r["secid"] == secid:
            return r["board"]
    return None
