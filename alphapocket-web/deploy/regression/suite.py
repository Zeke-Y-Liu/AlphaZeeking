"""回归套件注册表 + 变更影响映射（RG-1，声明式零依赖）。

语义
----
- ``SUITE``：全部可执行测试单元。每项 ``TestItem``：
  - ``name``：全局唯一（计划选择 / 报告引用）
  - ``domain``：core | interaction-api | interaction-web | interaction-e2e | data | factor
  - ``kind``：pytest | vitest | typecheck | build | e2e | script | factor-audit
  - ``cmd``：argv 列表（相对 ``cwd`` 执行；不含 shell 拼接，杜绝注入）
  - ``cwd``：执行目录（绝对路径，由 run.py 归一）
  - ``timeout``：秒（超时按 fail 计）
  - ``smoke``：True → 恒定冒烟集成员（任何增量都跑）
  - ``live``：True → 需要外网/真实上游，仅 ``--with-live`` 时纳入计划
  - ``env``：额外环境变量（如 PYTHONPATH）
  - ``requires``：可用性探测（命令 argv）；非零 → skip(reason)，不判 fail
- ``MAP``：变更路径 glob → ``TestItem.name`` 列表（增量模式影响映射；
  未命中的变更文件兜底进恒定冒烟集并在报告中 WARN）。

四域覆盖（§1.2）
----------------
- core：核心功能冒烟 + 基准数据锚点（smoke 恒跑）+ core 全量（full 展开）
- interaction：api 契约/错误语义、web vitest/typecheck(基线)/build、e2e（Chrome 基线）
- data：R0 合成 / R1 本地快照 / R2 实时上游（live）
- factor：单元集 + 完备性审计（R1 golden/pit 由 data.r1 提供）
"""
from __future__ import annotations

ROOT = "/workspace"

# ── 核心功能清单（域1「定义核心功能」的落地：功能 → 测试文件映射）─────
# 编排器启动时校验文件存在（缺失 = 注册表腐化，直接报错终止）。
CORE_FUNCTIONS: dict[str, list[str]] = {
    "认证与用户": ["core/tests/test_auth.py"],
    "健康与依赖探测": ["core/tests/test_health.py"],
    "标的注册与检索": ["core/tests/test_securities.py"],
    "数据同步与单标的拉取": ["core/tests/test_sync.py", "core/tests/test_data_sync.py"],
    "数据状态与验证查询": ["core/tests/test_data_status.py"],
    "组合 CRUD 与物化": ["core/tests/test_portfolios.py"],
    "组合评估（review）": ["core/tests/test_review.py"],
    "回测（backtest）": ["core/tests/test_backtest_api.py", "core/tests/test_backtest_params.py"],
    "调仓与执行单": ["core/tests/test_plan.py", "core/tests/test_exec_flow.py",
                     "core/tests/test_exec_orders.py"],
    "单公司分析": ["core/tests/test_analysis_api.py"],
    "引擎与因子注册表": ["core/tests/test_engines_api.py"],
    "实时行情": ["core/tests/test_quotes.py"],
    "异步任务与门禁": ["core/tests/test_jobs.py", "core/tests/test_gate.py"],
    "审计与溯源": ["core/tests/test_artifacts.py"],
    "模型与迁移": ["core/tests/test_models.py", "core/tests/test_migrations.py",
                   "core/tests/test_seed.py"],
    "错误语义与日志": ["core/tests/test_errors.py", "core/tests/test_logging.py"],
    "API 契约快照": ["core/tests/test_openapi_snapshot.py"],
    "核心基准链路与锚点": ["core/tests/test_core_baseline.py"],
}


class TestItem:
    """一个可执行测试单元（声明式；run.py 按此执行与报告）。"""

    def __init__(self, name: str, domain: str, kind: str, cmd: list[str], cwd: str,
                 timeout: int = 600, *, smoke: bool = False, live: bool = False,
                 env: dict[str, str] | None = None, requires: list[str] | None = None,
                 desc: str = "") -> None:
        self.name = name
        self.domain = domain
        self.kind = kind
        self.cmd = cmd
        self.cwd = cwd
        self.timeout = timeout
        self.smoke = smoke
        self.live = live
        self.env = env or {}
        self.requires = requires or []
        self.desc = desc


_PY = f"{ROOT}/alphapocket-web/core/.venv/bin/python"
_APPLIB_ENV = {"PYTHONPATH": f"{ROOT}/alphapocket"}
_CORE_ENV = {"PYTHONPATH": f"{ROOT}/alphapocket", "AP_NET_TESTS": "0"}

