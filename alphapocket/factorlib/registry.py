"""因子注册中心（架构文档 §15）：FactorMeta 元数据 + YAML 定义加载 + 状态机 + 依赖解析 + lookback。

设计要点：
- 因子定义即代码：defs/**/*.yaml 为唯一真源（Git 管理，评审合入）；
- 状态机落盘：sandbox/repo/factors/factor_registry_state.json 保存 status 与迁移历史
  （YAML 保持不可变，状态属运行时可变数据，二者合并为完整 FactorMeta 视图）；
- 状态机：draft → active → deprecated 单向（§21），非法迁移抛 RegistryError；
- 健康维度（M5）：health 与 lifecycle 两维分离——healthy/weakening/invalid/unknown
  是巡检产物、可恢复迁移（invalid 不自动回 healthy），只写状态文件，不进 FactorMeta；
- 血缘（M5）：hypothesis_id 绑定假设登记表编号（R3「无来源不激活」的 factorlib 载体），
  YAML 可选携带、状态文件值优先；
- 版本语义（M5）：MAJOR.MINOR.PATCH 三段式，口径变更 = bump_version 升版（不触发物化）；
- 依赖解析：meta.inputs 声明所需数据表，物化前校验表存在；
- lookback 校验：resolve_backtest_start 返回「全部因子历史充足」的最早回测起点。
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field, asdict
from datetime import date, datetime
from pathlib import Path

import yaml

# 合法引擎（架构文档 §16：三种表达方式）
ENGINES = ("sql", "qlib_expr", "python_udf")
# 合法因子分层（§14 F1–F5）
CATEGORIES = ("F1_technical", "F2_fundamental", "F3_flow", "F4_event", "F5_personal")
# 合法状态与单向迁移（§21 / 计划 M1.1）
STATUSES = ("draft", "active", "deprecated")
_TRANSITIONS = {"draft": ("active",), "active": ("deprecated",), "deprecated": ()}
# 健康维度（M5 / 计划 8.2）：巡检产出、可恢复；invalid 不自动回 healthy（须假设复查人工路径）
HEALTH = ("healthy", "weakening", "invalid", "unknown")
_HEALTH_TRANSITIONS = {
    "healthy":     {"weakening", "invalid", "unknown"},
    "weakening":   {"healthy", "invalid", "unknown"},
    "invalid":     {"unknown"},              # 只允许转 unknown（复查中），不直通 healthy
    "unknown":     {"healthy", "weakening", "invalid"},
}
# 版本号：MAJOR.MINOR.PATCH 三段式（M5 / 计划 8.3 版本冻结）
_VERSION_RE = re.compile(r"^\d+\.\d+\.\d+$")
# id 规范：{类别前缀}_{口径}_{窗口}，全小写蛇形（§14 命名规范）
_ID_RE = re.compile(r"^[a-z][a-z0-9]*(_[a-z0-9]+)*$")

# 必填字段（§15 FactorMeta）
_REQUIRED = ("id", "name", "category", "factor_type", "engine", "direction", "universe",
             "inputs", "lookback_days", "pit", "version", "status", "description")
# signal 因子持有窗口硬约束（§15：horizon_days ∈ [10, 30]）
_SIGNAL_HORIZON = (10, 30)


class RegistryError(Exception):
    """注册中心校验/迁移违例。"""


@dataclass
class FactorMeta:
    """因子元数据（§15 FactorMeta）。definition=True 表示来自 YAML 真源，False 表示状态文件补充。"""
    id: str
    name: str
    category: str
    factor_type: str          # alpha | signal
    engine: str               # sql | qlib_expr | python_udf
    direction: str            # high | low | both
    universe: list
    inputs: list              # 依赖数据表名（依赖解析用）
    lookback_days: int
    pit: bool
    version: str
    status: str
    description: str
    horizon_days: int | None = None
    freq: str = "1d"
    formula: str | None = None          # 声明式表达式（人类可读）
    neutralization: list = field(default_factory=list)
    transform: list = field(default_factory=list)
    owner: str = "system"
    sql: str | None = None              # engine=sql 时的可执行 SQL（$start/$end 参数绑定）
    udf: str | None = None              # engine=python_udf 时的函数源码
    expr: str | None = None             # engine=qlib_expr 时的表达式
    source_path: str | None = None      # YAML 来源（调试/血缘）
    hypothesis_id: str | None = None    # 血缘：假设登记表编号（M5 门禁硬校验；YAML 可选携带）

    def to_dict(self) -> dict:
        return asdict(self)


# ── 校验 ─────────────────────────────────────────────
def validate(meta: FactorMeta) -> list[str]:
    """返回违例列表（空 = 通过）。register/CLI validate 共用。"""
    errs: list[str] = []
    if not _ID_RE.match(meta.id or ""):
        errs.append(f"id 不符合蛇形命名规范：{meta.id!r}")
    if meta.category not in CATEGORIES:
        errs.append(f"category 非法：{meta.category!r}（允许 {CATEGORIES}）")
    if meta.factor_type not in ("alpha", "signal"):
        errs.append(f"factor_type 非法：{meta.factor_type!r}")
    if meta.engine not in ENGINES:
        errs.append(f"engine 非法：{meta.engine!r}（允许 {ENGINES}）")
    if meta.direction not in ("high", "low", "both"):
        errs.append(f"direction 非法：{meta.direction!r}")
    if meta.status not in STATUSES:
        errs.append(f"status 非法：{meta.status!r}")
    if not _VERSION_RE.match(meta.version or ""):
        errs.append(f"version 须为 MAJOR.MINOR.PATCH 三段式：{meta.version!r}")
    if not meta.inputs:
        errs.append("inputs 不能为空（依赖解析需要）")
    if not isinstance(meta.lookback_days, int) or meta.lookback_days < 0:
        errs.append(f"lookback_days 必须为非负整数：{meta.lookback_days!r}")
    if meta.pit is not True:
        errs.append("pit 必须为 true（PIT 红线：中长期因子库不接受非 point-in-time 定义）")
    if meta.engine == "sql" and not (meta.sql or "").strip():
        errs.append("engine=sql 但缺少 sql 字段")
    if meta.engine == "python_udf" and not (meta.udf or "").strip():
        errs.append("engine=python_udf 但缺少 udf 字段")
    if meta.engine == "qlib_expr" and not (meta.expr or "").strip():
        errs.append("engine=qlib_expr 但缺少 expr 字段")
    if meta.factor_type == "signal":
        lo, hi = _SIGNAL_HORIZON
        if not (meta.horizon_days and lo <= meta.horizon_days <= hi):
            errs.append(f"signal 因子 horizon_days 必须在 [{lo},{hi}]：{meta.horizon_days!r}")
    return errs


# ── YAML 加载（定义真源 + 状态合并）──────────────────
def _meta_from_yaml(d: dict, path: Path) -> FactorMeta:
    missing = [k for k in _REQUIRED if k not in d]
    if missing:
        raise RegistryError(f"{path.name}: 缺少必填字段 {missing}")
    m = FactorMeta(
        id=str(d["id"]), name=str(d["name"]), category=str(d["category"]),
        factor_type=str(d["factor_type"]), engine=str(d["engine"]), direction=str(d["direction"]),
        universe=list(d["universe"]), inputs=[str(x) for x in d["inputs"]],
        lookback_days=int(d["lookback_days"]), pit=bool(d["pit"]),
        version=str(d["version"]), status=str(d["status"]), description=str(d["description"]),
        horizon_days=d.get("horizon_days"), freq=str(d.get("freq", "1d")),
        formula=d.get("formula"), neutralization=list(d.get("neutralization", [])),
        transform=list(d.get("transform", [])), owner=str(d.get("owner", "system")),
        sql=d.get("sql"), udf=d.get("udf"), expr=d.get("expr"),
        source_path=str(path), hypothesis_id=d.get("hypothesis_id"),
    )
    errs = validate(m)
    if errs:
        raise RegistryError(f"{path.name}: " + "；".join(errs))
    return m


def _state_path(repo: Path) -> Path:
    return repo / "factors" / "factor_registry_state.json"


def _load_state(repo: Path) -> dict:
    p = _state_path(repo)
    if not p.exists():
        return {}
    return json.loads(p.read_text(encoding="utf-8"))


def defs_dir(repo: Path) -> Path:
    """因子定义目录（repo 下 defs/；默认随包分发，可被 repo 内同名文件覆盖）。"""
    pkg = Path(__file__).resolve().parent / "defs"
    repo_defs = repo / "factors" / "defs"
    return repo_defs if repo_defs.exists() else pkg


def _def_search_dirs(repo: Path) -> list[Path]:
    """定义搜索序（T-F9）：repo 内 defs（override）优先，包内 defs 兜底——两目录并集，
    按 id 去重（repo 同名 id 覆盖包内定义，跨目录重复不报错）。

    背景：旧 defs_dir 语义是「repo defs 存在则整体遮蔽包 defs」，repo 内落一个演练
    候选定义会把全部包内因子从注册视图里挤掉——合并语义下 repo defs 是叠加扩展位。
    """
    dirs: list[Path] = []
    repo_defs = repo / "factors" / "defs"
    if repo_defs.exists():
        dirs.append(repo_defs)
    dirs.append(Path(__file__).resolve().parent / "defs")
    return dirs


def load_factor(repo: Path, factor_id: str) -> FactorMeta:
    """加载单因子（YAML 定义 + 状态合并）。找不到抛 RegistryError。"""
    for base in _def_search_dirs(repo):
        hits = sorted(base.rglob(f"{factor_id}.yaml"))
        if hits:
            meta = _meta_from_yaml(yaml.safe_load(hits[0].read_text(encoding="utf-8")), hits[0])
            break
    else:
        raise RegistryError(f"因子定义不存在：{factor_id}"
                            f"（搜索 {[str(d) for d in _def_search_dirs(repo)]}）")
    st = _load_state(repo).get(factor_id)
    if st:  # 状态文件覆盖 YAML 内的静态声明（运行时真源）
        meta.status = st.get("status", meta.status)
        meta.version = st.get("version", meta.version)
        meta.hypothesis_id = st.get("hypothesis_id", meta.hypothesis_id)
    return meta


def load_registry(repo: Path) -> list[FactorMeta]:
    """加载全部因子定义（包 defs + repo defs 合并，repo 同名 id 覆盖），id 唯一性校验。"""
    metas: list[FactorMeta] = []
    owner: dict[str, Path] = {}                        # id → 首见目录（高优先 = repo）
    for base in _def_search_dirs(repo):
        for p in sorted(base.rglob("*.yaml")):
            meta = _meta_from_yaml(yaml.safe_load(p.read_text(encoding="utf-8")), p)
            if meta.id in owner:
                if owner[meta.id] == base:             # 同目录真重复 = 定义错误
                    raise RegistryError(f"因子 id 重复：{meta.id}（{owner[meta.id]} 与 {p}）")
                continue                               # 跨目录：repo 优先覆盖，包内让位
            owner[meta.id] = base
            st = _load_state(repo).get(meta.id)
            if st:
                meta.status = st.get("status", meta.status)
                meta.version = st.get("version", meta.version)
                meta.hypothesis_id = st.get("hypothesis_id", meta.hypothesis_id)
            metas.append(meta)
    return sorted(metas, key=lambda m: m.id)   # id 序：跨文件系统/目录布局确定性


# ── 状态机（§21）─────────────────────────────────────
def set_status(repo: Path, factor_id: str, new_status: str, who: str = "system",
               why: str = "", *, gate: dict | None = None) -> dict:
    """状态迁移：draft→active→deprecated 单向；历史追加写状态文件。返回迁移记录。

    draft→active 强制门禁（M5 8.1）：gate 须由调用方先跑 factorlib.lifecycle.
    activation_gate 再传入（registry 保持零 DB 依赖）；gate=None 或 gate.ok=False
    一律拒绝——激活无旁路，唯一入口 ap fl activate。active→deprecated 退役不加门禁。
    """
    if new_status not in STATUSES:
        raise RegistryError(f"目标状态非法：{new_status!r}")
    meta = load_factor(repo, factor_id)
    old = meta.status
    if new_status == old:
        raise RegistryError(f"{factor_id} 已处于 {old}，无迁移")
    if new_status not in _TRANSITIONS[old]:
        raise RegistryError(f"非法迁移：{old} → {new_status}（允许 {_TRANSITIONS[old]}）")
    if old == "draft" and new_status == "active":
        if gate is None:
            raise RegistryError(
                f"{factor_id}: draft→active 须经激活门禁（ap fl activate {factor_id}），"
                "set_status 不设旁路")
        if not gate.get("ok"):
            raise RegistryError(
                f"{factor_id}: 激活门禁未过：{'；'.join(gate.get('reasons') or ['未知拒因'])}")
    state = _load_state(repo)
    entry = state.get(factor_id, {"status": old, "version": meta.version, "history": []})
    rec = {"from": old, "to": new_status, "who": who, "why": why,
           "at": datetime.now().isoformat(timespec="seconds")}
    entry["status"] = new_status
    entry.setdefault("history", []).append(rec)
    state[factor_id] = entry
    p = _state_path(repo)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
    return rec


def _write_state(repo: Path, state: dict) -> None:
    p = _state_path(repo)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")


# ── 健康维度（M5 / 计划 8.2）：巡检产物，两维分离，不进 FactorMeta ──
def set_health(repo: Path, factor_id: str, new_health: str, *, by: str = "inspect",
               why: str = "", as_of: str | None = None) -> dict:
    """健康态迁移（可恢复，与 lifecycle 分离）：返回迁移记录。

    - 缺省基线：状态文件无 health 键时隐式视为 healthy（存量 active 因子基线态）；
    - invalid 不允许直通 healthy（须假设复查后经 unknown → healthy 人工路径）；
    - 同态重申允许（如持续 weakening 的逐季记录），但 (from,to,as_of) 与末条相同则幂等跳过；
    - as_of：巡检日（缺省今天），写入 health_history 供追溯。
    """
    if new_health not in HEALTH:
        raise RegistryError(f"目标健康态非法：{new_health!r}（允许 {HEALTH}）")
    meta = load_factor(repo, factor_id)
    state = _load_state(repo)
    entry = state.get(factor_id, {"status": meta.status, "version": meta.version, "history": []})
    old = entry.get("health", "healthy")           # 缺省 = 存量基线态（无 health 键 = 隐式 healthy）
    day = as_of or date.today().isoformat()
    if new_health != old and new_health not in _HEALTH_TRANSITIONS[old]:
        hint = ("；invalid 须经假设复查后重新走激活/降级流程"
                if old == "invalid" else "")
        raise RegistryError(f"非法健康迁移：{old} → {new_health}（允许 "
                            f"{sorted(_HEALTH_TRANSITIONS[old])}）{hint}")
    hist = entry.setdefault("health_history", [])
    last = hist[-1] if hist else None
    if last and last["to"] == new_health and last["as_of"] == day:
        return dict(last)   # 幂等：同 as_of 重跑（首条 from 与隐式基线可能不同），不重复追加
    rec = {"from": old, "to": new_health, "by": by, "why": why, "as_of": day}
    hist.append(rec)
    entry["health"] = new_health
    state[factor_id] = entry
    _write_state(repo, state)
    return rec


def get_health(repo: Path, factor_id: str) -> tuple[str, list[dict]]:
    """读取当前健康态与 health_history（状态文件真源；无记录 = 隐式 healthy 基线）。

    供合成池/复查联动消费（T-L5）：池判定与 warnings 都从这里取，不查 fl_factor_health
    （表是历史，状态文件是当前真源，T-L4 巡检已保证二者同步）。
    """
    entry = _load_state(repo).get(factor_id) or {}
    return entry.get("health", "healthy"), entry.get("health_history") or []


# ── 版本语义（M5 / 计划 8.3）：口径变更 = 升版，旧版冻结 ──
def bump_version(repo: Path, factor_id: str, new_version: str, *, who: str = "system",
                 why: str = "") -> dict:
    """注册面版本升级（不触发物化，物化收敛见 materialize）：返回升级记录。

    - 三段式校验；new != 当前；不得与盘上历史版本（YAML 静态 + 状态文件现值 +
      history 中全部 version_bump 的 from/to）重复；
    - history 追加 type=version_bump 记录（与状态迁移记录以 type 键区分）。
    """
    if not _VERSION_RE.match(new_version or ""):
        raise RegistryError(f"版本号须为 MAJOR.MINOR.PATCH 三段式：{new_version!r}")
    meta = load_factor(repo, factor_id)
    state = _load_state(repo)
    entry = state.get(factor_id, {"status": meta.status, "version": meta.version, "history": []})
    old = entry.get("version", meta.version)
    if new_version == old:
        raise RegistryError(f"{factor_id} 已处于版本 {old}，无升级")
    seen = {old, meta.version}
    for h in entry.get("history", []):
        if h.get("type") == "version_bump":
            seen.add(h["from"])
            seen.add(h["to"])
    if new_version in seen:
        raise RegistryError(f"版本 {new_version} 与历史版本重复（盘上曾用：{sorted(seen)}）")
    rec = {"type": "version_bump", "from": old, "to": new_version, "who": who, "why": why,
           "at": datetime.now().isoformat(timespec="seconds")}
    entry["version"] = new_version
    entry.setdefault("history", []).append(rec)
    state[factor_id] = entry
    _write_state(repo, state)
    return rec


def bind_hypothesis(repo: Path, factor_id: str, hypothesis_id: str, *, who: str = "system",
                    why: str = "") -> dict:
    """注册面血缘绑定（T-F9 personal_clone 通道用）：状态文件写 hypothesis_id。

    - history 追加 type=lineage_bind 记录（与状态迁移/升版记录以 type 键区分）；
    - 幂等：重复绑定同一 id 直接返回；已绑其它 id → 拒绝（换绑须先解绑，无旁路）；
    - 不改 status/version（登记动作不触碰生命周期状态机）。
    """
    meta = load_factor(repo, factor_id)
    state = _load_state(repo)
    entry = state.get(factor_id, {"status": meta.status, "version": meta.version, "history": []})
    old = entry.get("hypothesis_id")
    if old == hypothesis_id:
        return {"bound": True, "factor_id": factor_id, "hypothesis_id": hypothesis_id,
                "idempotent": True}
    if old:
        raise RegistryError(f"{factor_id} 已绑定 {old}，换绑须先解绑（无旁路）")
    entry["hypothesis_id"] = hypothesis_id
    entry.setdefault("history", []).append(
        {"type": "lineage_bind", "hypothesis_id": hypothesis_id, "who": who, "why": why,
         "at": datetime.now().isoformat(timespec="seconds")})
    state[factor_id] = entry
    _write_state(repo, state)
    return {"bound": True, "factor_id": factor_id, "hypothesis_id": hypothesis_id}


# ── 依赖解析与 lookback（计划 M1.5）──────────────────
_DT_TABLES = {"fl_daily_bar": "dt", "fl_trade_calendar": "dt", "fl_dws_financial_ttm": "period",
              "fl_fin_report": "period", "fl_dim_security": None}


def resolve_dependencies(meta: FactorMeta, con) -> list[str]:
    """依赖解析：校验 meta.inputs 中声明的表在库中存在，返回缺失表。"""
    missing = []
    for t in meta.inputs:
        row = con.execute(
            "SELECT count(*) FROM information_schema.tables WHERE table_name = ?", [t]).fetchone()
        if not row or row[0] == 0:
            missing.append(t)
    return missing


def resolve_backtest_start(con, metas: list[FactorMeta]) -> date | None:
    """回测起点 = max(各因子 lookback_days 满足日)。

    对每个因子：取其依赖表在库内的最早 dt（period），加 lookback_days 天；
    全部因子里取最大者。无任何可解析输入 → None。
    """
    starts: list[date] = []
    for m in metas:
        cand: list[date] = []
        for t in m.inputs:
            col = _DT_TABLES.get(t)
            if col is None:
                continue
            row = con.execute(f"SELECT min({col}) FROM {t}").fetchone()
            if row and row[0]:
                d = row[0] if isinstance(row[0], date) else date.fromisoformat(str(row[0]))
                cand.append(date.fromordinal(d.toordinal() + m.lookback_days))
        if cand:
            starts.append(max(cand))
    return max(starts) if starts else None
