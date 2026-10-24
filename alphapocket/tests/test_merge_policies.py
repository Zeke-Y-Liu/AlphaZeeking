"""合并策略参数化测试（T-R10：五策略纯函数 + apply_policy 分派 + fetch_all 三模式）

覆盖（DoD）：
- 五种合并策略各 1（含 apply_policy old=None 分支）；
- apply_policy 按 spec.merge_policy 分派（真实注册表 spec 逐条对拍）；
- replace/registry_merge 误入 → ValueError；非法 policy → ValueError；
- fetch_all mode 推导兼容（incremental_from→incremental / None→bootstrap）；
- refresh 缺 secid 报错；非法 mode 报错；
- merge_bars_union 边界 pct 修复对拍（与 datasource._merge_bars 逐字节一致）。

运行：``cd alphapocket && python -m pytest tests/test_merge_policies.py tests/test_datasource_merge.py -q``
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from applib import datasource as DS
from applib.datasets import get_spec
from applib.datasets.merge import (
    apply_policy,
    merge_bars_union,
    merge_carry_old,
    merge_fill_missing,
    merge_new_nonempty,
    merge_set_union,
)

# ── 五种合并策略（数据集级纯函数）────────────────────────────────

def test_merge_bars_union():
    old = {"A": [{"dt": "2026-10-08", "close": 10.0, "pct": 0.01}]}
    new = {"A": [{"dt": "2026-10-09", "close": 10.5, "pct": 0.05}],
           "B": [{"dt": "2026-10-09", "close": 2.0, "pct": None}]}
    merged = merge_bars_union(old, new)
    assert [r["dt"] for r in merged["A"]] == ["2026-10-08", "2026-10-09"]   # 并集升序
    assert merged["A"][1]["close"] == 10.5                                  # 重叠日新胜
    assert merged["B"] == new["B"]                                          # 仅新有的 secid 原样
    empty_new = merge_bars_union({"A": old["A"]}, {"A": []})                # 空窗口 → 保旧
    assert empty_new["A"] == old["A"]


def test_merge_bars_union_pct_boundary():
    """窗口首根 pct=None 用旧尾收盘补全；|cand|>0.33 公司行动守卫保持 None（对拍 _merge_bars）。"""
    old = {"X": [{"dt": "2026-10-08", "close": 10.0, "pct": 0.01}]}
    new = {"X": [{"dt": "2026-10-09", "close": 10.2, "pct": None}]}         # 10.2/10-1=2% → 补
    merged = merge_bars_union(old, new)
    assert merged["X"][1]["pct"] == pytest.approx(0.02)
    assert merged["X"] == DS._merge_bars(old["X"], new["X"])                # 与旧实现逐字节对拍

    old2 = {"Y": [{"dt": "2026-10-08", "close": 10.0, "pct": 0.01}]}
    new2 = {"Y": [{"dt": "2026-10-09", "close": 15.0, "pct": None}]}        # 50% → 守卫保持 None
    merged2 = merge_bars_union(old2, new2)
    assert merged2["Y"][1]["pct"] is None
    assert merged2["Y"] == DS._merge_bars(old2["Y"], new2["Y"])


def test_merge_new_nonempty():
    old = {"A": [{"dt": "2026-10-01", "close": 1.0}], "B": [{"dt": "2026-10-02", "close": 2.0}]}
    new = {"A": [], "C": [{"dt": "2026-10-03", "close": 3.0}]}
    merged = merge_new_nonempty(old, new)
    assert merged["A"] == old["A"]          # 新空 → 携带旧（滚动窗口防丢）
    assert merged["B"] == old["B"]          # 新缺 → 携带旧
    assert merged["C"] == new["C"]          # 新非空 → 用新


def test_merge_set_union():
    old = {"A": ["2026-10-01", "2026-10-02"], "B": ["2026-09-30"]}
    new = {"A": ["2026-10-02", "2026-10-03"]}
    merged = merge_set_union(old, new)
    assert merged["A"] == ["2026-10-01", "2026-10-02", "2026-10-03"]        # 去重并集升序
    assert merged["B"] == ["2026-09-30"]                                    # 新缺 → 保旧


def test_merge_fill_missing():
    old = {"A": {"reports": [1]}, "B": {"reports": [2]}}
    new = {"A": {"reports": [9]}}           # A 本次已拉取 → 全量重写不降级
    merged = merge_fill_missing(old, new)
    assert merged["A"] == {"reports": [9]}
    assert merged["B"] == {"reports": [2]}  # 拉取失败标的 → 补旧


def test_merge_carry_old():
    old = {"series": {"m2_yoy": [{"dt": "2026-09-30", "v": 8.0}], "shibor_1m": []},
           "meta": {"m2_yoy": {"source": "央行"}}, "as_of": "2026-09-30"}
    new = {"series": {"shibor_1m": [{"dt": "2026-10-09", "v": 1.6}], "m2_yoy": []},
           "meta": {"shibor_1m": {"source": "Shibor"}}, "as_of": "2026-10-09"}
    merged = merge_carry_old(old, new)
    assert merged["series"]["m2_yoy"] == old["series"]["m2_yoy"]            # 旧非空 + 新空 → 携带旧
    assert merged["meta"]["m2_yoy"] == {"source": "央行"}                    # 旧 meta 一并携带
    assert merged["series"]["shibor_1m"] == new["series"]["shibor_1m"]      # 新非空 → 用新
    assert merged["as_of"] == "2026-10-09"                                  # 合并后按全序列重算
    # 旧全空 → 仅重算 as_of，不破坏新值
    merged2 = merge_carry_old({"series": {}, "meta": {}, "as_of": None},
                              {"series": {"k": [{"dt": "2026-10-01", "v": 1}]},
                               "meta": {}, "as_of": None})
    assert merged2["as_of"] == "2026-10-01"


# ── apply_policy：分派与守卫 ─────────────────────────────────────

def test_apply_policy_old_none_passthrough():
    new = {"A": [{"dt": "2026-10-09", "close": 1.0}]}
    assert apply_policy(get_spec("daily_bars"), None, new) is new           # bootstrap 原样返回


def test_apply_policy_dispatch_by_spec():
    """真实注册表 spec 逐条对拍：7 条可合并数据集各归其策略。"""
    old = {"A": [1]}
    assert apply_policy(get_spec("daily_bars"), dict(old), {}) == {"A": [1]}
    assert apply_policy(get_spec("daily_bars_raw"), dict(old), {}) == {"A": [1]}
    assert apply_policy(get_spec("cross_check_tx"), dict(old), {}) == {"A": [1]}   # 新空携旧
    assert apply_policy(get_spec("adjust_ex_dates"), {"A": ["d1"]}, {}) == {"A": ["d1"]}
    assert apply_policy(get_spec("fundamental_values"), dict(old), {}) == {"A": [1]}
    assert apply_policy(get_spec("market_cap"), dict(old), {}) == {"A": [1]}
    ml = apply_policy(get_spec("macro_liquidity"),
                      {"series": {"k": [{"dt": "d", "v": 1}]}, "meta": {}, "as_of": None},
                      {"series": {}, "meta": {}, "as_of": None})
    assert ml["series"]["k"] == [{"dt": "d", "v": 1}]


def test_apply_policy_rejects_non_snapshot_policies():
    with pytest.raises(ValueError, match="不支持快照合并"):
        apply_policy(get_spec("trade_calendar"), {}, {})                    # replace
    with pytest.raises(ValueError, match="不支持快照合并"):
        apply_policy(get_spec("factors_registry"), {}, {})                  # registry_merge


def test_apply_policy_invalid_policy():
    fake = SimpleNamespace(name="ghost_ds", merge_policy="bogus")
    with pytest.raises(ValueError, match="bogus"):
        apply_policy(fake, {}, {})


# ── fetch_all 三模式：解析 / 校验（不触网，纯前置段）─────────────

def test_resolve_mode_derivation():
    assert DS._resolve_mode(None, None) == "bootstrap"                      # 缺省 → bootstrap
    assert DS._resolve_mode(None, {}) == "bootstrap"                        # 空 dict 视为未指定
    assert DS._resolve_mode(None, {"601058.SH": "2026-10-09"}) == "incremental"
    assert DS._resolve_mode("bootstrap", {"601058.SH": "2026-10-09"}) == "bootstrap"   # 显式优先
    assert DS._resolve_mode("refresh", None) == "refresh"


def test_resolve_mode_invalid():
    with pytest.raises(ValueError, match="mode 非法"):
        DS._resolve_mode("full", None)


def test_fetch_all_refresh_requires_secid():
    with pytest.raises(ValueError, match="refresh 模式必须提供 secid"):
        DS.fetch_all(SimpleNamespace(), mode="refresh")                     # 校验先于任何 IO/网络


def test_fetch_all_invalid_mode():
    with pytest.raises(ValueError, match="mode 非法"):
        DS.fetch_all(SimpleNamespace(), mode="sync_all")
