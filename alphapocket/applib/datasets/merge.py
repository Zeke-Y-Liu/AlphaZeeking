"""数据集增量合并策略（T-R10）：五种合并语义纯函数 + 按 spec.merge_policy 分派。

语义真源自 datasource.fetch_all 增量段（原 L1396-1408 等内联 if/elif）整体抽出，
**逐行为对拍、语义不动**；fetch_all 改为按声明分派（声明即文档）。

纯函数纪律：dict 进 dict 出（就地补齐 ``new`` 并返回，与原内联写法一致）；
不读盘、不打印、不依赖 ctx。
"""

from __future__ import annotations


def merge_bars(old: list, new: list) -> list:
    """旧序列 + 新窗口按 dt 并集合并（原 ``datasource._merge_bars`` L994-1022 整体迁入）。

    - 新窗口首根 ``pct`` 为 None 且旧尾存在时，用旧尾收盘补全边界收益
      （A 股服务端 start_date 过滤导致窗口首根无前收；|cand|>0.33 视为公司
      行动/份额折算，保持 None——与 fund 主链 0.33 阈值一致）；
    - 任一侧为空 → 原样返回另一侧（空窗口不破坏旧数据）。
    """
    if not old:
        return list(new)
    if not new:
        return list(old)
    by_dt: dict[str, dict] = {}
    for r in old:
        by_dt[r["dt"]] = dict(r)
    for r in new:
        by_dt[r["dt"]] = dict(r)
    merged = [by_dt[d] for d in sorted(by_dt)]
    first_new = min(r["dt"] for r in new)
    for i, r in enumerate(merged):
        if r["dt"] == first_new:
            if r.get("pct") is None and i > 0:
                prev_close = merged[i - 1].get("close")
                if prev_close and r.get("close"):
                    cand = r["close"] / prev_close - 1
                    if abs(cand) <= 0.33:
                        r["pct"] = cand
            break
    return merged


def merge_bars_union(old: dict, new: dict) -> dict:
    """bars_union 数据集级合并（daily_bars / daily_bars_raw）。

    语义出处：fetch_all 增量段原 L1397-1400。逐 secid：旧序列 + 新窗口按 dt
    并集（重叠日新胜 + 窗口首根 pct 边界修复）；新窗口为空/缺失 → 保旧。
    """
    for s, v in old.items():
        new[s] = merge_bars(v, new.get(s) or [])
    return new


def merge_new_nonempty(old: dict, new: dict) -> dict:
    """new_nonempty 数据集级合并（cross_check_tx 滚动窗口）。

    语义出处：fetch_all 增量段原 L1401-1403。逐 secid：新窗口非空用新、
    空/缺失携带旧（腾讯交叉源固定 120 日窗口，窗口可能为空）。
    """
    for s, v in old.items():
        if not new.get(s):
            new[s] = v
    return new


def merge_set_union(old: dict, new: dict) -> dict:
    """set_union 数据集级合并（adjust_ex_dates 除权除息日）。

    语义出处：fetch_all 增量段原 L1404-1405。逐 secid：日期集合去重并集、升序。
    """
    for s, v in old.items():
        new[s] = sorted(set(v) | set(new.get(s) or []))
    return new


def merge_fill_missing(old: dict, new: dict) -> dict:
    """fill_missing 数据集级合并（fundamental_values / market_cap）。

    语义出处：fetch_all 增量段原 L1406-1408（fundamental）与 L1470-1473（market_cap）。
    仅补缺失 secid：本次拉取已有标的全量重写不降级，拉取失败标的携带旧序列。
    """
    for s, v in old.items():
        if s not in new:
            new[s] = v
    return new


def merge_carry_old(old: dict, new: dict) -> dict:
    """carry_old 数据集级合并（macro_liquidity 平铺结构 {series, meta, as_of}）。

    语义出处：fetch_all 宏观段原 L1534-1543。逐指标：旧序列非空且新缺失 →
    携带旧 series 与旧 meta（月频窗口常为空，防丢历史）；合并后按全序列重算 as_of。
    """
    for k, v in (old.get("series") or {}).items():
        if v and not new["series"].get(k):
            new["series"][k] = v
            if (old.get("meta") or {}).get(k):
                new["meta"][k] = old["meta"][k]     # 旧 meta 无 error，口径一致
    dts = [r["dt"] for rows in new["series"].values() for r in rows]
    if dts:
        new["as_of"] = max(dts)
    return new


_POLICY_FNS = {
    "bars_union": merge_bars_union,
    "new_nonempty": merge_new_nonempty,
    "set_union": merge_set_union,
    "fill_missing": merge_fill_missing,
    "carry_old": merge_carry_old,
}


def apply_policy(spec, old: dict | None, new: dict) -> dict:
    """按 ``spec.merge_policy`` 分派合并策略；old=None（bootstrap 无快照）→ new 原样返回。

    ``replace`` / ``registry_merge`` 不在此处理（fetch_all 各自路径：全量重写 /
    T-R6 merge_registry 写保护合并）——误入 → ValueError（中文）。
    """
    if old is None:
        return new
    fn = _POLICY_FNS.get(spec.merge_policy)
    if fn is None:
        raise ValueError(f"{spec.name}: merge_policy {spec.merge_policy!r} 不支持快照合并"
                         f"（replace/registry_merge 走各自写入口，不可经 apply_policy）")
    return fn(old, new)
