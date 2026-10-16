"""M6 T-C1：Strategy DSL 测试——schema 校验一次报全 / 池准入 / 三过滤器 / 归一 spec / CLI 冒烟。

运行：``cd alphapocket && python -m pytest tests/test_strategy_dsl.py -q``
"""
from __future__ import annotations

import json
from datetime import date, timedelta
from pathlib import Path

import pytest
import yaml

from applib.strategy import StrategyError, apply_universe, load_strategy, parse_weight_kind

_STATE = "factors/factor_registry_state.json"
_DAY = "2026-10-11"


# ── 造数助手 ─────────────────────────────────────────────
def _pkg_yaml(fid: str) -> Path:
    import factorlib.registry as R
    return sorted((Path(R.__file__).parent / "defs").rglob(f"{fid}.yaml"))[0]


def _activate(repo: Path, fid: str, status: str = "active") -> None:
    p = repo / _STATE
    st = json.loads(p.read_text(encoding="utf-8")) if p.exists() else {}
    st[fid] = {"status": status, "version": "1.0.0", "history": []}
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(st, ensure_ascii=False, indent=2), encoding="utf-8")


def _active_repo(tmp_path: Path) -> Path:
    """repo：val_pe_ttm + mom_20d_fl 定义 + active 状态（池准入通过的最小前置）。"""
    repo = tmp_path / "repo"
    d = repo / "factors" / "defs"
    d.mkdir(parents=True, exist_ok=True)
    for fid in ("val_pe_ttm", "mom_20d_fl"):
        base = yaml.safe_load(_pkg_yaml(fid).read_text(encoding="utf-8"))
        (d / f"{fid}.yaml").write_text(yaml.dump(base, allow_unicode=True), encoding="utf-8")
        _activate(repo, fid)
    return repo


def _yml(tmp_path: Path, text: str, name: str = "s.yaml") -> Path:
    p = tmp_path / name
    p.write_text(text, encoding="utf-8")
    return p


def _load(tmp_path: Path, text: str, repo: Path | None = None):
    return load_strategy(_yml(tmp_path, text), repo or _active_repo(tmp_path),
                         tmp_path / "fl.duckdb")


_MIN_OK = """
id: t-ok
universe: CN_EQUITY_ALL
factors: [{id: val_pe_ttm, direction: asc}]
composite: {top_n: 3, method: [equal]}
"""


# ── schema 校验：一次报全 ─────────────────────────────────
def test_schema_errors_report_all(tmp_path):
    """缺 id / factors 空 / top_n / method / rebalance / universe 六错一次报全，不挤牙膏。"""
    with pytest.raises(StrategyError) as ei:
        _load(tmp_path, """
factors: []
composite: {top_n: 0, method: [nope]}
rebalance: weekly
universe: 123
""")
    msg = str(ei.value)
    assert "6 项" in msg and msg.count("\n  - ") == 6
    for kw in ("id 必填", "factors 必填", "top_n", "method", "rebalance", "universe"):
        assert kw in msg


def test_schema_enum_and_weight_errors(tmp_path):
    repo = _active_repo(tmp_path)
    with pytest.raises(StrategyError) as ei:
        _load(tmp_path, """
id: bad-enum
universe: CN_EQUITY_ALL
factors: [{id: val_pe_ttm, direction: sideways, weight: auto_wrong}]
composite: {top_n: 3, method: [equal, equal]}
rebalance: weekly
""", )
    msg = str(ei.value)
    assert "4 项" in msg
    for kw in ("direction 枚举非法", "weight 枚举非法", "重复", "rebalance 枚举非法"):
        assert kw in msg


def test_factor_duplicate_rejected(tmp_path):
    repo = _active_repo(tmp_path)
    with pytest.raises(StrategyError, match="因子重复：val_pe_ttm"):
        _load(tmp_path, """
id: dup
universe: CN_EQUITY_ALL
factors: [{id: val_pe_ttm}, {id: val_pe_ttm}]
composite: {top_n: 3, method: [equal]}
""")


def test_top_n_and_universe_filters_errors(tmp_path):
    repo = _active_repo(tmp_path)
    with pytest.raises(StrategyError) as ei:
        _load(tmp_path, """
id: t4
factors: [{id: val_pe_ttm}]
composite: {top_n: -1, method: [equal]}
universe: {base: CN, filters: "not-a-map"}
""")
    msg = str(ei.value)
    assert "2 项" in msg and "top_n" in msg and "universe.filters 必须是映射" in msg