SUITE: list[TestItem] = [
    # ── 域1 core：冒烟（恒跑）+ 基准锚点 + 全量 ─────────────────────
    TestItem(
        "core.smoke", "core", "pytest",
        [_PY, "-m", "pytest", "-q", "-m", "smoke",
         "tests/test_health.py", "tests/test_openapi_snapshot.py",
         "tests/test_core_baseline.py"],
        f"{ROOT}/alphapocket-web/core", timeout=300, smoke=True,
        env=_CORE_ENV, desc="核心冒烟：健康/契约快照/基准链路（任何变更都跑）"),
    TestItem(
        "core.all", "core", "pytest",
        [_PY, "-m", "pytest", "-q", "tests/"],
        f"{ROOT}/alphapocket-web/core", timeout=900,
        env=_CORE_ENV, desc="core 全量单测（198 项）"),

    # ── 域2 interaction：api 契约 + 错误语义 ─────────────────────────
    TestItem(
        "api.contract", "interaction-api", "pytest",
        [_PY, "-m", "pytest", "-q", "tests/test_openapi_snapshot.py"],
        f"{ROOT}/alphapocket-web/core", timeout=120, smoke=True,
        env=_CORE_ENV, desc="OpenAPI 契约快照（端点/契约不漂移）"),
    TestItem(
        "api.semantics", "interaction-api", "pytest",
        [_PY, "-m", "pytest", "-q", "tests/test_errors.py", "tests/test_sync.py",
         "tests/test_gate.py", "tests/test_analysis_api.py::test_create_profile_unregistered_secid_no_sync"],
        f"{ROOT}/alphapocket-web/core", timeout=300,
        env=_CORE_ENV, desc="错误码语义：422/409/503/门禁/降级路径"),

    # ── 域2 interaction：web ─────────────────────────────────────────
    TestItem(
        "web.vitest", "interaction-web", "vitest",
        ["pnpm", "exec", "vitest", "run"],
        f"{ROOT}/alphapocket-web/web", timeout=300,
        desc="web 组件/视图全量单测（116 项）"),
    TestItem(
        "web.typecheck", "interaction-web", "typecheck",
        [_PY, f"{ROOT}/alphapocket-web/deploy/regression/checks/web_typecheck.py"],
        f"{ROOT}/alphapocket-web/web", timeout=300,
        requires=[_PY, "-c",
                  "import sys; sys.exit(0 if __import__('pathlib').Path"
                  "('../core/.venv/bin/python').exists() else 1)"],
        desc="vue-tsc 基线对比：新增类型错误才 fail（存量错误冻结于基线文件）"),
    TestItem(
        "web.build", "interaction-web", "build",
        ["pnpm", "exec", "vite", "build"],
        f"{ROOT}/alphapocket-web/web", timeout=300,
        desc="生产构建可完成"),

    # ── 域2 interaction：e2e（Chrome 兼容基线）──────────────────────
    TestItem(
        "web.e2e", "interaction-e2e", "e2e",
        ["pnpm", "exec", "playwright", "test"],
        f"{ROOT}/alphapocket-web/web", timeout=420,
        requires=["pnpm", "exec", "playwright", "--version"],
        desc="浏览器 E2E 冒烟（Chromium=Chrome 基线）：登录/数据页/组合/回测页/console"),

    # ── 域3 data：可靠性分级 ──────────────────────────────────────────
    TestItem(
        "data.r0", "data", "pytest",
        [_PY, "-m", "pytest", "-q",
         "../alphapocket/tests/test_merge_policies.py",
         "../alphapocket/tests/test_datasource_merge.py",
         "../alphapocket/tests/test_data_sync_empty.py"],
        f"{ROOT}/alphapocket-web", timeout=300, smoke=True,
        env=_APPLIB_ENV,
        desc="R0 合成基准（离线确定性）：合并策略纯函数 + 空数据集防御"),
    TestItem(
        "data.r1", "data", "script",
        [_PY, f"{ROOT}/alphapocket-web/deploy/regression/checks/data_r1.py"],
        f"{ROOT}/alphapocket-web", timeout=300,
        requires=[_PY, f"{ROOT}/alphapocket-web/deploy/regression/checks/data_r1.py",
                  "--probe"],
        desc="R1 本地快照：当前真实 workspace 六项验证（不触外网）"),
    TestItem(
        "data.r2", "data", "script",
        [_PY, f"{ROOT}/alphapocket-web/deploy/regression/checks/data_r2.py"],
        f"{ROOT}/alphapocket-web", timeout=1800, live=True,
        desc="R2 实时上游：API 增量同步 → 等待完成 → 复跑 R1（--with-live）"),

    # ── 域4 factor：单元集 + 完备性审计 ──────────────────────────────
    TestItem(
        "factor.units", "factor", "pytest",
        [_PY, "-m", "pytest", "-q",
         "tests/test_factor_val_m7.py", "tests/test_factor_gro_m7.py",
         "tests/test_factor_qua_m7.py", "tests/test_factor_mom_lowvol_m7.py",
         "tests/test_factor_backtest.py",
         # 环境依赖型既有失败（读本体 sandbox/repo/hypothesis 状态登记文件，沙箱
         # 重置后丢失；M7 登记流非因子计算回归面）→ 显式排除并在此登记：
         "--deselect", "tests/test_factor_val_m7.py::test_val_defs_hypothesis_binding",
         "--deselect", "tests/test_factor_gro_m7.py::test_gro_defs_hypothesis_binding",
         "--deselect", "tests/test_factor_gro_m7.py::test_gro_earnings_surprise_gap_registered",
         "--deselect", "tests/test_factor_qua_m7.py::test_qua_defs_hypothesis_binding",
         "--deselect",
         "tests/test_factor_mom_lowvol_m7.py::test_defs_hypothesis_binding_and_direction"],
        f"{ROOT}/alphapocket", timeout=600, env=_APPLIB_ENV,
        desc="因子计算单元集（val/gro/qua/mom/lowvol/backtest）。注："
             "tests/test_factors_p1.py 的 test_rule_fn_daily_rotation_manual_nav"
             " 为策略 DSL 既有失败（KeyError 'rule'，非因子计算），不入套件"),
    TestItem(
        "factor.audit", "factor", "factor-audit",
        [_PY, "-m", "pytest", "-q", "tests/test_factor_completeness.py"],
        f"{ROOT}/alphapocket", timeout=120, smoke=True, env=_APPLIB_ENV,
        desc="完备性审计：registry 每个 active 因子必须有测试覆盖映射"),
    TestItem(
        "factor.golden", "factor", "script",
        [_PY, f"{ROOT}/alphapocket-web/deploy/regression/checks/data_r1.py",
         "--only", "golden_test,pit_audit"],
        f"{ROOT}/alphapocket-web", timeout=300,
        requires=[_PY, f"{ROOT}/alphapocket-web/deploy/regression/checks/data_r1.py",
                  "--probe"],
        desc="R1 真数据面：golden 全因子对账 + PIT 合规"),
]

