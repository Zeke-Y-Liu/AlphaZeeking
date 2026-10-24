"""M7 T-F9 多市场扩展位 + 个人化因子通道测试。

覆盖（契约 DoD a–e）：
a) markets 字段：缺省 CN 兼容（存量 YAML 零迁移）；非法枚举报错；["CN","HK"] 财务源预检清单；
b) account_mapping：映射校验（缺项报错列名）；HKFRS 样例映射往返；
c) HK 空跑：重检验触发 → HK 段数据缺失披露（不抛异常）+ CN 段正常；
d) personal_clone：登记 → draft；激活演练 → 未过关照拒（拒因含「个人化来源不豁免检验」）；
e) 存量 YAML（v1/v2 六份）零迁移回归 + 混合市场回测显式拒绝（不静默按 CN 跑）。

运行：``cd alphapocket && python -m pytest tests/test_multimarket_m7.py -q``
"""
from __future__ import annotations

import json
import shutil
from pathlib import Path

import duckdb
import pytest
import yaml

ROOT = Path(__file__).resolve().parent.parent
SB_REPO = ROOT / "sandbox" / "repo"
SB_STRATEGIES = SB_REPO / "strategies"
SB_DEMO_DEF = SB_REPO / "factors" / "defs" / "f5_personal" / "demo_clone_f1.yaml"
SB_ACCOUNT_MAP = SB_REPO / "dim" / "account_mapping.json"


@pytest.fixture
def clone_repo(fl_repo):
    """golden repo + repo 级 defs 扩展位（演练候选 def 落 repo defs，走合并加载路径）。"""
    dst = fl_repo / "factors" / "defs" / "f5_personal"
    dst.mkdir(parents=True, exist_ok=True)
    shutil.copy(SB_DEMO_DEF, dst / "demo_clone_f1.yaml")
    return fl_repo


@pytest.fixture
def pool_ready(fl_repo):
    """把存量策略 YAML 引用的因子全部激活（池准入放行；不物化——校验链零 DB 消费）。"""
    from factorlib.registry import load_factor, set_status
    fids: set[str] = set()
    for p in sorted(SB_STRATEGIES.glob("*.yaml")):
        raw = yaml.safe_load(p.read_text(encoding="utf-8"))
        for f in raw.get("factors") or []:
            fids.add(str(f["id"]))
    for fid in sorted(fids):
        set_status(fl_repo, fid, "active", who="m7-tf9", why="多市场测试池",
                   gate={"ok": True, "reasons": []})
    _ = load_factor  # 保持导入形态一致
    return fl_repo, sorted(fids)


def _write_strategy(tmp_path: Path, body: dict) -> Path:
    p = tmp_path / "strategy.yaml"
    p.write_text(yaml.safe_dump(body, allow_unicode=True), encoding="utf-8")
    return p


def _base_body(fid: str = "val_pe_ttm") -> dict:
    return {"id": "mkt-test", "name": "市场口径测试",
            "factors": [{"id": fid, "direction": "asc", "weight": "equal"}],
            "composite": {"top_n": 1, "method": ["equal"]},
            "rebalance": "monthly", "exec": "close",
            "universe": {"base": "CN_EQUITY_ALL"}}


# ── a) markets 字段：缺省 CN 兼容（零迁移）──────────────────────────────
def test_markets_default_cn_zero_migration(pool_ready, fl_db, tmp_path):
    """未写 markets 的 YAML（dict/串两种 universe 形态）→ 缺省 ["CN"]，无预检键。"""
    from applib.strategy import load_strategy
    repo, _ = pool_ready
    db, _ = fl_db
    p1 = _write_strategy(tmp_path, _base_body())                    # dict universe
    spec1 = load_strategy(p1, repo, db)
    assert spec1["universe"]["markets"] == ["CN"]
    assert "market_gaps" not in spec1
    body = _base_body()
    body["universe"] = "CN_EQUITY_ALL"                              # 串 universe（存量形态）
    spec2 = load_strategy(_write_strategy(tmp_path, body), repo, db)
    assert spec2["universe"]["markets"] == ["CN"]
    assert "market_gaps" not in spec2


