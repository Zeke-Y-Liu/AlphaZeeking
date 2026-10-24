"""M5 生命周期测试（T-L1 模型层）：hypothesis_id 血缘 + health 健康维度 + 版本语义。

后续 T-L2（激活门禁）/ T-L4（巡检）/ T-L5（复查联动）在本文件续写。

运行：``cd alphapocket && python -m pytest tests/test_factorlib_lifecycle.py -q``
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml

import factorlib.registry as R
from factorlib.registry import (
    FactorMeta,
    RegistryError,
    bump_version,
    load_factor,
    load_registry,
    set_health,
    set_status,
    validate,
)

_STATE = "factors/factor_registry_state.json"
_DAY = "2026-10-11"


def _pkg_yaml(fid: str) -> Path:
    return min((Path(R.__file__).parent / "defs").rglob(f"{fid}.yaml"))


def _read_state(repo: Path) -> dict:
    p = repo / _STATE
    return json.loads(p.read_text(encoding="utf-8")) if p.exists() else {}


def _write_state(repo: Path, state: dict) -> None:
    p = repo / _STATE
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")


def _repo_with_def(tmp_path: Path, fid: str = "val_pe_ttm", **extra) -> Path:
    """伪造 repo：拷贝包内 YAML 定义并合并额外键。"""
    d = tmp_path / "factors" / "defs"
    d.mkdir(parents=True)
    base = yaml.safe_load(_pkg_yaml(fid).read_text(encoding="utf-8"))
    base.update(extra)
    (d / f"{fid}.yaml").write_text(yaml.dump(base, allow_unicode=True), encoding="utf-8")
    return tmp_path


# ── hypothesis_id 血缘（三态：YAML 携带 / 状态覆盖 / 缺省 null）──────
def test_hypothesis_id_from_yaml(tmp_path):
    repo = _repo_with_def(tmp_path, hypothesis_id="H-0001")
    assert load_factor(repo, "val_pe_ttm").hypothesis_id == "H-0001"


def test_hypothesis_id_state_overrides_and_unbinds(tmp_path):
    repo = _repo_with_def(tmp_path, hypothesis_id="H-0001")
    _write_state(repo, {"val_pe_ttm": {"status": "draft", "version": "1.0.0",
                                       "hypothesis_id": "H-0009", "history": []}})
    assert load_factor(repo, "val_pe_ttm").hypothesis_id == "H-0009"   # 状态优先
    _write_state(repo, {"val_pe_ttm": {"status": "draft", "version": "1.0.0",
                                       "hypothesis_id": None, "history": []}})
    assert load_factor(repo, "val_pe_ttm").hypothesis_id is None       # 显式 null = 解绑


def test_hypothesis_id_default_none(tmp_path):
    """包内 8 个 YAML：存量 4 因子未携带 hypothesis_id → 缺省 None（存量零破坏）；
    T-F2 估值 4 因子携带 H-0007..H-0010（假设绑定，激活门禁血缘查依赖）。"""
    by_id = {m.id: m for m in load_registry(tmp_path)}
    for fid in ("val_pe_ttm", "mom_20d_fl", "sig_mom20_rank_fl", "eps_revise_fl"):
        assert by_id[fid].hypothesis_id is None
    for fid, hid in (("val_pb_lf", "H-0007"), ("val_ps_ttm", "H-0008"),
                     ("val_ev_ebitda", "H-0009"), ("val_div_yield", "H-0010")):
        assert by_id[fid].hypothesis_id == hid


# ── health 健康维度（可恢复迁移，两维分离）──────────────────────────
def _prime_health(repo: Path, target: str) -> None:
    """重置到隐式基线（删 health 键）后一步迁到 target（非 healthy 时）。"""
    st = _read_state(repo)
    entry = st.setdefault("val_pe_ttm", {})
    entry.pop("health", None)
    entry.pop("health_history", None)
    _write_state(repo, st)
    if target != "healthy":
        set_health(repo, "val_pe_ttm", target, as_of=_DAY)


def test_health_all_legal_transitions(tmp_path):
    """全部 10 条合法迁移逐对走通（每对独立前置状态；健康链可恢复，非单向）。"""
    legal = [("healthy", "weakening"), ("healthy", "invalid"), ("healthy", "unknown"),
             ("weakening", "healthy"), ("weakening", "invalid"), ("weakening", "unknown"),
             ("invalid", "unknown"),
             ("unknown", "healthy"), ("unknown", "weakening"), ("unknown", "invalid")]
    repo = _repo_with_def(tmp_path)
    for i, (old, new) in enumerate(legal):
        _prime_health(repo, old)
        day = f"2027-{i + 1:02d}-01"                                   # 每对独立日期，避开幂等
        rec = set_health(repo, "val_pe_ttm", new, as_of=day)
        assert (rec["from"], rec["to"]) == (old, new), (old, new)
        st = _read_state(repo)["val_pe_ttm"]
        assert st["health"] == new
        assert st["health_history"][-1]["as_of"] == day


def test_health_invalid_to_healthy_rejected_with_hint(tmp_path):
    repo = _repo_with_def(tmp_path)
    set_health(repo, "val_pe_ttm", "invalid", as_of=_DAY)
    with pytest.raises(RegistryError, match="复查"):
        set_health(repo, "val_pe_ttm", "healthy", as_of="2026-10-12")
    assert set_health(repo, "val_pe_ttm", "unknown", as_of="2026-10-12")["to"] == "unknown"


def test_health_illegal_target_rejected(tmp_path):
    repo = _repo_with_def(tmp_path)
    with pytest.raises(RegistryError, match="目标健康态非法"):
        set_health(repo, "val_pe_ttm", "zombie", as_of=_DAY)


def test_health_first_write_baseline_and_implicit_migration(tmp_path):
    """无 health 键：首写 healthy = 基线落档；首写 weakening = 从隐式 healthy 迁移。"""
    repo = _repo_with_def(tmp_path)
    rec = set_health(repo, "val_pe_ttm", "healthy", by="inspect", why="首巡检基线", as_of=_DAY)
    assert (rec["from"], rec["to"]) == ("healthy", "healthy")
    st = _read_state(repo)["val_pe_ttm"]
    assert st["health"] == "healthy" and st["health_history"][0]["why"] == "首巡检基线"

    repo2 = _repo_with_def(tmp_path / "r2")
    rec2 = set_health(repo2, "val_pe_ttm", "weakening", as_of=_DAY)
    assert (rec2["from"], rec2["to"]) == ("healthy", "weakening")      # 隐式基线迁移


def test_health_idempotent_same_day_and_resets_across_days(tmp_path):
    """同 (from,to,as_of) 幂等跳过；同态跨日追加（时序即信息）。"""
    repo = _repo_with_def(tmp_path)
    r1 = set_health(repo, "val_pe_ttm", "weakening", why="IC 衰减", as_of=_DAY)
    r2 = set_health(repo, "val_pe_ttm", "weakening", why="IC 衰减", as_of=_DAY)
    assert r1 == r2                                                    # 幂等返回既有记录
    assert len(_read_state(repo)["val_pe_ttm"]["health_history"]) == 1
    set_health(repo, "val_pe_ttm", "weakening", why="仍弱化", as_of="2026-10-12")
    hist = _read_state(repo)["val_pe_ttm"]["health_history"]
    assert [h["as_of"] for h in hist] == [_DAY, "2026-10-12"]          # 跨日追加
    assert hist[1]["from"] == "weakening"                              # 同态重申 from=当前


def test_health_not_in_factor_meta(tmp_path):
    """两维分离红线：health 是巡检产物，不进 FactorMeta 静态字段。"""
    repo = _repo_with_def(tmp_path)
    set_health(repo, "val_pe_ttm", "weakening", as_of=_DAY)
    meta = load_factor(repo, "val_pe_ttm")
    assert not hasattr(meta, "health")


def test_health_unknown_factor_rejected(tmp_path):
    repo = _repo_with_def(tmp_path)
    with pytest.raises(RegistryError, match="不存在"):
        set_health(repo, "no_such_factor", "healthy", as_of=_DAY)


# ── 版本语义（三段式 + bump 工作流）─────────────────────────────────
def test_validate_version_three_segment_format():
    base = {"id": "f2_val_x", "name": "x", "category": "F2_fundamental", "factor_type": "alpha",
                "engine": "sql", "direction": "low", "universe": ["CN"], "inputs": ["fl_daily_bar"],
                "lookback_days": 0, "pit": True, "status": "draft", "description": "d", "sql": "SELECT 1"}
    assert validate(FactorMeta(version="1.0.0", **base)) == []
    for bad in ("1", "1.0", "v1.0.0", "1.0.0.1", "", None):
        errs = validate(FactorMeta(version=bad, **base))
        assert any("version" in e for e in errs), bad


def test_bump_version_updates_state_and_load(tmp_path):
    repo = _repo_with_def(tmp_path)
    set_status(repo, "val_pe_ttm", "active", who="t", why="上线", gate={"ok": True, "reasons": []})
    rec = bump_version(repo, "val_pe_ttm", "1.1.0", who="t", why="口径微调")
    assert (rec["from"], rec["to"], rec["type"]) == ("1.0.0", "1.1.0", "version_bump")
    st = _read_state(repo)["val_pe_ttm"]
    assert st["version"] == "1.1.0"
    kinds = [("version_bump" if "type" in h else "status") for h in st["history"]]
    assert kinds == ["status", "version_bump"]                         # type 键区分记录
    assert load_factor(repo, "val_pe_ttm").version == "1.1.0"          # 合并视图生效


def test_bump_version_bad_format_rejected(tmp_path):
    repo = _repo_with_def(tmp_path)
    for bad in ("1", "1.0", "v2.0.0"):
        with pytest.raises(RegistryError, match="三段式"):
            bump_version(repo, "val_pe_ttm", bad)


def test_bump_version_same_and_history_duplicate_rejected(tmp_path):
    repo = _repo_with_def(tmp_path)
    with pytest.raises(RegistryError, match="无升级"):
        bump_version(repo, "val_pe_ttm", "1.0.0")                      # 与现值相同
    bump_version(repo, "val_pe_ttm", "1.1.0")
    bump_version(repo, "val_pe_ttm", "1.2.0")
    with pytest.raises(RegistryError, match="重复"):
        bump_version(repo, "val_pe_ttm", "1.1.0")                      # 与历史曾用重复


# ── 向后兼容与存量回归 ──────────────────────────────────────────────
def test_backward_compat_old_state_schema(tmp_path):
    """旧 schema（无 health/hypothesis_id/health_history 键）加载与写入均正常。"""
    repo = _repo_with_def(tmp_path)
    _write_state(repo, {"val_pe_ttm": {"status": "active", "version": "1.0.0",
                                       "history": [{"from": "draft", "to": "active",
                                                    "who": "m2-smoke", "why": "冒烟",
                                                    "at": "2026-10-10T04:17:27"}]}})
    meta = load_factor(repo, "val_pe_ttm")
    assert (meta.status, meta.version, meta.hypothesis_id) == ("active", "1.0.0", None)
    rec = set_health(repo, "val_pe_ttm", "weakening", as_of=_DAY)      # 首写正常迁移
    assert rec["from"] == "healthy"
    st = _read_state(repo)["val_pe_ttm"]
    assert st["health"] == "weakening" and len(st["health_history"]) == 1
    assert st["history"][0]["why"] == "冒烟"                            # 既有 history 不动


def test_load_registry_regression_package_defs(tmp_path):
    """存量回归：23 YAML 加载（存量 4 + T-F2 估值 4 + T-F3 质量 5 + T-F4 成长 4 +
    T-F5 动量/低波红利 6）；status=draft、validate 全过、版本三段式合法、
    假设绑定按 YAML 声明落档。"""
    metas = load_registry(tmp_path)
    assert [m.id for m in metas] == sorted(
        ("val_pe_ttm", "mom_20d_fl", "sig_mom20_rank_fl", "eps_revise_fl",
         "val_pb_lf", "val_ps_ttm", "val_ev_ebitda", "val_div_yield",
         "qua_roe_ttm", "qua_gross_margin", "qua_accruals",
         "qua_ocf_to_np", "qua_debt_to_asset",
         "gro_rev_yoy", "gro_np_yoy", "gro_rev_cagr_3y", "gro_stability",
         "mom_12_1", "mom_6_1", "mom_industry_adj",
         "low_vol_252d", "low_beta", "div_payout_streak"))
    _hid = {"val_pb_lf": "H-0007", "val_ps_ttm": "H-0008",
            "val_ev_ebitda": "H-0009", "val_div_yield": "H-0010",
            "qua_roe_ttm": "H-0011", "qua_gross_margin": "H-0012",
            "qua_accruals": "H-0013", "qua_ocf_to_np": "H-0014",
            "qua_debt_to_asset": "H-0015",
            "gro_rev_yoy": "H-0016", "gro_np_yoy": "H-0017",
            "gro_rev_cagr_3y": "H-0018", "gro_stability": "H-0019",
            "mom_12_1": "H-0020", "mom_6_1": "H-0021", "mom_industry_adj": "H-0022",
            "low_vol_252d": "H-0023", "low_beta": "H-0024", "div_payout_streak": "H-0025"}
    for m in metas:
        assert m.status == "draft" and m.version == "1.0.0"
        assert m.hypothesis_id == _hid.get(m.id)
        assert validate(m) == []


# ══ T-L2 激活门禁（③④⑤⑥ + 血缘，无旁路）═════════════════════════
import duckdb

from factorlib.lifecycle import activate_factor, activation_gate

_LIN, _VER = "L1", "1.0.0"
_OK_GATES = {   # 合格治理记录（M4 落库形态）：environment/multiplicity pass=NULL（M4 未显式判定）
    "time_robustness": (True, "{}"),
    "param_robustness": (True, "{}"),
    "environment": (None, "{}"),
    "incremental_value": (True, "{}"),
    "multiplicity": (None, '{"bonferroni": {"p": 0.02, "n_trials": 4, "p_adj": 0.08}}'),
}


def _gate_db(tmp_path: Path, *, materialized=True, eval_row=True, gates=None,
             lineage: str = _LIN, report_lineage: str | None = None,
             n_periods=21, dm=0.137) -> Path:
    """手建 fl 三表 + 合格治理记录（聚焦门禁判定逻辑本身，不跑真 prepare/eval）。

    lineage = 物化行 lineage；report_lineage = eval/gate 治理记录 lineage（缺省同 lineage），
    二者分离用于 lineage 守卫测试（旧版报告不放行新版定义）。
    """
    rep_lin = report_lineage or lineage
    db = tmp_path / "fl.duckdb"
    db.parent.mkdir(parents=True, exist_ok=True)
    con = duckdb.connect(str(db))
    con.execute("CREATE TABLE fl_factor_value_daily (dt DATE, secid TEXT, factor_id TEXT, "
                "value DOUBLE, factor_version TEXT, lineage_id TEXT)")
    if materialized:
        con.execute("INSERT INTO fl_factor_value_daily VALUES "
                    "('2026-09-30','600001','val_pe_ttm',12.3,?,?)", [_VER, lineage])
    con.execute("""CREATE TABLE fl_factor_eval (eval_id TEXT, factor_id TEXT,
                   factor_version TEXT, lineage_id TEXT, as_of TEXT, code_version TEXT,
                   horizon INTEGER, n_periods INTEGER, ic_mean DOUBLE, ic_ir DOUBLE,
                   nw_t DOUBLE, directed_mean DOUBLE, top_excess DOUBLE,
                   monotonicity DOUBLE, top_turnover DOUBLE, report_json TEXT,
                   created_at TEXT)""")
    if eval_row:
        con.execute("INSERT INTO fl_factor_eval VALUES "
                    "('E1','val_pe_ttm',?,?,?,?,1,?,0.15,0.5,2.5,?,0.04,-0.9,0.3,NULL,'t')",
                    [_VER, rep_lin, "2026-09-30", None, n_periods, dm])
    con.execute("""CREATE TABLE fl_factor_gate (gate_id TEXT, factor_id TEXT,
                   factor_version TEXT, lineage_id TEXT, as_of TEXT, code_version TEXT,
                   gate TEXT, pass BOOLEAN, detail_json TEXT, created_at TEXT)""")
    for name, (p, dj) in (gates if gates is not None else _OK_GATES).items():
        con.execute("INSERT INTO fl_factor_gate VALUES (?,?,?,?,?,?,?,?,?,?)",
                    [f"G-{name}", "val_pe_ttm", _VER, rep_lin, "2026-09-30",
                     None, name, p, dj, "t"])
    con.close()
    return db


def _gate_repo(tmp_path: Path, *, hyp: str | None = "H-0001") -> Path:
    """repo：因子定义 + 状态绑定血缘 + 假设登记表。"""
    repo = _repo_with_def(tmp_path)
    entry = {"status": "draft", "version": _VER, "history": []}
    if hyp:
        entry["hypothesis_id"] = hyp
    _write_state(repo, {"val_pe_ttm": entry})
    hp = repo / "hypothesis" / "registry.json"
    hp.parent.mkdir(parents=True, exist_ok=True)
    hp.write_text(json.dumps({"hypotheses": [{"id": "H-0001", "title": "估值效应"}]},
                             ensure_ascii=False), encoding="utf-8")
    return repo


def test_activation_gate_all_pass_and_activate(tmp_path):
    db = _gate_db(tmp_path)
    repo = _gate_repo(tmp_path / "r")
    g = activation_gate(db, repo, "val_pe_ttm")
    assert g["ok"] is True and g["reasons"] == []
    assert set(g["checks"]) == {"eval", "time", "param", "environment",
                                "incremental", "multiplicity", "lineage_hyp"}
    assert g["checks"]["environment"]["soft"] is True
    res = activate_factor(db, repo, "val_pe_ttm", who="t", why="门禁验收")
    assert res["activated"] is True
    st = _read_state(repo)["val_pe_ttm"]
    assert st["status"] == "active" and st["history"][-1]["who"] == "t"


def test_activation_set_status_no_bypass(tmp_path):
    """红线：裸 set_status(draft→active) 拒绝 + 错误消息指路唯一入口。"""
    repo = _gate_repo(tmp_path)
    with pytest.raises(RegistryError, match="ap fl activate"):
        set_status(repo, "val_pe_ttm", "active", who="hacker")


def test_activation_set_status_rejects_failed_gate(tmp_path):
    repo = _gate_repo(tmp_path)
    with pytest.raises(RegistryError, match="门禁未过"):
        set_status(repo, "val_pe_ttm", "active", gate={"ok": False, "reasons": ["x"]})


def test_activation_deprecated_needs_no_gate(tmp_path):
    """active→deprecated 退役自由（门禁只拦 draft→active）。"""
    repo = _gate_repo(tmp_path)
    set_status(repo, "val_pe_ttm", "active", gate={"ok": True, "reasons": []})
    rec = set_status(repo, "val_pe_ttm", "deprecated", who="t", why="退役")
    assert rec["to"] == "deprecated"


def test_gate_missing_eval_rejected(tmp_path):
    db = _gate_db(tmp_path, eval_row=False)
    g = activation_gate(db, _gate_repo(tmp_path / "r"), "val_pe_ttm")
    assert g["ok"] is False and any("eval" in r for r in g["reasons"])


def test_gate_eval_sample_too_small(tmp_path):
    db = _gate_db(tmp_path, n_periods=5)                       # < 12 三年月频下限
    g = activation_gate(db, _gate_repo(tmp_path / "r"), "val_pe_ttm")
    assert g["checks"]["eval"]["ok"] is False
    assert any("eval" in r for r in g["reasons"])


def test_gate_eval_negative_directed(tmp_path):
    db = _gate_db(tmp_path, dm=-0.1)
    g = activation_gate(db, _gate_repo(tmp_path / "r"), "val_pe_ttm")
    assert g["checks"]["eval"]["ok"] is False


def test_gate_missing_time_row_rejected(tmp_path):
    """红线：没跑过治理（缺行）一律拒绝。"""
    gates = {k: v for k, v in _OK_GATES.items() if k != "time_robustness"}
    db = _gate_db(tmp_path, gates=gates)
    g = activation_gate(db, _gate_repo(tmp_path / "r"), "val_pe_ttm")
    assert g["checks"]["time"]["ok"] is False and any("time" in r for r in g["reasons"])


def test_gate_time_fail_reasons(tmp_path):
    gates = {**_OK_GATES, "time_robustness": (False, '{"decay": 0.3}')}
    db = _gate_db(tmp_path, gates=gates)
    g = activation_gate(db, _gate_repo(tmp_path / "r"), "val_pe_ttm")
    assert g["ok"] is False and any("时间关" in r for r in g["reasons"])


def test_gate_param_none_rejected(tmp_path):
    """参数关硬且非 None：未跑变体网格不许激活。"""
    gates = {**_OK_GATES, "param_robustness": (None, "{}")}
    db = _gate_db(tmp_path, gates=gates)
    g = activation_gate(db, _gate_repo(tmp_path / "r"), "val_pe_ttm")
    assert g["checks"]["param"]["ok"] is False
    assert any("变体网格" in r for r in g["reasons"])


def test_gate_environment_fail_is_soft(tmp_path):
    """环境关软：pass=False 列示失效环境但不阻断（计划 7.3 局部失效可解释）。"""
    gates = {**_OK_GATES,
             "environment": (False, '{"failed_environments": ["bear"]}')}
    db = _gate_db(tmp_path, gates=gates)
    g = activation_gate(db, _gate_repo(tmp_path / "r"), "val_pe_ttm")
    assert g["ok"] is True                                     # 软检查不进 reasons
    assert g["checks"]["environment"]["ok"] is False
    assert "bear" in g["checks"]["environment"]["detail"]


def test_gate_incremental_cold_start_none(tmp_path):
    """首因子冷启动：incremental pass=None（无既有因子可比）通过。"""
    gates = {**_OK_GATES, "incremental_value": (None, '{"reasons": ["无既有因子可比"]}')}
    db = _gate_db(tmp_path, gates=gates)
    g = activation_gate(db, _gate_repo(tmp_path / "r"), "val_pe_ttm")
    assert g["checks"]["incremental"]["ok"] is True
    assert "冷启动" in g["checks"]["incremental"]["detail"]


def test_gate_incremental_fail(tmp_path):
    gates = {**_OK_GATES, "incremental_value": (False, "{}")}
    db = _gate_db(tmp_path, gates=gates)
    g = activation_gate(db, _gate_repo(tmp_path / "r"), "val_pe_ttm")
    assert g["ok"] is False and any("incremental" in r for r in g["reasons"])


def test_gate_multiplicity_high_trials_requires_padj(tmp_path):
    """⑥：候选 ≥20 须 Bonferroni p_adj<0.05；超线拒绝。"""
    repo = _gate_repo(tmp_path / "r")
    hi = '{"bonferroni": {"p": 0.04, "n_trials": 25, "p_adj": 1.0}}'
    g = activation_gate(_gate_db(tmp_path, gates={**_OK_GATES,
                                                  "multiplicity": (None, hi)}),
                        repo, "val_pe_ttm")
    assert g["checks"]["multiplicity"]["ok"] is False
    lo = '{"bonferroni": {"p": 0.001, "n_trials": 25, "p_adj": 0.025}}'
    g2 = activation_gate(_gate_db(tmp_path / "db2", gates={**_OK_GATES,
                                                           "multiplicity": (None, lo)}),
                         _gate_repo(tmp_path / "r2"), "val_pe_ttm")
    assert g2["checks"]["multiplicity"]["ok"] is True


def test_gate_multiplicity_missing_rejected(tmp_path):
    gates = {k: v for k, v in _OK_GATES.items() if k != "multiplicity"}
    db = _gate_db(tmp_path, gates=gates)
    g = activation_gate(db, _gate_repo(tmp_path / "r"), "val_pe_ttm")
    assert g["checks"]["multiplicity"]["ok"] is False


def test_gate_hypothesis_dangling_and_unbound(tmp_path):
    """血缘硬检查：悬空（不在登记表）与未绑定均拒。"""
    db = _gate_db(tmp_path / "db")
    g = activation_gate(db, _gate_repo(tmp_path / "r1", hyp="H-9999"), "val_pe_ttm")
    assert g["checks"]["lineage_hyp"]["ok"] is False and "悬空" in g["checks"]["lineage_hyp"]["detail"]
    g2 = activation_gate(db, _gate_repo(tmp_path / "r2", hyp=None), "val_pe_ttm")
    assert g2["checks"]["lineage_hyp"]["ok"] is False and "未绑定" in g2["checks"]["lineage_hyp"]["detail"]


def test_gate_lineage_guard_old_report_not_accepted(tmp_path):
    """lineage 守卫：物化 lineage=L2，治理记录全在 L1 → 旧版报告不给新版放行。"""
    db = _gate_db(tmp_path, lineage="L2", report_lineage=_LIN)
    g = activation_gate(db, _gate_repo(tmp_path / "r"), "val_pe_ttm")
    assert g["ok"] is False
    assert any("eval" in r for r in g["reasons"]) and any("time" in r for r in g["reasons"])


def test_gate_not_materialized_rejected(tmp_path):
    db = _gate_db(tmp_path, materialized=False)
    g = activation_gate(db, _gate_repo(tmp_path / "r"), "val_pe_ttm")
    assert g["checks"]["eval"]["ok"] is False and "未物化" in g["checks"]["eval"]["detail"]


def test_gate_reasons_report_all_at_once(tmp_path):
    """一次报全不挤牙膏：多因同时失败时 reasons 汇总全部硬检查拒因。"""
    gates = {**_OK_GATES, "time_robustness": (False, "{}"),
             "param_robustness": (None, "{}"), "incremental_value": (False, "{}")}
    db = _gate_db(tmp_path, gates=gates, eval_row=False)
    g = activation_gate(db, _gate_repo(tmp_path / "r", hyp="H-9999"), "val_pe_ttm")
    tags = {r.split(":")[0] for r in g["reasons"]}
    assert {"eval", "time", "param", "incremental", "lineage_hyp"} <= tags


def test_activate_factor_only_draft(tmp_path):
    db = _gate_db(tmp_path)
    repo = _gate_repo(tmp_path / "r")
    set_status(repo, "val_pe_ttm", "active", gate={"ok": True, "reasons": []})
    res = activate_factor(db, repo, "val_pe_ttm")
    assert res["activated"] is False and "仅 draft" in res["reason"]


# ══ T-L3 版本冻结与被引用关系（旧版不可变 + pin + 影响面）══════════
import hashlib

from applib.factor_backtest import (
    FactorBacktestError,
    load_section,
    run_factor_backtest,
)
from applib.factor_eval_fl import FactorEvalError, run_factor_eval
from factorlib.lifecycle import impact_report, refs
from factorlib.materialize import (
    MaterializeError,
    definition_fingerprint,
    latest_version,
    materialize,
    query,
)

_BT_SPEC = {"id": "fl_pin_top3", "name": "pin 对账", "top_n": 3, "rebalance": "monthly",
            "benchmark": "510300.SH",
            "factors": [{"id": "val_pe_ttm", "direction": "asc", "weight": 1.0}]}


def _row_digest(rows: list[dict]) -> str:
    """物化行逐位对账摘要（dt|secid|value|version|lineage 全参与）。"""
    payload = json.dumps(sorted(json.dumps(r, sort_keys=True, default=str) for r in rows))
    return hashlib.sha256(payload.encode()).hexdigest()


def test_latest_version_numeric_order():
    """三段式数值序：1.10.0 > 1.9.0（split_part 消除零填充字典序歧义）。"""
    con = duckdb.connect(":memory:")
    try:
        con.execute("CREATE TABLE fl_factor_value_daily (dt DATE, secid TEXT, factor_id TEXT, "
                    "value DOUBLE, factor_version TEXT, lineage_id TEXT)")
        assert latest_version(con, "f") is None                 # 空表
        for v in ("1.9.0", "1.10.0", "2.0.0"):
            con.execute("INSERT INTO fl_factor_value_daily VALUES "
                        "('2026-01-01','S','f',1.0,?, 'L')", [v])
        assert latest_version(con, "f") == "2.0.0"
        con.execute("DELETE FROM fl_factor_value_daily WHERE factor_version = '2.0.0'")
        assert latest_version(con, "f") == "1.10.0"             # 非零填充也正确
    finally:
        con.close()


def test_bump_full_recompute_old_rows_frozen(fl_db, fl_repo):
    """升版全量重算后旧版行逐位不变（count 翻倍 + v1 行 digest 对账），登记表双版本在案。"""
    db, _ = fl_db
    meta = load_factor(fl_repo, "val_pe_ttm")
    materialize(meta, db, mode="full")
    rows_v1 = query(db, "val_pe_ttm")
    dig_v1 = _row_digest(rows_v1)
    assert all(r["factor_version"] == "1.0.0" for r in rows_v1)

    bump_version(fl_repo, "val_pe_ttm", "1.1.0", who="t", why="口径微调")
    s2 = materialize(load_factor(fl_repo, "val_pe_ttm"), db, mode="full")
    assert s2["version"] == "1.1.0"

    rows_all = query(db, "val_pe_ttm")
    v1 = [r for r in rows_all if r["factor_version"] == "1.0.0"]
    v2 = [r for r in rows_all if r["factor_version"] == "1.1.0"]
    assert len(v1) * 2 == len(rows_all) and len(v2) == len(v1)
    assert _row_digest(v1) == dig_v1                            # 旧版行含 lineage 逐位不变
    strip = lambda rs: sorted((r["dt"], r["secid"], r["value"]) for r in rs)
    assert strip(v2) == strip(v1)                               # 同定义同输入 → 值一致
    assert {r["lineage_id"] for r in v1}.isdisjoint({r["lineage_id"] for r in v2})

    con = duckdb.connect(str(db), read_only=True)
    try:
        reg = con.execute("SELECT factor_version, definition_fp FROM fl_factor_lineage "
                          "WHERE factor_id='val_pe_ttm' ORDER BY factor_version").fetchall()
    finally:
        con.close()
    assert [r[0] for r in reg] == ["1.0.0", "1.1.0"]            # 两版本定义指纹各自登记


def test_same_version_definition_drift_rejected(fl_db, fl_repo):
    """同 version 定义文本漂移 → MaterializeError（口径变更必须升版），旧行原封不动。"""
    db, _ = fl_db
    meta_old = load_factor(fl_repo, "val_pe_ttm")
    materialize(meta_old, db, mode="full")
    fp_old = definition_fingerprint(meta_old)
    before = query(db, "val_pe_ttm")
    before_count = len(before)

    d = fl_repo / "factors" / "defs"                            # repo 内定义覆盖（真源切换）
    d.mkdir(parents=True)
    base = yaml.safe_load(_pkg_yaml("val_pe_ttm").read_text(encoding="utf-8"))
    base["sql"] = base["sql"] + "\n-- 口径微调（未升版 = 冻结违例）"
    (d / "val_pe_ttm.yaml").write_text(yaml.dump(base, allow_unicode=True), encoding="utf-8")

    meta = load_factor(fl_repo, "val_pe_ttm")
    assert meta.version == "1.0.0"                              # 同 version
    assert definition_fingerprint(meta) != fp_old               # 定义指纹随文本变化
    with pytest.raises(MaterializeError, match="升版"):
        materialize(meta, db, mode="full")
    after = query(db, "val_pe_ttm")
    assert len(after) == before_count and _row_digest(after) == _row_digest(before)


def test_input_fingerprint_drift_allowed(fl_db, fl_repo):
    """数据面漂移（新 K 线入库）→ 定义指纹不变 → full 放行，同版本段 lineage 统一更新。"""
    db, _ = fl_db
    s1 = materialize(load_factor(fl_repo, "val_pe_ttm"), db, mode="full")
    con = duckdb.connect(str(db))
    try:
        con.execute("INSERT INTO fl_daily_bar VALUES ('600099.SH', DATE '2021-06-14', "
                    "1,1,1,1,1,1,0.0)")
    finally:
        con.close()
    s2 = materialize(load_factor(fl_repo, "val_pe_ttm"), db, mode="full")
    assert s2["version"] == "1.0.0" and s2["lineage_id"] != s1["lineage_id"]
    rows = query(db, "val_pe_ttm")
    assert {r["lineage_id"] for r in rows} == {s2["lineage_id"]}  # 段内统一为最新 lineage


def _add_day(db, day: str, n: int = 10) -> None:
    """模拟新交易日：n 根平台 K 线 + 日历（test_factorlib_materialize 同形态）。"""
    con = duckdb.connect(str(db))
    try:
        for i in range(1, n + 1):
            close = 11.0 + i
            con.execute("INSERT INTO fl_daily_bar VALUES (?,?,?,?,?,?,?,?,?)",
                        (f"6000{i:02d}.SH", day, close, close, close, close,
                         10000, close * 10000, 0.0))
        con.execute("INSERT INTO fl_trade_calendar VALUES (?)", [day])
    finally:
        con.close()


def test_incremental_watermark_per_version(fl_db, fl_repo):
    """增量水位按版本隔离：升版后新版本段从 cal_lo 起算（不从旧版水位续），旧段不动。"""
    db, _ = fl_db
    materialize(load_factor(fl_repo, "val_pe_ttm"), db, mode="full")
    n_v1 = len(query(db, "val_pe_ttm"))

    _add_day(db, "2021-07-01")
    bump_version(fl_repo, "val_pe_ttm", "1.1.0", who="t", why="升版")
    s = materialize(load_factor(fl_repo, "val_pe_ttm"), db, mode="incremental")
    assert s["start"] == "2021-01-04"                           # 新版本段从日历起点起算
    v1 = [r for r in query(db, "val_pe_ttm") if r["factor_version"] == "1.0.0"]
    v2 = [r for r in query(db, "val_pe_ttm") if r["factor_version"] == "1.1.0"]
    assert len(v1) == n_v1 and len(v2) == n_v1 + 9              # S07 亏损缺席 → +9
    assert {str(r["dt"]) for r in v2} >= {"2021-07-01"}         # 新段含新交易日
    assert max(str(r["dt"]) for r in v1) == "2021-06-30"        # 旧段止于旧日历末

    _add_day(db, "2021-07-02")
    s2 = materialize(load_factor(fl_repo, "val_pe_ttm"), db, mode="incremental")
    assert s2["start"] == "2021-07-02"                          # 从当前版本段水位续算
    v2b = [r for r in query(db, "val_pe_ttm") if r["factor_version"] == "1.1.0"]
    assert len([r for r in v2b if str(r["dt"]) == "2021-07-02"]) == 9
    assert len([r for r in query(db, "val_pe_ttm")
                if r["factor_version"] == "1.0.0"]) == n_v1     # 旧版本段始终冻结


def test_backtest_pin_old_version_bit_identical(fl_db, fl_repo):
    """DoD 核心：物化 v1.1 → 升 v1.2 重算 → pin v1.1 重跑回测，series/metrics/cost 逐字节一致。"""
    db, _ = fl_db
    repo = fl_repo
    materialize(load_factor(repo, "val_pe_ttm"), db, mode="full")
    bt1 = run_factor_backtest(db, repo, _BT_SPEC, start="2021-03-01", end="2021-06-30")

    bump_version(repo, "val_pe_ttm", "1.1.0", who="t", why="升版")
    materialize(load_factor(repo, "val_pe_ttm"), db, mode="full")
    bt2 = run_factor_backtest(db, repo, {**_BT_SPEC, "factor_version": "1.0.0"},
                              start="2021-03-01", end="2021-06-30")
    # spec 元数据（factor_version 显式 pin 与否）天然不同 → 剔除后逐字节 diff
    strip_meta = lambda bt: {k: v for k, v in bt.items() if k != "factor_spec"}
    assert json.dumps(strip_meta(bt1), sort_keys=True, default=str) == \
        json.dumps(strip_meta(bt2), sort_keys=True, default=str)
    assert bt2["factor_lineages"]["val_pe_ttm"]["version"] == "1.0.0"
    assert bt2["pit_audit"]["check"] == "pass"


def test_backtest_default_latest_and_multi_version_unique_rows(fl_db, fl_repo):
    """默认取最新版且取行唯一（多版本并存不混合）；同定义升版 → 选股/净值与旧版一致。"""
    db, _ = fl_db
    repo = fl_repo
    materialize(load_factor(repo, "val_pe_ttm"), db, mode="full")
    bt1 = run_factor_backtest(db, repo, _BT_SPEC, start="2021-03-01", end="2021-06-30")
    bump_version(repo, "val_pe_ttm", "1.1.0", who="t", why="升版")
    materialize(load_factor(repo, "val_pe_ttm"), db, mode="full")

    bt3 = run_factor_backtest(db, repo, _BT_SPEC, start="2021-03-01", end="2021-06-30")
    assert bt3["factor_lineages"]["val_pe_ttm"]["version"] == "1.1.0"
    assert bt3["factor_lineages"]["val_pe_ttm"]["lineage_id"] != \
        bt1["factor_lineages"]["val_pe_ttm"]["lineage_id"]
    strip = lambda bt: [(t["dt"], sorted(t["secids"])) for t in bt["factor_targets"]]
    assert strip(bt3) == strip(bt1)                             # 同定义同输入 → 同选股
    assert bt3["series"] == bt1["series"] and bt3["strategy_metrics"] == bt1["strategy_metrics"]

    con = duckdb.connect(str(db), read_only=True)
    try:
        sec = load_section(con, "val_pe_ttm", "2021-03-31")     # 默认最新 → 单版本取行
    finally:
        con.close()
    assert len(sec) == 10                                       # 不因双版本翻倍


def test_backtest_pin_empty_version_rejected(fl_db, fl_repo):
    db, _ = fl_db
    repo = fl_repo
    materialize(load_factor(repo, "val_pe_ttm"), db, mode="full")
    with pytest.raises(FactorBacktestError, match="pin 版本无行"):
        run_factor_backtest(db, repo, {**_BT_SPEC, "factor_version": "9.9.9"},
                            start="2021-03-01", end="2021-06-30")


def test_eval_pin_version_and_missing_rejected(fl_db, fl_repo):
    """检验消费端 pin：显式版本走旧版行；缺省取最新；空版本拒绝出报告。"""
    db, _ = fl_db
    repo = fl_repo
    materialize(load_factor(repo, "val_pe_ttm"), db, mode="full")
    bump_version(repo, "val_pe_ttm", "1.1.0", who="t", why="升版")
    materialize(load_factor(repo, "val_pe_ttm"), db, mode="full")

    rep = run_factor_eval(db, repo, "val_pe_ttm", "asc", version="1.0.0")
    assert rep["factor_version"] == "1.0.0"
    rep_new = run_factor_eval(db, repo, "val_pe_ttm", "asc")
    assert rep_new["factor_version"] == "1.1.0"
    assert rep["reports"][1]["ic"]["series"] == \
        rep_new["reports"][1]["ic"]["series"]                   # 同定义同输入 → IC 一致
    with pytest.raises(FactorEvalError, match="未物化"):
        run_factor_eval(db, repo, "val_pe_ttm", "asc", version="9.9.9")


def _refs_db(tmp_path: Path) -> tuple[Path, Path]:
    """手建 value/eval/gate 三表（双版本）+ artifacts 回测产物，供 refs/impact 聚合断言。"""
    db = tmp_path / "fl.duckdb"
    repo = tmp_path / "repo"
    repo.mkdir(parents=True, exist_ok=True)
    con = duckdb.connect(str(db))
    con.execute("CREATE TABLE fl_factor_value_daily (dt DATE, secid TEXT, factor_id TEXT, "
                "value DOUBLE, factor_version TEXT, lineage_id TEXT)")
    con.executemany("INSERT INTO fl_factor_value_daily VALUES (?,?,?,?,?,?)", [
        ("2026-01-30", "600001", "val_pe_ttm", 10.0, "1.0.0", "LA1"),
        ("2026-01-30", "600002", "val_pe_ttm", 11.0, "1.0.0", "LA1"),
        ("2026-02-27", "600001", "val_pe_ttm", 10.5, "1.0.0", "LA1"),
        ("2026-01-30", "600001", "val_pe_ttm", 10.1, "1.1.0", "LA2"),
        ("2026-02-27", "600001", "val_pe_ttm", 10.6, "1.1.0", "LA2")])
    con.execute("""CREATE TABLE fl_factor_eval (eval_id TEXT, factor_id TEXT,
                   factor_version TEXT, lineage_id TEXT, as_of TEXT, code_version TEXT,
                   horizon INTEGER, n_periods INTEGER, ic_mean DOUBLE, ic_ir DOUBLE,
                   nw_t DOUBLE, directed_mean DOUBLE, top_excess DOUBLE,
                   monotonicity DOUBLE, top_turnover DOUBLE, report_json TEXT,
                   created_at TEXT)""")
    con.executemany("INSERT INTO fl_factor_eval VALUES (?,?,?,?,?,NULL,?,21,0.15,0.5,2.5,"
                    "0.14,0.04,NULL,0.3,NULL,'t')", [
        ("E1", "val_pe_ttm", "1.0.0", "LA1", "2026-09-30", 1),
        ("E3", "val_pe_ttm", "1.0.0", "LA1", "2026-09-30", 3),
        ("E6", "val_pe_ttm", "1.0.0", "LA1", "2026-09-30", 6),
        ("E1b", "val_pe_ttm", "1.1.0", "LA2", "2026-10-10", 1)])
    con.execute("""CREATE TABLE fl_factor_gate (gate_id TEXT, factor_id TEXT,
                   factor_version TEXT, lineage_id TEXT, as_of TEXT, code_version TEXT,
                   gate TEXT, pass BOOLEAN, detail_json TEXT, created_at TEXT)""")
    con.executemany("INSERT INTO fl_factor_gate VALUES (?,?,?,?,?,NULL,?,?,?, 't')", [
        ("G1", "val_pe_ttm", "1.0.0", "LA1", "2026-09-30", "time_robustness", False, "{}"),
        ("G2", "val_pe_ttm", "1.0.0", "LA1", "2026-09-30", "param_robustness", True, "{}"),
        ("G3", "val_pe_ttm", "1.1.0", "LA2", "2026-10-10", "time_robustness", True, "{}")])
    con.close()
    adir = tmp_path / "artifacts" / "run_1"                     # repo.parent/artifacts
    adir.mkdir(parents=True)
    (adir / "backtest_p1.json").write_text(json.dumps({
        "id": "strat_pe", "factor_spec": {"factors": [{"id": "val_pe_ttm"}]},
        "factor_lineages": {"val_pe_ttm": {"version": "1.0.0", "lineage_id": "LA1"}}}),
        encoding="utf-8")
    (adir / "backtest_p2.json").write_text(json.dumps({
        "id": "strat_other", "factor_spec": {"factors": [{"id": "mom_20d_fl"}]}}),
        encoding="utf-8")
    (adir / "backtest_bad.json").write_text("{broken", encoding="utf-8")   # 坏文件透明跳过
    return db, repo


def test_refs_aggregate_and_version_filter(tmp_path):
    db, repo = _refs_db(tmp_path)
    r = refs(db, repo, "val_pe_ttm")
    assert r["version"] == "1.1.0"                              # 展示锚点 = 最新版
    assert len(r["evals"]) == 4 and len(r["gates"]) == 3        # 全版本聚合
    assert {v["version"]: v["rows"] for v in r["values"]} == {"1.0.0": 3, "1.1.0": 2}
    v10 = next(v for v in r["values"] if v["version"] == "1.0.0")
    assert v10["dt_range"] == ["2026-01-30", "2026-02-27"]
    assert [b["strategy"] for b in r["backtests"]] == ["strat_pe"]   # 坏文件/他因子跳过

    r10 = refs(db, repo, "val_pe_ttm", version="1.0.0")
    assert r10["version"] == "1.0.0"
    assert len(r10["evals"]) == 3 and len(r10["gates"]) == 2    # 等值收窄
    assert [v["rows"] for v in r10["values"]] == [3]
    assert [b["strategy"] for b in r10["backtests"]] == ["strat_pe"]  # pin v1.0.0 仍命中


def test_impact_report_renders_and_persists(tmp_path):
    db, repo = _refs_db(tmp_path)
    out = impact_report(db, repo, "val_pe_ttm", out_dir=tmp_path / "artifacts")
    assert out["n_rows"] == 5 and out["n_strategies"] == 1
    assert "影响面评估" in out["md"] and "strat_pe" in out["md"]
    adir = Path(out["artifact_dir"])
    assert adir.exists() and (adir / "impact.md").exists()
    rj = json.loads((adir / "refs.json").read_text(encoding="utf-8"))
    assert rj["factor_id"] == "val_pe_ttm" and len(rj["values"]) == 2
    assert "fl_refs_val_pe_ttm_" in adir.name                   # 落盘目录契约形态


# ══ T-L4 季度巡检：judge_health 三指标三态真值表（纯函数直测，零 IO）══
from factorlib.lifecycle import judge_health


def _m(decay=None, recent=None, cov=None, bull=None, bear=None) -> dict:
    return {"ic_decay": decay, "ic_recent_mean": recent, "coverage_ratio": cov,
            "regime_fail": {"bull": bull, "bear": bear}}


def test_judge_health_all_good():
    j = judge_health(_m(decay=0.9, recent=0.15, cov=1.0, bull=False, bear=False))
    assert j["health"] == "healthy" and j["reasons"] == []


def test_judge_health_ic_decay_weakening():
    """指标1：衰减比 < 0.5 → weakening（近窗均值仍 > 0，不到 invalid）。"""
    j = judge_health(_m(decay=0.25, recent=0.05, cov=1.0, bull=False, bear=False))
    assert j["health"] == "weakening"
    assert any("IC 衰减比" in r for r in j["reasons"])


def test_judge_health_recent_mean_le_zero_invalid():
    """指标1 invalid 线：近窗 directed IC 均值 ≤ 0 → invalid（衰减比同时触发也归并）。"""
    j = judge_health(_m(decay=-0.5, recent=-0.01, cov=1.0, bull=False, bear=False))
    assert j["health"] == "invalid"
    assert any("≤ 0" in r for r in j["reasons"])


def test_judge_health_coverage_drift_weakening_only():
    """指标2：覆盖度比 < 0.8 → weakening（数据断供是工程问题，永不判 invalid）。"""
    j = judge_health(_m(decay=0.95, recent=0.1, cov=0.6, bull=False, bear=False))
    assert j["health"] == "weakening"
    assert any("覆盖度" in r for r in j["reasons"])


def test_judge_health_single_regime_fail_weakening():
    """指标3：单段失效 → weakening（局部失效带出待答项）。"""
    j = judge_health(_m(decay=0.95, recent=0.1, cov=1.0, bull=True, bear=None))
    assert j["health"] == "weakening" and any("bull" in r for r in j["reasons"])


def test_judge_health_all_regimes_fail_invalid():
    """指标3 invalid 线：牛熊全段失效 → invalid。"""
    j = judge_health(_m(decay=0.95, recent=0.1, cov=1.0, bull=True, bear=True))
    assert j["health"] == "invalid" and any("全段失效" in r for r in j["reasons"])


def test_judge_health_all_none_unknown():
    """全指标 None → unknown（数据不足，透明降级红线）。"""
    j = judge_health(_m())
    assert j["health"] == "unknown" and len(j["reasons"]) == 3
    assert j["thresholds"]["ic_decay"] == 0.5                   # 默认阈值透明回带


def test_judge_health_single_unknown_does_not_lower():
    """单指标 unknown 不拉低整体：其余健康 → healthy，但 reasons 透明披露缺口。"""
    j = judge_health(_m(decay=0.95, recent=0.1, cov=None, bull=False, bear=False))
    assert j["health"] == "healthy"
    assert any("不拉低整体" in r and "覆盖度" in r for r in j["reasons"])


def test_judge_health_invalid_wins_merge():
    """就低合并：invalid 与 weakening 同时触发 → invalid。"""
    j = judge_health(_m(decay=0.2, recent=-0.01, cov=0.5, bull=True, bear=True))
    assert j["health"] == "invalid"


def test_judge_health_thresholds_override():
    """阈值可配：ic_decay=0.3 时衰减比 0.6 不再触发。"""
    assert judge_health(_m(decay=0.6, recent=0.05, cov=1.0, bull=False, bear=False),
                        {"ic_decay": 0.3})["health"] == "healthy"
    assert judge_health(_m(decay=0.6, recent=0.05, cov=1.0, bull=False, bear=False),
                        {"ic_decay": 0.9})["health"] == "weakening"


def test_save_health_row_idempotent(tmp_path):
    """落库幂等：同 judge 同 as_of 重跑行数不变（覆盖），新 as_of 追加；JSON 全程可读。"""
    from factorlib.lifecycle import save_health_row
    con = duckdb.connect(str(tmp_path / "h.duckdb"))
    con.close()
    j1 = judge_health(_m(decay=0.25, recent=0.05, cov=1.0, bull=False, bear=False))
    j2 = judge_health(_m(decay=0.95, recent=0.1, cov=1.0, bull=False, bear=False))
    save_health_row(tmp_path / "h.duckdb", "f", "1.0.0", "L1", "2026-10-11", j1)
    save_health_row(tmp_path / "h.duckdb", "f", "1.0.0", "L1", "2026-10-11", j1)  # 同 as_of 重跑
    save_health_row(tmp_path / "h.duckdb", "f", "1.0.0", "L1", "2026-10-12", j2)  # 新 as_of 追加
    con = duckdb.connect(str(tmp_path / "h.duckdb"), read_only=True)
    try:
        rows = con.execute("SELECT as_of, health, reasons_json, metrics_json "
                           "FROM fl_factor_health ORDER BY as_of").fetchall()
    finally:
        con.close()
    assert [(r[0], r[1]) for r in rows] == [("2026-10-11", "weakening"),
                                            ("2026-10-12", "healthy")]  # 幂等覆盖 + 追加
    assert "IC 衰减比" in json.loads(rows[0][2])[0]
    assert json.loads(rows[1][3])["ic_decay"] == 0.95


# ══ T-L5 假设复查联动 + 降级动作 + 合成池（必答/抽查/标警/摘除/通知）══
from applib.factor_inspect import (
    downgrade_actions,
    health_rows_from_state,
)
from applib.hypothesis import load as hyp_load
from applib.hypothesis import render_review_fl_md, review_items_fl
from factorlib.lifecycle import ACTIVE_CAP, active_pool


def _repo_all_defs(tmp_path: Path, *, hyp_reg: bool = True) -> Path:
    """多因子 repo：拷贝包内全部 4 个 YAML（pool/review 需要批量语义）。"""
    d = tmp_path / "factors" / "defs"
    d.mkdir(parents=True)
    pkg = Path(R.__file__).parent / "defs"
    for p in sorted(pkg.rglob("*.yaml")):
        (d / p.name).write_text(p.read_text(encoding="utf-8"), encoding="utf-8")
    if hyp_reg:
        hp = tmp_path / "hypothesis" / "registry.json"
        hp.parent.mkdir(parents=True, exist_ok=True)
        hp.write_text(json.dumps({"hypotheses": [
            {"id": "H-0001", "title": "动量效应", "status": "supported"}]},
            ensure_ascii=False), encoding="utf-8")
    return tmp_path


# ── review_items_fl：纯函数直测（dict 进 dict 出，不读 DB）────────────
def test_review_fl_mandatory_weakening_invalid(tmp_path):
    """weakening/invalid 全部必答；reasons 随行带出；id 序确定性。"""
    reg = {"hypotheses": [{"id": "H-0001", "title": "动量", "status": "supported"}]}
    rows = [{"factor_id": "f_b", "health": "weakening", "hypothesis_id": "H-0001",
             "reasons": ["weakening: IC 衰减比 0.3"]},
            {"factor_id": "f_a", "health": "invalid", "hypothesis_id": "H-0001",
             "reasons": []}]
    res = review_items_fl(tmp_path, reg, rows)
    assert res["n_mandatory"] == 2 and res["n_sample"] == 0 and res["n_active"] == 0
    assert [it["factor_id"] for it in res["mandatory"]] == ["f_a", "f_b"]
    assert all(it["kind"] == "mandatory" and not it["lineage_dangling"]
               for it in res["mandatory"])
    assert res["mandatory"][1]["reasons"] == ["weakening: IC 衰减比 0.3"]
    assert "假设仍成立吗" in res["mandatory"][0]["question"]


def test_review_fl_healthy_sample_every_5(tmp_path):
    """healthy 每 5 取 1（HEALTHY_SAMPLE_EVERY 复用）：7 个 → 抽 f1/f6（确定性）。"""
    reg = {"hypotheses": [{"id": "H-0001", "title": "动量"}]}
    rows = [{"factor_id": f"f{i}", "health": "healthy", "hypothesis_id": "H-0001",
             "reasons": []} for i in range(1, 8)]
    res = review_items_fl(tmp_path, reg, rows)
    assert res["n_mandatory"] == 0 and res["n_active"] == 7
    assert [it["factor_id"] for it in res["sample"]] == ["f1", "f6"]
    assert all(it["kind"] == "sample" for it in res["sample"])


def test_review_fl_dangling_lineage_is_mandatory(tmp_path):
    """血缘悬空（未绑定/不在登记表）本身即必答项——healthy 也升级为必答。"""
    reg = {"hypotheses": [{"id": "H-0001", "title": "动量"}]}
    rows = [{"factor_id": "f_unbound", "health": "healthy", "hypothesis_id": None,
             "reasons": []},
            {"factor_id": "f_dangling", "health": "healthy", "hypothesis_id": "H-9999",
             "reasons": []},
            {"factor_id": "f_unknown_dangling", "health": "unknown", "hypothesis_id": None,
             "reasons": []},
            {"factor_id": "f_ok", "health": "healthy", "hypothesis_id": "H-0001",
             "reasons": []}]
    res = review_items_fl(tmp_path, reg, rows)
    assert [it["factor_id"] for it in res["mandatory"]] == \
        ["f_dangling", "f_unbound", "f_unknown_dangling"]
    assert all(it["lineage_dangling"] for it in res["mandatory"])
    assert all("血缘悬空" in it["question"] and "无来源不激活" in it["action"]
               for it in res["mandatory"])
    assert [it["factor_id"] for it in res["sample"]] == ["f_ok"]    # 唯一正常 healthy 仍抽查


def test_review_fl_unknown_not_itemized(tmp_path):
    """unknown 透明披露不强制：数据缺口随巡检报告走，不生成待答项。"""
    reg = {"hypotheses": [{"id": "H-0001", "title": "动量"}]}
    rows = [{"factor_id": "f_x", "health": "unknown", "hypothesis_id": "H-0001",
             "reasons": ["IC衰减: 无 directed IC 序列（全史不足）"]}]
    res = review_items_fl(tmp_path, reg, rows)
    assert res["n_mandatory"] == 0 and res["n_sample"] == 0 and res["n_active"] == 0


def test_render_review_fl_md_smoke(tmp_path):
    reg = {"hypotheses": [{"id": "H-0001", "title": "动量"}]}
    res = review_items_fl(tmp_path, reg, [
        {"factor_id": "f_bad", "health": "invalid", "hypothesis_id": None, "reasons": ["x"]},
        {"factor_id": "f_ok", "health": "healthy", "hypothesis_id": "H-0001", "reasons": []}])
    md = render_review_fl_md(res)
    assert "必答复查" in md and "抽查" in md and "⚠ 悬空" in md and "f_bad" in md


# ── active_pool：合成池快照（M6 消费契约）────────────────────────────
def _activate(repo: Path, fid: str, why: str = "t") -> None:
    set_status(repo, fid, "active", who="t", why=why, gate={"ok": True, "reasons": []})


def test_active_pool_invalid_excluded(tmp_path):
    """invalid → excluded（摘除语义 + reasons 作 reason），healthy 留池；cap 随池披露。"""
    repo = _repo_all_defs(tmp_path)
    for f in ("mom_20d_fl", "val_pe_ttm"):
        _activate(repo, f)
    set_health(repo, "mom_20d_fl", "invalid", why="自动摘除：近24月 directed IC 均值 ≤ 0",
               as_of=_DAY)
    pool = active_pool(None, repo, as_of=_DAY)
    assert pool["n_active"] == 2 and pool["cap"] == ACTIVE_CAP == 30
    assert [p["factor_id"] for p in pool["pool"]] == ["val_pe_ttm"]
    ex = pool["excluded"][0]
    assert ex["factor_id"] == "mom_20d_fl" and ex["health"] == "invalid"
    assert "directed IC 均值 ≤ 0" in ex["reason"]
    assert pool["as_of"] == _DAY


def test_active_pool_weakening_stays_with_warnings(tmp_path):
    """weakening 留池带警示（策略层提示，8.4）：warnings 取 health_history 最新 why。"""
    repo = _repo_all_defs(tmp_path)
    _activate(repo, "mom_20d_fl")
    set_health(repo, "mom_20d_fl", "weakening", why="weakening: IC 衰减比 0.30 < 0.5",
               as_of=_DAY)
    pool = active_pool(None, repo)
    p = pool["pool"][0]
    assert p["health"] == "weakening"
    assert p["warnings"] == ["weakening: IC 衰减比 0.30 < 0.5"]
    assert "mom_20d_fl" not in [x["factor_id"] for x in pool["excluded"]]


def test_active_pool_draft_deprecated_absent(tmp_path):
    """draft / deprecated 不进 pool 也不进 excluded（excluded 只列 invalid 的 active）。"""
    repo = _repo_all_defs(tmp_path)
    _activate(repo, "mom_20d_fl")
    set_status(repo, "mom_20d_fl", "deprecated", who="t", why="退役")
    pool = active_pool(None, repo)                     # val_pe_ttm 等保持 draft
    assert pool["n_active"] == 0 and pool["pool"] == [] and pool["excluded"] == []


def test_active_pool_carries_version_and_hypothesis(tmp_path):
    """version 取注册中心当前版本（T-L3 真源语义）；hypothesis_id 非空才携带。"""
    repo = _repo_all_defs(tmp_path)
    _activate(repo, "mom_20d_fl")
    st = _read_state(repo)
    st["mom_20d_fl"].update({"hypothesis_id": "H-0001", "version": "1.2.0"})
    _write_state(repo, st)
    pool = active_pool(None, repo)
    p = pool["pool"][0]
    assert p["version"] == "1.2.0" and p["hypothesis_id"] == "H-0001" and p["warnings"] == []


def test_active_pool_invalid_does_not_deprecate(tmp_path):
    """红线：invalid 摘除只影响 pool 查询，不迁 lifecycle（退役人工决策）。"""
    repo = _repo_all_defs(tmp_path)
    _activate(repo, "mom_20d_fl")
    set_health(repo, "mom_20d_fl", "invalid", as_of=_DAY)
    assert load_factor(repo, "mom_20d_fl").status == "active"


# ── downgrade_actions：降级动作编排（通知/影响面/出路/幂等）──────────
class _DownCtx:
    def __init__(self, tmp_path: Path, repo: Path):
        self.sandbox_dir = tmp_path / "sandbox"
        self.local_repo = repo
        self.artifacts_dir = tmp_path / "artifacts"


def _down_repo(tmp_path: Path) -> Path:
    """repo：val_pe_ttm 定义（带血缘）+ 假设登记表 H-0001。"""
    repo = _repo_with_def(tmp_path, hypothesis_id="H-0001")
    hp = repo / "hypothesis" / "registry.json"
    hp.parent.mkdir(parents=True, exist_ok=True)
    hp.write_text(json.dumps({"hypotheses": [
        {"id": "H-0001", "title": "估值效应", "status": "supported"}]},
        ensure_ascii=False), encoding="utf-8")
    return repo


def _down_out(health: str = "invalid", transition: dict | None = None) -> dict:
    return {"run_id": "fl_inspect_t", "as_of": _DAY, "drill": False,
            "factors": [{"factor_id": "val_pe_ttm", "health": health,
                         "reasons": ["invalid: 近24月 directed IC 均值 -0.01 ≤ 0"],
                         "version": "1.0.0", "health_transition": transition}]}


def test_downgrade_invalid_migration_impact_and_exits(tmp_path, monkeypatch):
    """DoD：weakening→invalid 迁移触发 impact_report（mock 验证参数）；通知含影响面
    摘要 + 两条出路文案；必答复查项随 review 字段产出。"""
    repo = _down_repo(tmp_path)
    seen = {}

    def fake_impact(db_path, repo_, fid, version=None, out_dir=None):
        seen.update(db=db_path, repo=repo_, fid=fid, version=version)
        return {"version": version, "n_strategies": 2, "n_rows": 1234, "md": "",
                "factor_id": fid, "refs": {"evals": [1], "gates": [1, 2]}}

    monkeypatch.setattr("factorlib.lifecycle.impact_report", fake_impact)
    ctx = _DownCtx(tmp_path, repo)
    out = _down_out(transition={"from": "weakening", "to": "invalid", "as_of": _DAY})
    res = downgrade_actions(ctx, out)
    assert res["review"]["n_mandatory"] == 1                     # invalid → 必答
    assert seen["fid"] == "val_pe_ttm" and seen["version"] == "1.0.0"
    assert seen["db"] == Path(ctx.sandbox_dir) / "data" / "fl.duckdb"
    n = res["notifications"][0]
    assert n["factor_id"] == "val_pe_ttm" and n["health"] == "invalid"
    assert n["migration"] == {"from": "weakening", "to": "invalid"}
    assert n["impact"]["n_strategies"] == 2 and n["impact"]["n_rows"] == 1234
    assert n["impact"]["n_evals"] == 1 and n["impact"]["n_gates"] == 2
    assert len(n["actions"]) == 2
    assert "出路①" in n["actions"][0] and "set-status" in n["actions"][0] \
        and "refuted" in n["actions"][0]
    assert "出路②" in n["actions"][1] and "invalid → unknown" in n["actions"][1]
    md = Path(res["notifications_md"])                           # 通知落盘（人读）
    assert md.exists() and "影响面" in md.read_text(encoding="utf-8")
    assert "fl_inspect_t" in str(md) and md.name == "notifications.md"


def test_downgrade_no_migration_no_impact(tmp_path, monkeypatch):
    """同态重申（from==to）不算迁移：不触发影响面评估，weakening 只标警留池。"""
    repo = _down_repo(tmp_path)
    called = []
    monkeypatch.setattr("factorlib.lifecycle.impact_report",
                        lambda *a, **k: called.append(a))
    ctx = _DownCtx(tmp_path, repo)
    out = _down_out(health="weakening",
                    transition={"from": "weakening", "to": "weakening", "as_of": _DAY})
    res = downgrade_actions(ctx, out)
    assert called == [] and res["notifications"][0]["impact"] is None
    assert res["notifications"][0]["actions"][0].startswith("标警留池")


def test_downgrade_same_asof_rerun_dedup(tmp_path, monkeypatch):
    """幂等：同 as_of 重跑通知按 factor+as_of 去重（不累积），MD 覆盖写不追加。"""
    repo = _down_repo(tmp_path)
    monkeypatch.setattr("factorlib.lifecycle.impact_report",
                        lambda *a, **k: {"version": "1.0.0", "n_strategies": 0,
                                         "n_rows": 0, "md": "",
                                         "refs": {"evals": [], "gates": []}})
    ctx = _DownCtx(tmp_path, repo)
    out = _down_out(transition={"from": "healthy", "to": "invalid", "as_of": _DAY})
    r1 = downgrade_actions(ctx, out)
    r2 = downgrade_actions(ctx, out)                             # 同 as_of 重跑
    key = lambda rs: [(n["factor_id"], n["as_of"]) for n in rs]
    assert key(r1["notifications"]) == key(r2["notifications"]) == [("val_pe_ttm", _DAY)]
    assert len(r2["notifications"]) == 1
    assert Path(r2["notifications_md"]).read_text(encoding="utf-8") == \
        Path(r1["notifications_md"]).read_text(encoding="utf-8")


def test_downgrade_drill_no_notification(tmp_path):
    """演练态：不产生复查/通知（不落盘红线的自然延伸）。"""
    repo = _down_repo(tmp_path)
    ctx = _DownCtx(tmp_path, repo)
    out = {**_down_out(transition={"from": "healthy", "to": "invalid", "as_of": _DAY}),
           "drill": True}
    assert downgrade_actions(ctx, out) == {"review": None, "notifications": []}
    assert not (Path(ctx.artifacts_dir) / "fl_inspect_t" / "notifications.md").exists()


# ── 复查入口联动：状态行构造 + 悬空必答（端到端最小闭环）────────────
def test_health_rows_from_state_active_only(tmp_path):
    """仅 active 因子进复查行；reasons 取 health_history 最新 why；draft 不进。"""
    repo = _repo_all_defs(tmp_path)
    _activate(repo, "mom_20d_fl")
    _activate(repo, "val_pe_ttm")
    set_health(repo, "mom_20d_fl", "weakening", why="weakening: IC 衰减比 0.3 < 0.5",
               as_of=_DAY)
    rows = health_rows_from_state(repo)
    by = {r["factor_id"]: r for r in rows}
    assert set(by) == {"mom_20d_fl", "val_pe_ttm"}
    assert by["mom_20d_fl"]["health"] == "weakening"
    assert by["mom_20d_fl"]["reasons"] == ["weakening: IC 衰减比 0.3 < 0.5"]
    assert by["val_pe_ttm"]["health"] == "healthy" and by["val_pe_ttm"]["reasons"] == []


def test_review_flow_from_state_dangling_mandatory(tmp_path):
    """端到端：状态行 → 复查。存量 active 未绑血缘 → 必答（悬空），无来源不激活落地。"""
    repo = _repo_all_defs(tmp_path)
    _activate(repo, "mom_20d_fl")
    res = review_items_fl(repo, hyp_load(repo), health_rows_from_state(repo))
    assert res["n_mandatory"] == 1
    it = res["mandatory"][0]
    assert it["factor_id"] == "mom_20d_fl" and it["lineage_dangling"] is True
    assert "血缘悬空" in it["question"]