def test_yaml_parse_error_and_top_level_type(tmp_path):
    with pytest.raises(StrategyError, match="YAML 解析失败"):
        _load(tmp_path, "a: [unclosed")
    with pytest.raises(StrategyError, match="顶层必须是映射"):
        _load(tmp_path, "- 1\n- 2\n")
    with pytest.raises(StrategyError, match="策略文件不存在"):
        load_strategy(tmp_path / "ghost.yaml", tmp_path, tmp_path / "db.duckdb")


def test_factors_count_bounds(tmp_path):
    repo = _active_repo(tmp_path)
    with pytest.raises(StrategyError, match="factors 必填且为 1..8"):
        _load(tmp_path, """
id: t-many
universe: CN_EQUITY_ALL
factors: [{id: f1}, {id: f2}, {id: f3}, {id: f4}, {id: f5}, {id: f6}, {id: f7}, {id: f8}, {id: f9}]
composite: {top_n: 3, method: [equal]}
""")


# ── weight 判别（T-C2 分派依据）────────────────────────────
def test_parse_weight_kind_matrix():
    assert parse_weight_kind("equal") == "fixed"
    assert parse_weight_kind(2) == "fixed" and parse_weight_kind(0.5) == "fixed"
    for auto in ("auto_ic_12m", "auto_ic_ir", "auto_lasso"):
        assert parse_weight_kind(auto) == auto
    with pytest.raises(StrategyError, match="weight 枚举非法"):
        parse_weight_kind("auto_xxx")
    with pytest.raises(StrategyError, match="weight 类型非法"):
        parse_weight_kind(True)          # bool 不是合法数值
    with pytest.raises(StrategyError, match="weight 类型非法"):
        parse_weight_kind([1, 2])


# ── 归一 spec ────────────────────────────────────────────
def test_normalized_defaults_and_spec_shape(tmp_path):
    repo = _active_repo(tmp_path)
    spec = _load(tmp_path, _MIN_OK, repo)
    assert spec["id"] == "t-ok" and spec["name"] == "t-ok"          # name 缺省取 id
    assert spec["factors"] == [{"id": "val_pe_ttm", "direction": "asc",
                                "weight": 1.0, "weight_kind": "fixed"}]
    assert spec["top_n"] == 3 and spec["methods"] == ["equal"]
    assert spec["rebalance"] == "monthly" and spec["exec"] == "close"
    assert spec["benchmark"] == "510300.SH" and spec["factor_version"] is None
    assert spec["constraints"] == {"t_plus_1": False, "limit_move": "defer",
                                   "suspension": "defer", "lot_size": 0,
                                   "max_weight": None, "min_history": None}
    assert spec["universe"] == {"base": "CN_EQUITY_ALL", "filters": {},
                                "markets": ["CN"]}   # T-F9：markets 缺省（向后兼容）


def test_full_spec_normalization(tmp_path):
    repo = _active_repo(tmp_path)
    spec = _load(tmp_path, """
id: full-spec
name: 显式策略名
universe: CN_EQUITY_ALL
factors:
  - {id: val_pe_ttm, direction: asc, weight: auto_ic_12m}
  - {id: mom_20d_fl, direction: desc, weight: 2}
composite: {top_n: 5, method: [equal, ic_12m, ic_ir, lasso]}
rebalance: quarterly
exec: next_day_vwap
benchmark: 000905.SH
factor_version: 1.2.0
constraints: {max_weight: 0.4, min_history: 500, t_plus_1: true, lot_size: 100}
""", repo)
    assert spec["name"] == "显式策略名"
    assert [f["weight_kind"] for f in spec["factors"]] == ["auto_ic_12m", "fixed"]
    assert spec["factors"][1]["weight"] == 2.0
    assert spec["methods"] == ["equal", "ic_12m", "ic_ir", "lasso"]
    assert spec["rebalance"] == "quarterly" and spec["exec"] == "next_day_vwap"
    assert spec["benchmark"] == "000905.SH" and spec["factor_version"] == "1.2.0"
    assert spec["constraints"]["t_plus_1"] is True and spec["constraints"]["lot_size"] == 100
    assert spec["universe"] == {"base": "CN_EQUITY_ALL", "filters": {},
                                "markets": ["CN"]}   # T-F9：显式缺省归一


# ── 池准入（M5 契约）──────────────────────────────────────
def test_pool_gate_pass_active(tmp_path):
    repo = _active_repo(tmp_path)
    spec = _load(tmp_path, _MIN_OK, repo)
    assert spec["factors"][0]["id"] == "val_pe_ttm"