def test_existing_strategy_yaml_zero_migration(pool_ready, fl_db):
    """存量 v1/v2 六份 YAML：源文件不含 markets 字段（T-F9 前产物），经新加载链
    全部缺省 markets=["CN"] 且校验通过——零迁移回归（DoD e）。
    T-F9 演练 YAML（hk-dryrun-pe-ttm.yaml）显式排除：它是本任务新增产物，自带 markets。"""
    from applib.strategy import load_strategy
    repo, _ = pool_ready
    db, _ = fl_db
    files = sorted(SB_STRATEGIES.glob("*.yaml"))
    assert len(files) == 7, f"策略应 7 份（6 存量 + 1 T-F9 演练）：{[p.name for p in files]}"
    legacy = [p for p in files if p.name != "hk-dryrun-pe-ttm.yaml"]
    assert len(legacy) == 6
    for p in legacy:
        raw = yaml.safe_load(p.read_text(encoding="utf-8"))
        uni = raw.get("universe")
        assert not (isinstance(uni, dict) and "markets" in uni), f"{p.name} 不应含 markets"
        spec = load_strategy(p, repo, db)
        assert spec["universe"]["markets"] == ["CN"], p.name
        assert "market_gaps" not in spec, p.name


def test_markets_illegal_enum_rejected(pool_ready, fl_db, tmp_path):
    """非法枚举/形态/重复 → StrategyError 一次报明（允许集 CN/HK/US）。"""
    from applib.strategy import StrategyError, load_strategy
    repo, _ = pool_ready
    db, _ = fl_db
    for bad in (["JP"], "CN", [], ["CN", "CN"], {"CN": 1}):
        body = _base_body()
        body["universe"]["markets"] = bad
        with pytest.raises(StrategyError) as ei:
            load_strategy(_write_strategy(tmp_path, body), repo, db)
        assert "markets" in str(ei.value)


# ── a) ["CN","HK"]：财务源就绪性预检清单（缺口显式披露，不阻断加载）─────
def test_market_readiness_hk_gaps_disclosed(pool_ready, fl_db, tmp_path):
    """golden repo（无映射文件/无 HK 数据）→ market_gaps 列全三类缺口 + 混合口径披露。"""
    from applib.strategy import load_strategy
    repo, _ = pool_ready
    db, _ = fl_db
    body = _base_body()
    body["universe"]["markets"] = ["CN", "HK"]
    spec = load_strategy(_write_strategy(tmp_path, body), repo, db)   # 不抛异常
    gaps = "\n".join(spec["market_gaps"])
    assert "科目映射缺失" in gaps                                     # ① 映射文件缺失
    assert "行情未接入" in gaps                                       # ② fl_daily_bar 无 HK 行
    assert "财务源缺失" in gaps and "val_pe_ttm" in gaps              # ③ 财务因子不可用清单
    assert "混合口径" in gaps                                         # 跨市场语义边界披露


def test_market_readiness_hk_with_mapping_and_bars(pool_ready, fl_db, tmp_path):
    """补映射文件 + HK 行情行 → 前两类缺口消除；财务源缺口保留（数据未接入）。"""
    from applib.strategy import load_strategy
    repo, _ = pool_ready
    db, _ = fl_db
    (repo / "dim").mkdir(exist_ok=True)
    shutil.copy(SB_ACCOUNT_MAP, repo / "dim" / "account_mapping.json")
    con = duckdb.connect(str(db))
    try:
        con.execute("INSERT INTO fl_daily_bar VALUES "
                    "('00700.HK', DATE '2021-01-29', 10, 11, 9, 10, 1000, 1e6, 0.01)")
    finally:
        con.close()
    body = _base_body()
    body["universe"]["markets"] = ["CN", "HK"]
    spec = load_strategy(_write_strategy(tmp_path, body), repo, db)
    gaps = "\n".join(spec["market_gaps"])
    assert "科目映射" not in gaps
    assert "行情未接入" not in gaps
    assert "财务源缺失" in gaps and "val_pe_ttm" in gaps


