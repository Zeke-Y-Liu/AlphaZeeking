"""data_missing 检测单点测试（T-R10）：正常/缺失/新鲜但 incomplete 三分支
+ 透明降级不阻断语义 + spec 声明零代码接入。
"""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from applib import datasets as DS
from applib.datasets import DatasetSpec, SourceSpec

TODAY = "2026-10-12"


def _spec(**over):
    base = dict(dataset_id="dwd_daily_bar", path="market/daily_bars.json",
                merge_policy="union_bars",
                sources=(SourceSpec(interface="ak.stock_zh_a_daily", upstream="新浪财经"),),
                validation={"min_rows": 100, "as_of_field": "dt"})
    base.update(over)
    return DatasetSpec(**base)


def _write(repo: Path, rel: str, payload):
    p = repo / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")


# ── 分支①：正常（新鲜 + 行数达标）→ ok ───────────────────────────────────
def test_ok_when_fresh_and_complete(tmp_path):
    rows = [{"dt": "2026-10-12", "close": 1.0} for _ in range(150)]
    _write(tmp_path, "market/daily_bars.json", {"000001.SZ": rows, "as_of": TODAY})
    out = DS.check_data_missing(repo=tmp_path, specs={"dwd_daily_bar": _spec()},
                                stale_days=7, today=TODAY)
    it = out["datasets"]["dwd_daily_bar"]
    assert it["status"] == "ok" and "reason" not in it
    assert it["checks"]["freshness"]["pass"] and it["checks"]["min_rows"]["pass"]


# ── 分支②：缺失（文件不存在）→ missing ──────────────────────────────────
def test_missing_when_file_absent(tmp_path):
    out = DS.check_data_missing(repo=tmp_path, specs={"dwd_daily_bar": _spec()},
                                stale_days=7, today=TODAY)
    it = out["datasets"]["dwd_daily_bar"]
    assert it["status"] == "missing"
    assert it["checks"]["data_missing"]["pass"] is False
    assert "不存在" in it["reason"]
    assert out["summary"]["missing"] == 1 and out["summary"]["ok"] == 0


# ── 分支③：新鲜但 incomplete（行数下限不达标）→ incomplete ───────────────
def test_incomplete_when_fresh_but_short(tmp_path):
    rows = [{"dt": "2026-10-10", "close": 1.0}] * 3
    _write(tmp_path, "market/daily_bars.json", {"000001.SZ": rows})
    out = DS.check_data_missing(repo=tmp_path, specs={"dwd_daily_bar": _spec()},
                                stale_days=7, today=TODAY)
    it = out["datasets"]["dwd_daily_bar"]
    assert it["status"] == "incomplete"
    assert it["checks"]["freshness"]["pass"] is True          # 新鲜
    assert it["checks"]["min_rows"]["pass"] is False          # 行数不足


# ── 其余分支：stale / invalid / severity 单值取最严重 ────────────────────
def test_stale_when_old_data(tmp_path):
    _write(tmp_path, "market/daily_bars.json",
           {"000001.SZ": [{"dt": "2026-09-01", "close": 1.0}] * 200})
    out = DS.check_data_missing(repo=tmp_path, specs={"dwd_daily_bar": _spec()},
                                stale_days=7, today=TODAY)
    assert out["datasets"]["dwd_daily_bar"]["status"] == "stale"


def test_invalid_when_corrupted_json(tmp_path):
    p = tmp_path / "market" / "daily_bars.json"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("{broken json", encoding="utf-8")
    out = DS.check_data_missing(repo=tmp_path, specs={"dwd_daily_bar": _spec()},
                                stale_days=7, today=TODAY)
    it = out["datasets"]["dwd_daily_bar"]
    assert it["status"] == "invalid" and "JSON" in it["reason"]


def test_multi_defect_takes_most_severe(tmp_path):
    # stale + incomplete 同时存在 → 取更严重 incomplete（行数不足比过期严重）
    _write(tmp_path, "market/daily_bars.json",
           {"000001.SZ": [{"dt": "2026-09-01", "close": 1.0}] * 2})
    out = DS.check_data_missing(repo=tmp_path, specs={"dwd_daily_bar": _spec()},
                                stale_days=7, today=TODAY)
    assert out["datasets"]["dwd_daily_bar"]["status"] == "incomplete"


# ── 透明降级语义：检测全程不抛异常、不阻断（缺失也不 raise）────────────────
def test_never_raises_and_carries_semantics(tmp_path):
    # 全部数据集缺失 → 仍正常返回结构化结果，不失败
    out = DS.check_data_missing(repo=tmp_path,
                                specs={"a": _spec(dataset_id="a"),
                                       "b": _spec(dataset_id="b", path="x/y.json")},
                                stale_days=7, today=TODAY)
    assert out["semantics"].startswith("提示不阻断")
    assert all(it["status"] == "missing" for it in out["datasets"].values())
    assert out["summary"] == {"ok": 0, "stale": 0, "incomplete": 0,
                              "invalid": 0, "missing": 2}


# ── 零代码接入：新 spec 只写 validation 声明即自动纳入检测 ─────────────────
def test_new_dataset_zero_code_via_validation_decl(tmp_path):
    _write(tmp_path, "alt/new_set.json", {"indicators": {"v": 1}, "as_of": TODAY})
    spec = _spec(dataset_id="alt_new_set", path="alt/new_set.json",
                 merge_policy="overwrite",
                 validation={"required_keys": ["indicators"], "as_of_field": "as_of"})
    out = DS.check_data_missing(repo=tmp_path, specs={"alt_new_set": spec},
                                stale_days=7, today=TODAY)
    assert out["datasets"]["alt_new_set"]["status"] == "ok"


# ── 模板路径：glob 命中即有数据（trade_calendar_{mkt}.json 形态）──────────
def test_template_path_glob_hit(tmp_path):
    _write(tmp_path, "market/trade_calendar_A.json", {"days": ["2026-10-09"]})
    spec = _spec(dataset_id="ods_trade_calendar", path="market/trade_calendar_{mkt}.json",
                 merge_policy="overwrite", validation={})
    out = DS.check_data_missing(repo=tmp_path, specs={"ods_trade_calendar": spec},
                                stale_days=7, today=TODAY)
    assert out["datasets"]["ods_trade_calendar"]["status"] == "ok"


# ── 全量 registry 冒烟：15 项声明全部可检测（结构性自检）──────────────────
def test_full_registry_smoke(tmp_path):
    out = DS.check_data_missing(repo=tmp_path, stale_days=7, today=TODAY)
    assert len(out["datasets"]) >= 15
    assert set(out["summary"].keys()) == {"ok", "stale", "incomplete", "invalid", "missing"}
    # 空仓下全部 missing，也不抛异常（不阻断语义的全量证明）
    assert all(it["status"] == "missing" for it in out["datasets"].values())
