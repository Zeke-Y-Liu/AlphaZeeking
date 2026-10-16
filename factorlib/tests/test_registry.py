"""注册中心单测（状态机 / 校验 / 排序 / lookback）。

校验类测试走 _parse_factor（YAML → FactorMeta 的唯一入口）；
状态机 / lookback 测试用返回的 meta 实例。
"""
import pytest

from factorlib.registry import (FactorMeta, RegistryError, _parse_factor, load_definitions,
                                resolve_start, set_status)

# YAML 视角的最小合法定义（engine=sql → 须带 sql 字段）
DOC = dict(id="val_pe_ttm", name="市盈率TTM", family="valuation", engine="sql",
           depends=["dwd_daily_bar"], sql="SELECT 1", direction=-1)


def _parse(**kw):
    return _parse_factor({**DOC, **kw}, source="test")


def _meta(**kw):
    """完整字段直接构造 FactorMeta（状态机/lookback 测试用，不走注册校验）。"""
    base = dict(id="val_pe_ttm", name="市盈率TTM", family="valuation", type="alpha",
                status="draft", version="1.0.0", universe=["CN"], engine="sql",
                lookback_days=0, depends=["dwd_daily_bar"], direction=-1,
                hypothesis_id=None, transform=[], body="SELECT 1")
    base.update(kw)
    return FactorMeta(**base)


def test_parse_defaults_and_meta():
    m = _parse()
    assert m.status == "draft" and m.type == "alpha" and m.version == "1.0.0"
    assert m.body == "SELECT 1" and m.direction == -1


def test_load_definitions_sorted_and_parsed():
    metas = load_definitions("/workspace/factorlib")
    assert [m.id for m in metas] == sorted(m.id for m in metas)      # 按 id 排序（检查点锚）
    pe = next(m for m in metas if m.id == "val_pe_ttm")
    assert pe.engine == "sql" and pe.direction == -1 and pe.status == "draft"
    assert pe.depends == ["dwd_daily_bar", "dwd_financial_report"]
    assert "announce_date <= b.dt" in pe.body                        # PIT 对齐内嵌于定义
    assert pe.version == "1.0.0"


def test_register_rejects_missing_required():
    with pytest.raises(RegistryError, match="缺少必填字段"):
        _parse(name=None)


def test_register_rejects_bad_family_prefix():
    with pytest.raises(RegistryError, match="族前缀"):
        _parse(id="pe_ttm")


def test_register_rejects_unknown_engine():
    with pytest.raises(RegistryError, match="未知引擎"):
        _parse(engine="spark")


def test_register_rejects_engine_body_mismatch():
    with pytest.raises(RegistryError, match="须提供非空"):
        _parse(engine="python_udf")            # fn 字段缺失（sql 文本不是该引擎的声明体）


def test_register_rejects_non_draft_initial():
    with pytest.raises(RegistryError, match="draft"):
        _parse(status="active")


def test_register_rejects_unknown_fields():
    with pytest.raises(RegistryError, match="未声明字段"):
        _parse(risk_premium=0.05)


def test_status_machine_happy_path():
    m = _parse()
    assert set_status(m, "active").status == "active"
    assert set_status(m, "deprecated").status == "deprecated"


def test_status_machine_rejects_skip_and_back():
    m = _parse()
    with pytest.raises(RegistryError, match="非法迁移"):
        set_status(m, "deprecated")           # draft → deprecated 跳级
    m2 = set_status(_parse(), "active")
    with pytest.raises(RegistryError, match="非法迁移"):
        set_status(m2, "draft")               # 回退
    m3 = set_status(m2, "deprecated")
    with pytest.raises(RegistryError, match="非法迁移"):
        set_status(m3, "active")              # 终态


def test_resolve_start_max_lookback():
    days = [f"2021-01-{d:02d}" for d in range(4, 31)]
    metas = [_meta(lookback_days=0), _meta(id="mom_x", lookback_days=5)]
    assert resolve_start(metas, days) == days[5]      # 起点 = max(lookback) 满足日
    with pytest.raises(RegistryError, match="交易日不足"):
        resolve_start([_meta(lookback_days=100)], days)
    assert resolve_start([], days) is None