# ── b) account_mapping：校验缺项列名 + HKFRS 往返 ───────────────────────
def test_account_mapping_validation_and_roundtrip(fl_repo, tmp_path):
    """样例映射往返（eps ↔ BasicEarningsPerShare）；缺科目 → missing 列名；缺文件全判缺。"""
    from factorlib.account_map import (
        AccountMapError,
        external_name,
        internal_name,
        load_account_map,
        validate_market,
    )
    assert not (fl_repo / "dim" / "account_mapping.json").exists()
    v0 = validate_market(fl_repo, "HK", ["eps", "revenue"])
    assert v0["ok"] is False and v0["missing"] == ["eps", "revenue"]   # 缺文件 → 全列缺项

    dim = fl_repo / "dim"
    dim.mkdir(exist_ok=True)
    shutil.copy(SB_ACCOUNT_MAP, dim / "account_mapping.json")
    mapping = load_account_map(fl_repo)
    # HKFRS 往返：内部 → 外部 → 内部
    ext = external_name(mapping, "HK", "eps")
    assert ext == "BasicEarningsPerShare"
    assert internal_name(mapping, "HK", ext) == "eps"
    ext_us = external_name(mapping, "US", "net_profit")
    assert internal_name(mapping, "US", ext_us) == "net_profit"
    # 全量校验通过
    assert validate_market(fl_repo, "HK", ["eps", "revenue", "net_profit"])["ok"] is True
    # 缺科目 → 逐条列名
    bad = json.loads(SB_ACCOUNT_MAP.read_text(encoding="utf-8"))
    del bad["markets"]["HK"]["mappings"]["revenue"]
    p = dim / "account_mapping.json"
    p.write_text(json.dumps(bad, ensure_ascii=False, indent=2), encoding="utf-8")
    v1 = validate_market(fl_repo, "HK", ["eps", "revenue"])
    assert v1["ok"] is False and v1["missing"] == ["revenue"]
    # 非法市场枚举
    with pytest.raises(AccountMapError):
        validate_market(fl_repo, "JP", ["eps"])


# ── c) HK 空跑：重检验触发 + 分段披露（不抛异常，CN 段正常）────────────
def test_hk_dryrun_eval_flow(fl_repo, fl_db):
    """run_market_eval(["CN","HK"])：CN 段 M3 重跑落档；HK 段就绪性披露，全程不抛异常。"""
    from applib.factor_eval_fl import run_market_eval, save_eval_report
    from factorlib.materialize import materialize
    from factorlib.registry import load_factor, set_status
    db, _ = fl_db
    set_status(fl_repo, "val_pe_ttm", "active", who="m7-tf9", why="HK 空跑演练",
               gate={"ok": True, "reasons": []})
    materialize(load_factor(fl_repo, "val_pe_ttm"), db, mode="full")
    res = run_market_eval(db, fl_repo, "val_pe_ttm", ["CN", "HK"])
    assert set(res["segments"]) == {"CN", "HK"}
    cn = res["segments"]["CN"]
    assert cn["rechecked"] is True and cn["h1"]["n_periods"] and cn["h1"]["directed_mean"] is not None
    hk = res["segments"]["HK"]
    assert hk["rechecked"] is False and hk["ok"] is False
    assert any("财务源缺失" in g for g in hk["gaps"])
    # CN 段报告已落 fl_factor_eval（重检验落档）
    con = duckdb.connect(str(db), read_only=True)
    try:
        n = con.execute("SELECT count(*) FROM fl_factor_eval WHERE factor_id = 'val_pe_ttm'"
                        ).fetchone()[0]
    finally:
        con.close()
    assert n >= 1
    _ = save_eval_report


# ── d) personal_clone：登记 → draft；门禁照跑 → 未过关照拒（不豁免）─────
def test_personal_clone_register_and_gate_no_waiver(clone_repo, fl_db):
    """登记：source_type=personal_clone + 血缘绑定 + 保持 draft；
    门禁：七查照跑全链评估，无检验记录照拒，拒因含「个人化来源不豁免检验」。"""
    from applib.hypothesis import load as hyp_load
    from applib.hypothesis import personal_clone_register
    from factorlib.lifecycle import activation_gate
    from factorlib.registry import load_factor
    db, _ = fl_db
    res = personal_clone_register(clone_repo, "demo_clone_f1",
                                  strategy="某红利低波 ETF 策略",
                                  thesis="红利 + 低波两维复合的因子化拆解：低股息调整波动超配")
    assert "error" not in res, res
    assert res["hypothesis_id"] == "H-0001" and res["source"]["type"] == "personal_clone"
    assert res["source"]["ref"] == "某红利低波 ETF 策略" and res["bound"] is True
    meta = load_factor(clone_repo, "demo_clone_f1")
    assert meta.status == "draft" and meta.hypothesis_id == "H-0001"     # 登记不改状态机
    reg = hyp_load(clone_repo)
    hyp = reg["hypotheses"][0]
    assert hyp["source"]["type"] == "personal_clone"
    assert hyp["linked_factors"] == ["demo_clone_f1"]
    # 门禁七查照跑（无豁免短路）：血缘过、检验/治理关全拒
    gate = activation_gate(db, clone_repo, "demo_clone_f1")
    assert gate["ok"] is False
    assert gate["checks"]["lineage_hyp"]["ok"] is True                    # 来源已登记
    assert len(gate["checks"]) >= 6                                       # 七查逐项在档
    reasons = "\n".join(gate["reasons"])
    assert "未物化" in reasons or "无同 lineage" in reasons or "未跑" in reasons
    assert "个人化来源不豁免检验" in reasons                               # 不豁免提示在拒因


