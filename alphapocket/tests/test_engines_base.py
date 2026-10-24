"""engines/ 包装层骨架契约测试（T-R1）：委托 / 入参校验 / 位约束 / 无状态 / provenance。"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from applib.engines import EngineSpec, EngineWrapper, check_output_range, validate_input


def _spec(**over) -> EngineSpec:
    base = {"engine_id": "test_engine", "version": "v1",
                "input_schema": {"secid": str, "n": int, "factor": (int, float)},
                "required": ("secid",),
                "output_range": (-2, 2), "range_fields": ("level",)}
    base.update(over)
    return EngineSpec(**base)


# ── 入参校验（契约 2）：失败在包装层拦截，不进计算函数 ─────────────
def test_validate_input_pass_and_fail():
    assert validate_input(_spec(), {"secid": "510300.SH", "n": 20}) is None
    assert "缺少必填" in validate_input(_spec(), {})             # 缺 required
    assert "未声明" in validate_input(_spec(), {"secid": "s", "x": 1})  # 越出 schema
    assert "类型须为" in validate_input(_spec(), {"secid": "s", "n": "20"})  # 类型错
    # 联合类型与 None 放行规则
    assert validate_input(_spec(), {"secid": "s", "factor": 0.5}) is None
    assert validate_input(_spec(required=("secid", "n")), {"secid": "s"}) is not None


# ── 位范围（契约 3）：越界收集告警不截断不抛异常 ──────────────────
def test_output_range_warnings():
    assert check_output_range(_spec(), {"level": 2}) == []
    w = check_output_range(_spec(), {"level": 3})
    assert len(w) == 1 and "越界" in w[0] and "不截断" in w[0]
    assert check_output_range(_spec(), {"level": None}) == []       # 非数值不检
    assert check_output_range(_spec(output_range=None), {"level": 9}) == []
    assert check_output_range(_spec(), "not-a-dict") == []


# ── 包装器三面：委托 / 校验拦截 / provenance / 无状态（契约 1/4/5） ──
def test_wrapper_delegates_and_records_provenance():
    calls = []

    def compute(secid, n=None, factor=None):    # 假 compute_x：签名形参=入参键
        calls.append((secid, n, factor))
        return {"level": 1, "value": secid.upper()}

    eng = EngineWrapper(_spec(), compute)
    out = eng({"secid": "abc", "n": 5})
    assert calls == [("abc", 5, None)]          # 委托：原参透传、公式体零复制
    assert out["value"] == "ABC" and out["level"] == 1
    assert out["_engine"]["engine_id"] == "test_engine" and out["_engine"]["status"] == "ok"
    assert "elapsed_ms" in out["_engine"]
    assert "_range_warnings" not in out


def test_wrapper_blocks_invalid_input():
    calls = []

    def compute(secid, n=None):
        calls.append(secid)
        return {"level": 0}

    eng = EngineWrapper(_spec(), compute)
    out = eng({"n": 1})                          # 缺 required → 拦截
    assert out["status"] == "invalid_input" and "缺少必填" in out["error"]
    assert calls == []                           # 计算函数未被调用
    assert out["_engine"]["status"] == "invalid_input"


def test_wrapper_out_of_range_warns_not_truncates():
    eng = EngineWrapper(_spec(), lambda secid: {"level": 5})
    out = eng({"secid": "s"})
    assert out["level"] == 5                     # 不截断
    assert len(out["_range_warnings"]) == 1


def test_wrapper_stateless():
    """契约 4：同实例串行两次调用互不影响（包装层零可变状态）。"""
    eng = EngineWrapper(_spec(), lambda secid, n=None, factor=None: {"level": -2, "secid": secid})
    a = eng({"secid": "s1"})
    b = eng({"secid": "s2"})
    assert a["secid"] == "s1" and b["secid"] == "s2"
    assert a["_engine"] is not b["_engine"]      # provenance 每次独立生成
