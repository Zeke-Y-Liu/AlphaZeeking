"""DatasetSpec schema/校验/驱动通道测试（T-R5）。"""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from applib import datasets as DS
from applib.datasets import DatasetSpec, SourceSpec


def _ok(**over):
    base = dict(dataset_id="dwd_daily_bar", path="market/daily_bars.json",
                merge_policy="union_bars",
                fetch_params={"start": "2021-01-01", "frequency": "daily"},
                sources=(SourceSpec(interface="ak.stock_zh_a_daily", upstream="新浪财经",
                                    role="primary"),
                         SourceSpec(interface="web.ifzq.gtimg.cn", upstream="腾讯证券",
                                    role="crosscheck")),
                validation={"min_rows": 100, "required_keys": ["dt", "close"],
                            "as_of_field": "dt"})
    base.update(over)
    return DatasetSpec(**base)


# ── schema：合法 spec 通过 ───────────────────────────────────────────────
def test_valid_spec_passes():
    s = _ok()
    DS.validate_spec(s)                                 # 不 raise 即通过
    mini = DatasetSpec(dataset_id="x", path="a/b.json", merge_policy="overwrite",
                       sources=(SourceSpec("ak.foo", "机构"),))
    DS.validate_spec(mini)                              # 最小 spec（仅必填）


# ── 非法 spec 逐字段拒绝 + 错误信息含字段定位 ─────────────────────────────
def _reject(spec, frag):
    try:
        DS.validate_spec(spec)
        raise AssertionError(f"should reject: {frag}")
    except ValueError as e:
        assert frag in str(e), f"错误信息缺字段定位: {e}"


def test_invalid_specs_rejected_per_field():
    _reject(_ok(dataset_id=""), "DatasetSpec.dataset_id")
    _reject(_ok(path=""), "DatasetSpec.path")
    _reject(_ok(path="/abs/bars.json"), "DatasetSpec.path")          # 绝对路径
    _reject(_ok(path="market/../x.json"), "DatasetSpec.path")        # ..
    _reject(_ok(path="market/bars.parquet"), "DatasetSpec.path")     # 非 json
    _reject(_ok(merge_policy="upsert_v3"), "DatasetSpec.merge_policy")
    _reject(_ok(fetch_params=None), "DatasetSpec.fetch_params")
    _reject(_ok(sources=()), "DatasetSpec.sources")                  # 空 → 多源必需
    _reject(_ok(sources=("ak.foo",)), "DatasetSpec.sources[0]")
    _reject(_ok(sources=(SourceSpec("", "新浪"),)), "DatasetSpec.sources[0]")
    _reject(_ok(sources=(SourceSpec("ak.foo", "新浪", role="master"),)),
            "DatasetSpec.sources[0].role")
    _reject(_ok(validation={"min_rows": -1}), "DatasetSpec.validation.min_rows")
    _reject(_ok(validation={"required_keys": [1, 2]}), "DatasetSpec.validation.required_keys")
    _reject(_ok(validation={"as_of_field": 3}), "DatasetSpec.validation.as_of_field")


# ── 加载器：文件加载 + dataset 定位 ──────────────────────────────────────
def test_load_specs_from_file(tmp_path):
    f = tmp_path / "datasets.json"
    f.write_text(json.dumps({
        "dwd_daily_bar": {"path": "market/daily_bars.json", "merge_policy": "union_bars",
                          "sources": [{"interface": "ak.stock_zh_a_daily", "upstream": "新浪财经"}]},
        "bad": {"path": "/abs.json", "merge_policy": "union_bars",
                "sources": [{"interface": "ak.x", "upstream": "y"}]}}), encoding="utf-8")
    try:
        DS.load_specs(f)
        raise AssertionError("should reject bad")
    except ValueError as e:
        assert "DatasetSpec[bad]" in str(e) and "DatasetSpec.path" in str(e)   # 双重定位
    f.write_text(json.dumps({"dwd_daily_bar": {
        "path": "market/daily_bars.json", "merge_policy": "union_bars",
        "fetch_params": {"start": "2021-01-01"},
        "sources": [{"interface": "ak.stock_zh_a_daily", "upstream": "新浪财经",
                     "role": "primary"}],
        "validation": {"min_rows": 100}}}), encoding="utf-8")
    specs = DS.load_specs(f)
    assert specs["dwd_daily_bar"].validation["min_rows"] == 100
    assert specs["dwd_daily_bar"].sources[0].role == "primary"


# ── 驱动通道：策略委托（行为等价既有实现）+ 未注册策略显式拒绝 ────────────
def test_merge_by_policy_delegates():
    s = _ok()
    old = {"600519.SH": [{"dt": "2024-01-02", "close": 10.0}]}
    new = {"600519.SH": [{"dt": "2024-01-03", "close": 11.0}]}
    merged, info = DS.merge_by_policy(s, new, old)      # union_bars → 委托 writeguard
    assert [r["dt"] for r in merged["600519.SH"]] == ["2024-01-02", "2024-01-03"]
    assert info["resume"] == ["600519.SH"]              # 接链语义与 writeguard.merge_bars 一致
    assert merged["600519.SH"][1]["pct"] == 0.1         # new 首行 pct 接链重算（10→11）
    # registry_merge 委托等价
    sr = DatasetSpec(dataset_id="factor_registry", path="factors/registry.json",
                     merge_policy="registry_merge",
                     sources=(SourceSpec("inline", "内置"),))
    m, _ = DS.merge_by_policy(
        sr, {"factors": [{"id": "a", "status": "active"}]},
        {"factors": [{"id": "a", "status": "paused"}]})
    assert m["factors"][0]["status"] == "paused"        # 用户改动胜出（与 T-R4 等价）


def test_merge_by_policy_rejects_unregistered():
    s = _ok(merge_policy="carry_old")
    try:
        DS.merge_by_policy(s, {}, {})
        raise AssertionError("should reject")
    except NotImplementedError as e:
        assert "T-R6" in str(e) and "carry_old" in str(e)   # 显式拒绝不私造第二实现


# ── persist_spec：相对 repo 落盘 ─────────────────────────────────────────
def test_persist_spec_writes_relative_path(tmp_path):
    s = _ok(path="market/daily_bars.json")
    p = DS.persist_spec(tmp_path, s, {"k": 1})
    assert p == tmp_path / "market/daily_bars.json" and json.loads(p.read_text()) == {"k": 1}


# ── 迁移清单（DoD 3）：存量硬编码定义已登记，T-R6 逐项销账 ────────────────
def test_pending_migration_covers_hardcoded():
    ids = {m["dataset_id"] for m in DS.PENDING_MIGRATION}
    assert {"dwd_daily_bar", "dwd_financial_report", "macro_liquidity",
            "factor_registry", "portfolio_cn4"} <= ids
    assert all(set(m) >= {"dataset_id", "path", "src"} for m in DS.PENDING_MIGRATION)
    # 合并策略枚举与 writeguard 现有能力对齐（单点声明，无第二实现）
    assert DS.MERGE_POLICY_FN["union_bars"].__module__.endswith("writeguard")