def test_personal_clone_errors_and_no_hint_for_regular_draft(fl_repo, fl_db):
    """缺拆解依据/来源拒登记；非 draft 拒登记；非 personal_clone 拒因不含不豁免提示
    （提示只跟来源类型走，门禁语义对所有来源一致）。"""
    from applib.hypothesis import personal_clone_register
    from factorlib.lifecycle import activation_gate
    from factorlib.registry import set_status
    db, _ = fl_db
    assert "error" in personal_clone_register(fl_repo, "demo_clone_f1",
                                              strategy="s", thesis="  ")
    assert "error" in personal_clone_register(fl_repo, "demo_clone_f1",
                                              strategy="", thesis="t")
    assert "error" in personal_clone_register(fl_repo, "no_such_factor",
                                              strategy="s", thesis="t")
    set_status(fl_repo, "val_pe_ttm", "active", who="m7-tf9", why="t",
               gate={"ok": True, "reasons": []})
    assert "error" in personal_clone_register(fl_repo, "val_pe_ttm",
                                              strategy="s", thesis="t")
    # 非 personal_clone 的 draft 候选：同样照拒，但拒因无个人化提示
    gate = activation_gate(db, fl_repo, "val_pe_ttm")
    assert gate["ok"] is False
    assert "个人化来源不豁免检验" not in "\n".join(gate["reasons"])


# ── e) 混合市场回测显式拒绝（不静默按 CN 跑）───────────────────────────
def test_assert_cn_only_rejects_mixed_backtest():
    """assert_cn_only：混合市场 spec → StrategyError；CN 单市场 spec 放行。"""
    from applib.strategy import StrategyError, assert_cn_only
    assert_cn_only({"universe": {"markets": ["CN"]}})
    assert_cn_only({"universe": {"base": "CN_EQUITY_ALL"}})               # 缺 markets → 缺省 CN
    with pytest.raises(StrategyError) as ei:
        assert_cn_only({"universe": {"markets": ["CN", "HK"]}})
    assert "M8+" in str(ei.value) and "显式拒绝" in str(ei.value)


# ── f) fetch 破坏性重写守卫（T-F10 收口回归缺陷根修）────────────────────
def test_fetch_destructive_rewrite_guard():
    """T-F9 演练 `ap fetch --secid 00700.HK`（缺省 mode）触发 bootstrap：rmtree 重建
    remote/repo 后仅写 1 标的 → 56 标的快照整体丢失（core 冒烟 KeyError '510300.SH'
    实锤）。守卫：bootstrap/refresh + universe 子集 → ValueError 指引 incremental；
    全量 bootstrap（universe=None）放行（既有 data_sync 兼容）。"""
    from types import SimpleNamespace

    from applib import datasource as DS
    with pytest.raises(ValueError, match="破坏性重写守卫"):
        DS.fetch_all(SimpleNamespace(), universe=["00700.HK"])            # 缺省 bootstrap + 子集
    with pytest.raises(ValueError, match="破坏性重写守卫"):
        DS.fetch_all(SimpleNamespace(), universe=["00700.HK"], mode="refresh")
    with pytest.raises(ValueError, match="破坏性重写守卫"):
        DS.fetch_all(SimpleNamespace(), universe=["00700.HK"], mode="bootstrap")


def test_fetch_cli_secid_defaults_incremental():
    """CLI --secid 子集 + 缺省 mode → 命令层自动升级 incremental（合并语义）。
    断言 cmd_fetch 的推导真源（argparse default=None；守卫语义在 fetch_all 层）。"""
    import inspect

    from applib import cli
    src = inspect.getsource(cli.cmd_fetch)
    assert '"incremental" if args.secid else None' in src, \
        "cmd_fetch 必须保留 --secid → incremental 自动升级（防守卫误伤 CLI 便捷路径）"
