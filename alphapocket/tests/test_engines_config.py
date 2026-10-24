"""engines 配置注入与版本绑定测试（T-R3）。"""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from applib import factors as F
from applib.engines import (
    BETA_SPEC,
    ENGINE_REGISTRY_VERSION,
    EngineWrapper,
    beta_engine,
    engine_config_for,
    load_engine_config,
)


def _bars(n=30, base=10.0):
    return [{"dt": f"2024-{1 + i // 28:02d}-{1 + i % 28:02d}", "open": base + i * 0.1,
             "high": base + i * 0.1 + 0.2, "low": base + i * 0.1 - 0.1,
             "close": round(base + i * 0.1, 3), "volume": 1000 + i, "amount": 1e5 + i}
            for i in range(n)]


ARGS = {"secid": "600519.SH", "bars": _bars(), "as_of": "2024-01-29"}


def _spy_eng():
    """带调用捕获的 beta 包装器（fn 换成透传 compute_beta 的 spy）。"""
    seen, origin = [], beta_engine._fn

    def spy(**kw):
        seen.append(kw)
        return origin(**kw)

    return EngineWrapper(BETA_SPEC, spy), seen


# ── 配置变更可生效：params 注入真实改变公式行为（走签名默认 → 注入值 diff） ──
def test_config_params_take_effect():
    eng, seen = _spy_eng()
    d = eng(dict(ARGS))                                   # 签名默认 win=20
    c = eng(dict(ARGS), config={"params": {"win": 10}})   # 注入 win=10
    assert seen[0].get("win", 20) == 20 and seen[1]["win"] == 10   # 默认=签名缺省（kwargs 不含）
    assert d["state"] == c["state"] == "above"            # 状态词不受窗口影响（合理）
    assert {k: v for k, v in d.items() if not k.startswith("_")} != \
           {k: v for k, v in c.items() if not k.startswith("_")}   # 数值面真实变化


def test_explicit_args_override_config_params():
    eng, seen = _spy_eng()
    eng(dict(ARGS, win=25), config={"params": {"win": 10}})
    assert seen[-1]["win"] == 25                          # 显式入参 > 配置默认


# ── 停用：enabled=false 不进公式（红线上不绕过包装层） ─────────────────
def test_disabled_blocks_before_compute():
    eng, seen = _spy_eng()
    out = eng(dict(ARGS), config={"enabled": False})
    assert out["status"] == "disabled" and "停用" in out["reason"]
    assert seen == [] and out["_engine"]["status"] == "disabled"


# ── 版本绑定：相符 / 不符两分支（不符=显式登记 + 结果仍正确产出） ────────
def test_registry_version_binding():
    eng, _ = _spy_eng()
    ok = eng(dict(ARGS), config={"params": {"win": 10},
                                 "registry_version": ENGINE_REGISTRY_VERSION})
    assert "_version_warning" not in ok and ok["state"] == "above"

    bad = eng(dict(ARGS), config={"params": {"win": 10}, "registry_version": "v9"})
    assert "_version_warning" in bad and "≠ 注册表" in bad["_version_warning"]
    assert bad["state"] == "above" and bad["_engine"]["status"] == "ok"   # 不阻断：结果正确产出


# ── 无配置走默认并告警（缺条目不静默） ────────────────────────────────
def test_missing_config_warns_with_defaults():
    eng, seen = _spy_eng()
    out = eng(dict(ARGS), config={})                      # 空配置条目
    assert seen[-1].get("win", 20) == 20                  # 走函数签名默认（kwargs 缺省）
    assert "_config_warnings" in out and "未显式配置" in out["_config_warnings"][0]


# ── 配置文件加载：缺文件 {} / 有效解析 / 形态校验 / 按引擎取条目 ─────────
def test_load_engine_config(tmp_path):
    assert load_engine_config(None) == {} and load_engine_config(tmp_path / "no.json") == {}
    f = tmp_path / "engines.json"
    f.write_text(json.dumps({"engine_b_beta": {"params": {"win": 10}},
                             "engine_c_macro": {"enabled": False},
                             "bad": "not-a-dict"}), encoding="utf-8")
    cfg = load_engine_config(f)
    assert cfg["engine_b_beta"]["params"] == {"win": 10}          # 非法条目降级 {} 不炸
    assert engine_config_for(cfg, beta_engine) == {"params": {"win": 10}}
    assert engine_config_for({}, beta_engine) == {}
    try:
        engine_config_for({"engine_b_beta": 5}, beta_engine)      # 非 dict 条目 → 显式报错
        raise AssertionError("should raise")
    except ValueError:
        pass
    # 真实公式委托 identity 不受 spy 影响的本体仍可注入
    out = beta_engine(dict(ARGS), config=engine_config_for(cfg, beta_engine))
    assert out["state"] == "above" and "_engine" in out
    assert beta_engine._fn is F.compute_beta                      # 红线：公式仍原位
