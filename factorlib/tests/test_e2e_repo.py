"""端到端回归（DoD-1）：真实 repo 数据走完 注册→计算→物化→查询→审计→复现 全流程，无人工干预。

数据源：alphapocket/remote/repo（market/daily_bars.json + factors/fundamental_values.json），
经独立建库装载（与 applib data.py 双实现交叉锚定数据契约）。
"""
import pytest

from conftest import ALPHAPOCKET_REPO, sql_from_yaml

pytestmark = pytest.mark.skipif(
    not (ALPHAPOCKET_REPO / "market/daily_bars.json").exists(),
    reason="alphapocket remote repo 不存在（端到端跳过）")


def test_e2e_register_materialize_query_audit(repo_con):
    from factorlib import load_definitions, materialize, pit_audit, verify_reproducible
    from factorlib.registry import set_status

    # 1) 注册：YAML → FactorMeta（含校验与排序；root = factorlib 仓库根）
    metas = load_definitions(ALPHAPOCKET_REPO.parent.parent.parent / "factorlib")
    meta = [m for m in metas if m.id == "val_pe_ttm"][0]
    assert meta.engine == "sql" and meta.status == "draft"
    meta = set_status(meta, "active")                       # draft → active

    # 2) 计算+物化：full 全量
    r = materialize(repo_con, meta, mode="full")
    assert r["rows"] > 0

    # 3) 查询：长表可按 (factor_id, dt) 消费
    dt, n = repo_con.execute(
        "SELECT max(dt), count(*) FROM factor_value_daily WHERE factor_id = 'val_pe_ttm'"
    ).fetchone()
    assert n == r["rows"]

    # 4) PIT 审计：0 违例（红线）
    rep = pit_audit(repo_con, factor_id="val_pe_ttm")
    assert rep["checked"] == r["pit_rows"]
    assert rep["violations"] == 0

    # 5) 复现性：两次全量重算逐行 diff = 0
    rr = verify_reproducible(repo_con, meta)
    assert rr["reproducible"] is True


# ── M2 端到端（实施计划 §5）：全部注册因子（M2 新增 11 + M1 val_pe_ttm = 12）──
# 全量物化 → 审计 0 违例 → 复现 diff=0 → 公告日抢跑抽检
M2_FIDS = ["flt_avg_amount_20d", "flt_listed_days", "flt_st",
           "qua_accruals", "qua_debt_to_asset", "qua_gross_margin",
           "qua_ocf_to_np", "qua_roe_ttm",
           "val_div_yield", "val_pb_lf", "val_pe_ttm", "val_ps_ttm"]


@pytest.fixture(scope="module")
def m2_repo_con(repo_con):
    """真实 repo 库：12 因子全量物化一次（module 级复用，含 val_pe_ttm 共 12 个注册因子）。"""
    from factorlib import load_definitions, materialize
    metas = {m.id: m for m in load_definitions(
        ALPHAPOCKET_REPO.parent.parent.parent / "factorlib")}
    results = {}
    for fid in M2_FIDS:
        results[fid] = materialize(repo_con, metas[fid], mode="full")
    return repo_con, metas, results


def test_e2e_m2_materialize_and_audit(m2_repo_con):
    """M2 DoD-1：12 因子真实数据端到端物化；财务因子 PIT 审计 0 违例（红线）。"""
    con, _, results = m2_repo_con
    from factorlib import pit_audit
    total_pit = 0
    for fid in M2_FIDS:
        r = results[fid]
        assert r["rows"] > 0, f"{fid} 物化 0 行"
        total_pit += r["pit_rows"]
        rep = pit_audit(con, factor_id=fid)
        assert rep["violations"] == 0, f"{fid} PIT 违例 {rep['violations']}：{rep['samples'][:3]}"
        assert rep["checked"] == r["pit_rows"]
    # 财报类因子必须产出 PIT 元数据（估值/质量 8 因子）
    assert total_pit > 0


def test_e2e_m2_reproducible(m2_repo_con):
    """M2 DoD-5：每因子两次全量重算逐行 diff = 0。"""
    from factorlib import verify_reproducible
    con, metas, _ = m2_repo_con
    for fid in M2_FIDS:
        rr = verify_reproducible(con, metas[fid])
        assert rr["reproducible"] is True, f"{fid} 复现 diff != 0：{rr['digest_1']} vs {rr['digest_2']}"


def test_e2e_m2_no_front_running(m2_repo_con):
    """M2 DoD-6：公告日前后 5 日抽检——因子值/口径切换不早于公告日（无抢跑）。

    案例（真实数据确定性选取）：002014.SZ / val_pe_ttm，used_announce 首个切换点。
    断言：切换日 dt >= 新 used_announce；前 5 日口径仍为旧公告；后 5 日口径为新公告。
    （全域 used_announce <= dt 已由 pit_audit 覆盖；此处验证切换行为可见且时序正确。）
    """
    con, _, _ = m2_repo_con
    rows = con.execute("""
        WITH s AS (SELECT CAST(dt AS VARCHAR) AS dt, used_announce, used_period,
                   lag(used_announce) OVER (ORDER BY dt) AS prev_ann
                   FROM factor_pit_meta
                   WHERE factor_id = 'val_pe_ttm' AND secid = '002014.SZ')
        SELECT dt, used_announce, used_period FROM s
        WHERE prev_ann IS NOT NULL AND used_announce != prev_ann ORDER BY dt
    """).fetchall()
    assert rows, "002014.SZ 应存在公告切换点"
    for dt, ann, period in rows:
        assert str(dt) >= str(ann), f"切换日 {dt} 早于公告日 {ann}（抢跑）"
    # 首/末切换点 ±5 日窗口行为核对
    dt0, ann0, per0 = str(rows[0][0]), str(rows[0][1]), str(rows[0][2])
    before = con.execute("""
        SELECT count(*) FROM factor_pit_meta
        WHERE factor_id = 'val_pe_ttm' AND secid = '002014.SZ'
          AND dt >= CAST(? AS DATE) - 5 AND dt < CAST(? AS DATE)
          AND used_period = CAST(? AS DATE)
    """, [dt0, dt0, per0]).fetchone()[0]
    assert before == 0, f"公告 {ann0} 前 5 日窗口内已出现新口径 {per0}（抢跑）"
    after = con.execute("""
        SELECT count(*) FROM factor_pit_meta
        WHERE factor_id = 'val_pe_ttm' AND secid = '002014.SZ'
          AND dt >= CAST(? AS DATE) AND dt <= CAST(? AS DATE) + 5
          AND used_period = CAST(? AS DATE)
    """, [dt0, dt0, per0]).fetchone()[0]
    assert after >= 1, f"公告 {ann0} 起应使用新口径 {per0}"
