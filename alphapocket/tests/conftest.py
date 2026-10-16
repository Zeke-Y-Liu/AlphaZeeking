"""factorlib 测试共享 fixture：golden 合成 repo 副本 + prepare 后的 DuckDB。

运行约定：``cd alphapocket && python -m pytest tests/ -q``（cwd 在 sys.path → import factorlib）。
fixture 产物提交于 tests/fixtures/fl_golden（由 make_fixture.py 生成，期望值为手工核对表）。
"""
from __future__ import annotations

import shutil
from pathlib import Path

import pytest

FIXTURE_DIR = Path(__file__).resolve().parent / "fixtures" / "fl_golden"
CHECKPOINTS = ("2021-01-29", "2021-02-26", "2021-04-29", "2021-04-30")


@pytest.fixture
def fl_repo(tmp_path) -> Path:
    """golden 合成 repo 的可变副本（状态文件/物化产物写入 tmp，不污染提交物）。"""
    repo = tmp_path / "repo"
    shutil.copytree(FIXTURE_DIR / "repo", repo)
    return repo


@pytest.fixture
def fl_db(fl_repo, tmp_path):
    """repo → fl_* 输入表（prepare）。返回 (db_path, prepare摘要)。"""
    from factorlib.prepare import prepare
    db = tmp_path / "fl.duckdb"
    summary = prepare(fl_repo, db)
    return db, summary


@pytest.fixture
def golden():
    """手工期望值表：{factor_id, checkpoints, cases:[{secid, dt, close, ttm_eps, expected,
    used_period, used_announce}]}。"""
    import json
    return json.loads((FIXTURE_DIR / "golden" / "golden_pe_ttm.json").read_text(encoding="utf-8"))