def test_pool_gate_reject_draft_and_unregistered(tmp_path):
    repo = _active_repo(tmp_path)
    _activate(repo, "val_pe_ttm", status="draft")      # 拉回 draft
    with pytest.raises(StrategyError) as ei:
        _load(tmp_path, """
id: t-gate
universe: CN_EQUITY_ALL
factors: [{id: val_pe_ttm}, {id: ghost_factor}]
composite: {top_n: 3, method: [equal]}
""", repo)
    msg = str(ei.value)
    assert "status=draft" in msg and "未注册" in msg   # 逐个列出现状


def test_pool_gate_reject_invalid_health(tmp_path):
    from factorlib.registry import set_health
    repo = _active_repo(tmp_path)
    set_health(repo, "val_pe_ttm", "invalid", as_of=_DAY)
    with pytest.raises(StrategyError, match="invalid"):
        _load(tmp_path, _MIN_OK, repo)


def test_pool_gate_reject_deprecated(tmp_path):
    repo = _active_repo(tmp_path)
    _activate(repo, "val_pe_ttm", status="deprecated")
    with pytest.raises(StrategyError, match="deprecated"):
        _load(tmp_path, _MIN_OK, repo)


# ── universe 三过滤器 ─────────────────────────────────────
def _biz_days(n: int, start=(2021, 1, 4)) -> list[str]:
    d, out = date(*start), []
    while len(out) < n:
        if d.weekday() < 5:
            out.append(d.isoformat())
        d += timedelta(days=1)
    return out


def test_flt_st_filters_and_degraded(tmp_path):
    days = _biz_days(25)
    bars = {"600000.SH": [{"dt": d, "amount": 1e8} for d in days],
            "600001.SH": [{"dt": d, "amount": 1e8} for d in days],
            "600002.SH": [{"dt": d, "amount": 1e8} for d in days]}
    repo = tmp_path / "repo"
    (repo / "dim").mkdir(parents=True)
    (repo / "dim" / "security.json").write_text(json.dumps(
        [{"secid": "600000.SH", "name": "平安银行"},
         {"secid": "600001.SH", "name": "*ST 退市"},
         {"secid": "600002.SH", "name": "ST 某某"}], ensure_ascii=False), encoding="utf-8")
    d0 = days[-1]
    # 正常：ST/*ST 剔除
    out, snap = apply_universe({"flt_st": True}, bars, [d0], repo=repo)
    assert out[d0] == {"600000.SH"}
    assert snap["flt_st"] == {"n_before": 3, "n_after": 1}
    # 降级：无 repo（无名称源）→ degraded 披露，不是静默全通过
    out2, snap2 = apply_universe({"flt_st": True}, bars, [d0], repo=None)
    assert out2[d0] == {"600000.SH", "600001.SH", "600002.SH"}
    assert snap2["flt_st"]["degraded"] and "name 源缺失" in snap2["flt_st"]["degraded"]
    assert snap2["flt_st"]["n_after"] == snap2["flt_st"]["n_before"] == 3
    # 大小写：小写 st 也剔除（统一 upper 判定）；未登记名称的标的按非 ST 放行（如实语义）
    (repo / "dim" / "security.json").write_text(json.dumps(
        [{"secid": "600000.SH", "name": "正常"}, {"secid": "600001.SH", "name": "st小写"},
         {"secid": "600002.SH", "name": "也正常"}], ensure_ascii=False), encoding="utf-8")
    out3, _ = apply_universe({"flt_st": True}, bars, [d0], repo=repo)
    assert out3[d0] == {"600000.SH", "600002.SH"}


def test_flt_listed_days(tmp_path):
    long_days, short_days = _biz_days(130), _biz_days(50)
    bars = {"A": [{"dt": d, "amount": 1e8} for d in long_days],
            "B": [{"dt": d, "amount": 1e8} for d in short_days]}
    early, late = long_days[100], long_days[129]
    out, snap = apply_universe({"flt_listed_days": 120}, bars, [early, late])
    assert out[early] == set() and out[late] == {"A"}
    # 快照锚点 = 首个启用日（early）：n_before=2 → n_after=0
    assert snap["flt_listed_days"] == {"n_before": 2, "n_after": 0}


