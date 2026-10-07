"""R3 研报→假设登记通道测试（《研究缺口R1-R4实施计划》§5.4 DoD）

- 门禁注入：不带 hypothesis_id 的 draft 因子执行激活 → 被拒（无旁路）
- 双向血缘：hypothesis.list --factor <id> 可点回来源
- 巡检演练：人为将某因子置 weakening → 自动生成对应假设的复查待办
- 状态机：仅允许 proposed→testing→supported/refuted→retired
运行：``cd alphapocket && python -m pytest tests/ -q``
"""
from __future__ import annotations

from applib import hypothesis as HB


def _hyp_reg() -> dict:
    return {"hypotheses": [{
        "id": "H-0001", "title": "低估值溢价",
        "source": {"type": "book", "ref": "《量化的黑箱》第 3 章", "url": None, "quoted_at": "2026-10-07"},
        "statement": "低 PE 分位个股未来 3–6 月超额收益为正", "expected_direction": "positive",
        "universe": ["CN"], "status": "supported", "linked_factors": ["val_pe_pct"],
        "evidence_runs": [], "created_at": "2026-10-07", "updated_at": "2026-10-07"}]}


def _factor_reg() -> dict:
    return {"factors": [
        {"id": "val_pe_pct", "name": "PE五年分位", "type": "alpha", "status": "active",
         "version": "v1.0", "universe": ["CN"], "hypothesis_id": "H-0001"},
        {"id": "new_sig", "name": "新信号", "type": "signal", "status": "draft",
         "version": "v0.1", "universe": ["CN"]},
    ]}


# ── 登记：最小字段校验（宁缺毋滥）───────────────────────
def test_register_min_fields_validated():
    reg = {"hypotheses": []}
    assert "error" in HB.register(reg, {"title": "T", "expected_direction": "positive",
                                        "source": {"type": "book", "ref": "x"}})   # 缺 statement
    assert "error" in HB.register(reg, {"title": "T", "statement": "S",
                                        "expected_direction": "up",
                                        "source": {"type": "book", "ref": "x"}})   # 方向非法
    assert "error" in HB.register(reg, {"title": "T", "statement": "S",
                                        "expected_direction": "positive",
                                        "source": {"type": "book", "ref": ""}})     # 缺来源
    ok = HB.register(reg, {"title": "成长延续", "statement": "营收高增个股中期跑赢",
                           "expected_direction": "positive",
                           "source": {"type": "report", "ref": "某研报 2026Q3"}})
    assert ok["registered"] is True and ok["hypothesis"]["id"] == "H-0001"


def test_register_allocates_sequential_id():
    reg = _hyp_reg()
    ok = HB.register(reg, {"title": "质量溢价", "statement": "高 ROE 中期跑赢",
                           "expected_direction": "positive",
                           "source": {"type": "book", "ref": "《量化的黑箱》第 3 章"}})
    assert ok["hypothesis"]["id"] == "H-0002"


# ── 血缘双向（§5.2）─────────────────────────────────────
def test_link_bidirectional_and_reverse_query():
    reg, freg = _hyp_reg(), _factor_reg()
    res = HB.link(reg, freg, "H-0001", "new_sig")
    assert res["linked"] is True
    # 因子→假设
    assert HB.factor_ref(freg, "new_sig")["hypothesis_id"] == "H-0001"
    # 假设→因子
    assert "new_sig" in HB.find(reg, "H-0001")["linked_factors"]
    # hypothesis.list --factor 反向可查
    got = HB.listing(reg, freg, factor="new_sig")
    assert got["n"] == 1 and got["hypotheses"][0]["id"] == "H-0001"
    assert got["factor"]["hypothesis_id"] == "H-0001"


def test_link_conflict_rejected():
    reg, freg = _hyp_reg(), _factor_reg()
    res = HB.link(reg, freg, "H-0001", "val_pe_pct")   # 已绑定同源 → 幂等通过
    assert res["linked"] is True
    # 构造第二假设，尝试把已绑定因子改绑 → 拒绝（不设旁路）
    HB.register(reg, {"id": "H-0002", "title": "T", "statement": "S",
                      "expected_direction": "positive", "source": {"type": "book", "ref": "x"}})
    bad = HB.link(reg, freg, "H-0002", "val_pe_pct")
    assert "error" in bad and "已绑定" in bad["error"]


# ── 激活门禁（§5.2 / M5 8.1）────────────────────────────
def test_activation_gate_rejects_draft_without_hypothesis():
    reg, freg = _hyp_reg(), _factor_reg()
    res = HB.activate(freg, reg, "new_sig")            # draft 且无 hypothesis_id
    assert res["activated"] is False
    assert "拒绝激活" in res["reason"] and "hypothesis_id" in res["reason"]
    assert HB.factor_ref(freg, "new_sig")["status"] == "draft"   # 未旁路激活


def test_activation_passes_with_hypothesis_id():
    reg, freg = _hyp_reg(), _factor_reg()
    HB.link(reg, freg, "H-0001", "new_sig")
    res = HB.activate(freg, reg, "new_sig")
    assert res["activated"] is True and res["hypothesis_id"] == "H-0001"
    assert HB.factor_ref(freg, "new_sig")["status"] == "active"


def test_activation_rejects_non_draft():
    reg, freg = _hyp_reg(), _factor_reg()
    res = HB.activate(freg, reg, "val_pe_pct")         # 已 active，非 draft
    assert res["activated"] is False and "仅 draft" in res["reason"]


# ── 状态机（§5.1）──────────────────────────────────────
def test_status_machine_transitions():
    reg = _hyp_reg()
    h = HB.find(reg, "H-0001")
    h["status"] = "proposed"
    assert HB.transition(reg, "H-0001", "testing")["to"] == "testing"
    assert "error" in HB.transition(reg, "H-0001", "proposed")   # 非法回退
    assert HB.transition(reg, "H-0001", "supported")["to"] == "supported"
    assert "error" in HB.transition(reg, "H-0001", "refuted")    # supported→refuted 非法


# ── 巡检：假设复查（§5.2 / M5 8.2）──────────────────────
def test_review_mandatory_when_weakening():
    reg, freg = _hyp_reg(), _factor_reg()
    HB.factor_ref(freg, "val_pe_pct")["status"] = "weakening"    # 人为置 weakening
    res = HB.review_items(freg, reg)
    assert res["n_mandatory"] == 1
    it = res["mandatory"][0]
    assert it["factor_id"] == "val_pe_pct" and it["hypothesis_id"] == "H-0001"
    assert it["question"] == "假设仍成立吗？"
    assert "假设仍成立吗" in HB.render_review_md(res)


def test_review_samples_20pct_of_healthy():
    hyps = [{"id": "H-0001", "title": "T", "status": "supported", "linked_factors": [],
             "source": {"type": "book", "ref": "x"}, "statement": "S",
             "expected_direction": "positive", "universe": ["CN"], "evidence_runs": []}]
    factors = [{"id": f"f{i:02d}", "name": f"F{i}", "type": "alpha", "status": "active",
                "version": "v1.0", "hypothesis_id": "H-0001"} for i in range(10)]
    res = HB.review_items({"factors": factors}, {"hypotheses": hyps})
    assert res["n_active"] == 10 and res["n_sample"] == 2        # 每 5 取 1 = 20%
    assert res["n_mandatory"] == 0