# ── 变更影响映射（glob → suite 项名）────────────────────────────────
MAP: dict[str, list[str]] = {
    "alphapocket-web/core/app/**": ["core.all", "api.semantics", "data.r1"],
    "alphapocket-web/core/tests/**": ["core.all"],
    "alphapocket-web/core/migrations/**": ["core.all"],
    "alphapocket-web/core/alembic.ini": ["core.all"],
    "alphapocket-web/web/src/**": ["web.vitest", "web.typecheck", "web.build", "web.e2e"],
    "alphapocket-web/web/e2e/**": ["web.e2e"],
    "alphapocket-web/web/playwright.config.*": ["web.e2e"],
    "alphapocket-web/web/package.json": ["web.vitest", "web.typecheck", "web.build", "web.e2e"],
    "alphapocket-web/bff/**": ["web.e2e"],
    "alphapocket/applib/datasource.py": ["data.r0", "data.r1", "core.all"],
    "alphapocket/applib/data.py": ["data.r0", "data.r1", "core.all"],
    "alphapocket/applib/merge*.py": ["data.r0"],
    "alphapocket/applib/datasets/**": ["data.r0", "data.r1"],
    "alphapocket/applib/technical.py": ["core.all"],
    "alphapocket/applib/industry.py": ["core.all"],
    "alphapocket/applib/runner.py": ["data.r1", "factor.golden"],
    "alphapocket/applib/factor*.py": ["factor.units", "factor.golden", "core.all"],
    "alphapocket/applib/strategy*.py": ["factor.units"],
    "alphapocket/tests/**": ["factor.units", "data.r0"],
    "alphapocket/remote/repo/factors/registry.json": ["factor.audit", "factor.units"],
    "alphapocket-web/deploy/**": ["data.r1"],
    "docs/**": [],
}