def test_flt_avg_amount_20d(tmp_path):
    days = _biz_days(21)
    d0 = days[-1]                                   # 过滤日（严格 dt < d 的过去 20 根）
    bars = {"A": [{"dt": d, "amount": 1e8} for d in days[:-1]],    # 均额 1e8 ≥ 5e7 → 留
            "B": [{"dt": d, "amount": 1e6} for d in days[:-1]],    # 均额不足 → 剔
            "C": [{"dt": d, "amount": 1e9} for d in days[:10]]}    # 样本 <20 → 剔
    out, snap = apply_universe({"flt_avg_amount_20d": 5e7}, bars, [d0])
    assert out[d0] == {"A"}
    assert snap["flt_avg_amount_20d"] == {"n_before": 3, "n_after": 1}


def test_unknown_filter_and_disabled(tmp_path):
    days = _biz_days(25)
    bars = {"A": [{"dt": d, "amount": 1e8} for d in days]}
    d0 = days[-1]
    with pytest.raises(StrategyError, match="未知过滤器"):
        apply_universe({"flt_ghost": 1}, bars, [d0])
    # false / 0 / null = 关闭（不进快照，不过滤）
    out, snap = apply_universe({"flt_st": False, "flt_avg_amount_20d": 0,
                                "flt_listed_days": None}, bars, [d0])
    assert out[d0] == {"A"} and snap == {}


def test_filter_snapshot_one_anchor_per_filter(tmp_path):
    """快照锚点唯一：多日期只记首个启用日，不逐日刷计数。"""
    days = _biz_days(40)
    bars = {"A": [{"dt": d, "amount": 1e8} for d in days],
            "B": [{"dt": d, "amount": 1e8} for d in days]}
    out, snap = apply_universe({"flt_avg_amount_20d": 5e7}, bars, [days[25], days[39]])
    assert snap["flt_avg_amount_20d"]["n_before"] == 2      # 首日锚点，非末日
    assert set(out) == {days[25], days[39]}


# ── CLI validate 冒烟 ─────────────────────────────────────
def test_cli_strategy_validate_smoke(tmp_path, monkeypatch, capsys):
    from applib import cli as CLI
    from applib.ctx import Ctx
    ctx = Ctx(root=str(tmp_path))
    ctx.ensure_sandbox()
    monkeypatch.setattr(CLI, "Ctx", lambda: ctx)
    repo = ctx.local_repo
    # sandbox 内布好最小世界：active 因子 + bars + dim
    d = repo / "factors" / "defs"
    d.mkdir(parents=True, exist_ok=True)
    base = yaml.safe_load(_pkg_yaml("val_pe_ttm").read_text(encoding="utf-8"))
    (d / "val_pe_ttm.yaml").write_text(yaml.dump(base, allow_unicode=True), encoding="utf-8")
    _activate(repo, "val_pe_ttm")
    days = _biz_days(45)
    (repo / "market").mkdir(parents=True, exist_ok=True)
    (repo / "market" / "daily_bars.json").write_text(json.dumps(
        {"600000.SH": [{"dt": x, "amount": 1e8} for x in days],
         "600001.SH": [{"dt": x, "amount": 1e8} for x in days]}, ensure_ascii=False),
        encoding="utf-8")
    (repo / "dim").mkdir(parents=True, exist_ok=True)
    (repo / "dim" / "security.json").write_text(json.dumps(
        [{"secid": "600000.SH", "name": "平安银行"},
         {"secid": "600001.SH", "name": "*ST 退市"}], ensure_ascii=False), encoding="utf-8")
    yml = _yml(tmp_path, """
id: smoke-strategy
factors: [{id: val_pe_ttm, direction: asc}]
composite: {top_n: 1, method: [equal]}
universe:
  base: CN_EQUITY_ALL
  filters: {flt_st: true, flt_avg_amount_20d: 50000000}
""")
    CLI.cmd_strategy(type("A", (), {"action": "validate", "file": str(yml)})())
    out = capsys.readouterr().out
    assert "校验通过" in out and "val_pe_ttm" in out and "过滤器预览" in out
    assert "flt_st" in out and "2 → 1" in out          # ST 过滤生效（2 剔 1 留 1）
    # 校验失败 → SystemExit（含原因）
    bad = _yml(tmp_path, "id: bad\nuniverse: CN_EQUITY_ALL\nfactors: [{id: ghost_x}]\n"
               "composite: {top_n: 1, method: [equal]}\n", name="bad.yaml")
    with pytest.raises(SystemExit, match="不在合成池"):
        CLI.cmd_strategy(type("A", (), {"action": "validate", "file": str(bad)})())
