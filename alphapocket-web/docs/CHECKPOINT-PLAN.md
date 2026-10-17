# AP-Checkpoint：无 Docker 沙箱的「类镜像」快速启动方案

> 状态：设计稿（待评审后按任务卡执行）
> 作者依据：2026-10-16 两次沙箱重置实测（见 §1）
> 目标：把沙箱重置后的全栈恢复从 **~15 分钟**降到 **暖启动 ~2 分钟 / 冷启动 ~6-8 分钟**

---

## 1. 问题与实测证据

### 1.1 重置语义（两次实测确认）

| 内容 | 位置 | 重置后 |
|---|---|---|
| `.git` 仓库与所有**跟踪**文件（含 `remote/repo` 25M 基线数据） | /workspace | ✅ 存活 |
| `.gitignore` 的内容：`.venv`、`node_modules`、`var/ws`（**含 DuckDB 物化与 PG 无关的运行数据**） | /workspace | ❌ 全灭 |
| 系统盘：apt 组件（PG/Redis）、`/usr/local/bin/minio`、pyenv（`/root/.pyenv`）、Playwright chromium（`/root/.cache`） | 系统盘 | ❌ 全灭 |
| 系统件：bash/tar/gzip/curl | /usr/bin | ✅ 存活；**zstd 无** |

**结论**：checkpoint 不能放在 `/workspace` 的 gitignored 目录里等下次用——**唯一可靠的传输通道是 git 本身（跟踪文件 / 数据分支 / Release asset）**。

### 1.2 全量重建成本清单（slow path，实测口径）

| 步骤 | 实测耗时 |
|---|---|
| apt 安装 PG16 + Redis + curl | ~2-3 min |
| pyenv python3.12 + venv + pip 全家（akshare 链最重） | ~4-5 min |
| pnpm install ×3（bff/web/e2e） | ~0.5-1 min |
| MinIO 二进制下载 + 启动 | ~1 min |
| alembic 迁移 + seed | ~10 s |
| bootstrap 全量数据同步（15 标的外网拉取）+ DuckDB 物化 + 因子 + 六项验证 | ~3-4 min |
| （可选）Playwright chromium 下载 | ~1.5 min |
| **合计** | **~12-16 min** |

---

## 2. 方案总览：三层「镜像」+ git 传输

类 Docker 心智：`manifest = image config`，`L0 = 依赖基础层`，`L1 = 状态层`，`deps_key = image digest`，`restore = run`。

```
┌────────────────────────────────────────────────────────────┐
│ L0 依赖镜像（deps_key 绑定，依赖清单不变则永不过期）        │
│  - pyenv python 3.12.3      ~35 MB  (gz)                   │
│  - core/.venv              ~150 MB  (gz, 剔 __pycache__)   │
│  - node_modules ×3         ~100 MB  (gz)                   │
│  - chromium（按需拉）      ~85 MB  (gz)                    │
│  - infra 工具包            ~45 MB  (gz: redis/minio/curl/  │
│    zstd/busybox/PG16-embedded)                             │
│  传输：GitHub Release asset（tag ck-l0-<deps_key>，        │
│        单 asset ≤2GB；chromium 独立 asset 按需拉）          │
├────────────────────────────────────────────────────────────┤
│ L1 状态快照（state_key = git HEAD + 数据 as_of）            │
│  - PG dump（schema+seed+业务数据，-Fc 格式）  ~2 MB        │
│  - MinIO artifacts（评估/回测产物）           ~5 MB        │
│  传输：git 孤儿数据分支 ap-state（自动 commit+push）         │
│  注意：remote/repo 基线数据已在主分支 git 内，无需传输       │
├────────────────────────────────────────────────────────────┤
│ L2 配置（随主分支 git）                                     │
│  - .env（由 .env.example + 脚本生成）                       │
│  - 脚本本体 deploy/ck/*（本方案交付物，进库）                │
└────────────────────────────────────────────────────────────┘
```

### 2.1 键与校验

```
deps_key  = sha256(pyproject.toml ‖ bff/pnpm-lock.yaml ‖
                   web/pnpm-lock.yaml ‖ e2e/package.json ‖ python -V)[:12]
state_key = <git HEAD short> + <as_of>
```

- L0 manifest（`manifest-L0.json`）：deps_key、各 tar 的 sha256、体积、构建时间、git HEAD、python 版本
- 恢复前逐文件 sha256 校验；**校验失败只作废该层并降级 slow 重建，绝不半恢复**
- 代码迁移新于 dump 时：`alembic current` ≠ head → 补跑 `alembic upgrade head`（幂等）

### 2.2 恢复决策树（新 start-local-sandbox.sh 主干）

```
启动
 ├─ 系统件探测（bash/tar/gzip 必在；zstd 缺则解压用 checkpoint 自带 zstd）
 ├─ 缓存探测 /workspace/.ck/cache/L0 命中且 deps_key 匹配？
 │   ├─ 命中 ──────────────► FAST-LOCAL：跳过下载（暖启动场景）
 │   └─ miss ──► gh release pull ck-l0-<deps_key>（含 sha 校验）
 │                 └─ 失败/无 tag ──► SLOW：原全量安装路径
 ├─ restore L0（解压回原路径：pyenv /root/.pyenv/versions、
 │             venv core/.venv、node ×3、infra → /workspace/ap-bin）
 ├─ start-infra（全部用 /workspace/ap-bin 内二进制，零 apt）：
 │     PG16-embedded initdb(/workspace/ap-data/pg) → 建role/库/扩展
 │     redis / minio :9100
 ├─ restore L1：fetch ap-state → pg_restore → minio artifacts
 │     └─ 无 ap-state ──► alembic upgrade head + seed（幂等）
 ├─ copy remote/repo(主分支内) → var/ws/default/remote/repo
 ├─ 起三件套 → POST /data/sync {"mode":"incremental"}（非 bootstrap）
 │     └─ incremental 失败/超时 ──► 降级 bootstrap
 ├─ 健康门禁：healthz ×3 + data/status + 六项验证 + e2e 冒烟
 └─ 尾钩（首启成功后自动）：build L0/L1 → push（下次即快）
```

### 2.3 传输矩阵

| 层 | 内容 | 通道 | 理由 |
|---|---|---|---|
| L0 大件 | venv/node/chromium/pyenv/PG-embedded | GitHub Release asset | 超 100MB 单文件 git 硬限；Release 上限 2GB；gh CLI 已验证可用 |
| L0 小件 | redis/minio/curl/zstd/busybox static | **主分支 git**（`deploy/ck/bin/`，合计 <15MB） | 无网络即可恢复基础设施，是最小依赖闭环 |
| L1 | PG dump + MinIO artifacts | git 孤儿分支 `ap-state` | 体量小；随代码仓库走；git diff 可审计 |
| 基线数据 | remote/repo 25M | 主分支 git（现状已如此） | 重置存活实证 |

### 2.4 预期收益（诚实口径）

| 场景 | 当前 | 本方案 | 备注 |
|---|---|---|---|
| 暖启动（仅进程重启/会话切换，/workspace 缓存在） | ~12 min（重装依赖） | **~2 min** | 解压+infra+incremental |
| 冷启动（全重置） | ~12-16 min | **~6-8 min** | release 下载 ~3-4 min 为主项；可配 ghproxy 镜像再压缩 |
| 依赖未变的日常恢复 | 同上 | 数据增量 ~1 min | incremental 仅补新交易日 |

---

## 3. 风险矩阵

| 风险 | 缓解 |
|---|---|
| venv 内 shebang 绝对路径失效 | 固定恢复到原路径 `core/.venv`；pyenv 恢复到 `/root/.pyenv/versions/3.12.3`；路径表在 manifest |
| deps_key 变更（依赖升级） | 只触发 L0 重建；L1 不受影响（数据独立于依赖） |
| Release 不可达/带宽差 | FAST-LOCAL 缓存 + SLOW 全量兜底双降级；chromium 独立 asset 按需不拉 |
| dump 与代码迁移漂移 | `alembic current` 校验 + 幂等 `upgrade head`；seed 幂等 |
| 半恢复状态污染 | 逐层 sha256 前置校验；PG 恢复前把旧数据目录改名 `.bak` |
| git 仓库体积膨胀 | L1 走孤儿分支定期重建（`git commit-tree` 每次新建根，不叠历史）；bin/ 小件一次性 |

---

## 4. Flash 可执行任务拆解（15 卡）

> 执行约定：每卡独立提交，格式 `feat(ck): T编号 — 标题`；只做卡内事，禁改卡外文件；
> 所有脚本 `bash -n` 过 + 验收断言逐条跑给结果才算完成。

### P0 地基

**T0.1 脚本基座 `deploy/ck/ck-env.sh`**
- 新建文件，定义：`CK_DIR=/workspace/.ck`、`CACHE=$CK_DIR/cache`、`AP_BIN=/workspace/ap-bin`、
  `deps_key()`（按 §2.1 公式，用 sha256sum 拼接）、`ck_hash()`（sha256 文件）、
  `ck_pack()`/`ck_unpack()`（探测 zstd，缺则 gzip；统一 `.tar.zst|.tar.gz` 后缀函数）、`say()` 日志。
- 验收：`bash -n` 通过；`source deploy/ck/ck-env.sh && echo $(deps_key)` 输出 12 位；
  `ck_pack/ck_unpack` 对临时目录 round-trip 内容一致。

**T0.2 gitignore + Makefile 登记**
- `.gitignore` 增 `/.ck/`、`/ap-bin/`、`/ap-data/`；Makefile 增目标占位：
  `ck-build`/`ck-restore`/`ck-push`/`ck-pull`（本卡先 echo 提示 not implemented，后续卡逐个替换实现）。
- 验收：`make help` 可见 4 目标；`git status` 干净；`make ck-build` 打印占位提示退出 0。

**T0.3 重置语义哨兵 `deploy/ck/ck-probe.sh`**
- 在 `/workspace/.ck-probe-marker`（**不加 ignore，让它进 git** 记录对照基线）与
  `/workspace/.ck/probe-untracked.txt`（gitignored 对照）各写 `date +%s + $RANDOM` + md5；
  `ck-probe.sh report` 对比两者与 git HEAD，输出「本次重置清了什么」三行报告。
- 验收：`ck-probe.sh mark` 后两文件存在；人为删 untracked 后 `report` 能正确报告其丢失。

### P1 L0 构建

**T1.1 venv 层 `build-l0-venv.sh`**
- `source ck-env.sh`；`tar` 打 `/root/.pyenv/versions/3.12.3`（若存在）→ `py-3.12.3.tar.zst`；
  `core/.venv`（`--exclude='__pycache__'`）→ `core-venv.tar.zst`；写 sha256 到 `.sha256` 伴文件。
- 验收：两 tar 生成；`tar -tf` 含 `bin/python`；`.sha256` 与 `sha256sum -c` 通过。

**T1.2 node 层 `build-l0-node.sh`**
- 打 `bff/node_modules`、`web/node_modules`、`e2e/node_modules` 合一 `node-all.tar.zst`
  （用 `--transform` 或分目录打保留路径），打 `~/.cache/ms-playwright/chromium*` → `chromium.tar.zst`（独立 asset，按需拉）。
- 验收：tar -tf 首行可见 `alphapocket-web/bff/node_modules`；chromium tar 含 `chrome-linux`。

**T1.3 infra 层 `build-l0-infra.sh`**
- 幂等下载到 `deploy/ck/bin/`（存在且 `--version` 可执行则跳过）：
  redis-static、minio、curl-static、zstd-static、busybox；
  下载 PG16 EDB 免安装二进制包（linux-x64 binaries zip）解包重打 `pg-embedded.tar.zst`（bin/lib/share 全量）。
- 验收：`bin/redis-server --version`、`bin/minio --version`、`bin/zstd -V`、
  `bin/postgres --version` 全部输出；`pg-embedded.tar.zst` sha256 落盘。

**T1.4 L0 汇总 `build-l0.sh` + manifest**
- 串 T1.1-1.3 → 产 `manifest-L0.json`（jq 手写或 python -c 生成）：deps_key、git HEAD、
  python 版本、`{file,sha256,bytes}` 数组；合并为 release 卷 `ck-l0-<deps_key>/`（不强制单 tar，多 asset）。
- 验收：`jq .deps_key manifest-L0.json` == `deps_key`；逐条 `sha256sum -c` 全过。

### P2 L0 恢复

**T2.1 `restore-l0.sh`**
- 读 manifest → 逐文件校验 sha256 → 按路径表解压：
  pyenv→`/root/.pyenv/versions/3.12.3`；venv→`core/.venv`；node→各自目录；
  infra→`/workspace/ap-bin`；chromium 仅当 `$CK_WITH_CHROMIUM=1`。
- 失败即退出码 2 并打印「降级：跑 build-l0 或原 slow path」，**不做部分恢复**。
- 验收：删 `core/.venv` 后跑恢复，`venv/bin/python -c "import fastapi,pandas,duckdb,akshare"` 通过；
  `ap-bin/redis-server --version`、`pnpm -C web exec vite --version` 通过。

**T2.2 `start-infra.sh`（零 apt 基础设施）**
- `ap-bin` 内 PG：`initdb -D /workspace/ap-data/pg`（已存在则跳过）→ `pg_ctl start` →
  建 role `ap`（SUPERUSER LOGIN）→ 建库 `ap` → `CREATE EXTENSION pg_trgm,"uuid-ossp"`；
  redis：`ap-bin/redis-server --daemonize yes --port 6379 --dir /workspace/ap-data`；
  minio：`ap-bin/minio server /workspace/ap-data/mio --address 127.0.0.1:9100`（后台）；
  已运行端口则幂等跳过。
- 验收：`pg_isready`、`redis-cli ping`=PONG、minio health 200、`psql -h 127.0.0.1 -U ap -d ap -tAc "select 1"`。

**T2.3 `push-l0.sh` / `pull-l0.sh`**
- push：`gh release create ck-l0-<deps_key> <assets...> --notes "L0 deps image"`（tag 已存在则 `gh release upload --clobber`）。
- pull：`gh release download ck-l0-<deps_key> -D $CACHE` → 校验 → `restore-l0.sh`。
- 验收：`gh release view ck-l0-<deps_key>` 列出全部 asset；pull 后 sha 与 manifest 一致。

### P3 L1 状态层

**T3.1 `build-l1.sh`（PG dump + artifacts → ap-state 分支）**
- `pg_dump -Fc -h 127.0.0.1 -U ap ap > $CK_DIR/ap.dump`；tar minio 数据目录 `minio-artifacts.tar.zst`；
  孤儿分支提交：`git checkout --orphan ap-state` 思路或 `git mktree`——推荐零检出方案：
  `blob=$(git hash-object -w ap.dump)`、`tree=$(git mktree <<< ...)`、
  `git commit-tree $tree -m "state as_of=<data/status as_of> head=<HEAD>"` → `git push origin <sha>:refs/heads/ap-state`（不切工作区）。
- 验收：`git ls-remote origin ap-state` 有新 sha；`git cat-file -s <blob>` 与 dump 大小一致；`pg_restore --list` 首行正常。

**T3.2 `restore-l1.sh`**
- `git fetch origin ap-state` → `git show origin/ap-state:ap.dump > $CK/ap.dump` →
  start-infra 已起的 PG：建 role/库（若空）→ `pg_restore --clean --if-exists -d ap`；
  artifacts 解回 `/workspace/ap-data/mio`；随后 `alembic current`≠head 则 `upgrade head`；seed 幂等兜底。
- 验收：`psql count users=1, securities=15, portfolios=3`；`alembic current`=head。

**T3.3 `warm-repo.sh`（基线数据 + 增量补齐）**
- `mkdir -p var/ws/default/remote && cp -r alphapocket/remote/repo var/ws/default/remote/repo`；
  起三件套 → 登录拿 token → `POST /api/v1/data/sync {"mode":"incremental"}` → 轮询 job done（超时 5min 降级 bootstrap）。
- 验收：job done；`GET /api/v1/data/status` as_of=最近交易日、15/15 has_bars；六项验证全 pass。

### P4 编排与验收

**T4.1 重写 `deploy/start-local-sandbox.sh`**
- 按 §2.2 决策树实现：FAST-LOCAL / FAST-PULL / SLOW 三路 + 结束计时与路径徽标输出；
  SLOW 尾钩自动 `build-l0.sh && push`、成功启动后自动 `build-l1.sh`；保留旧逻辑为函数 `slow_path()`。
- 验收：连续两轮执行，第二轮日志含 `FAST-LOCAL` 徽标且总耗时 <3min；`--slow` 参数可强制走旧路径。

**T4.2 `bench.sh`**
- 两模式计时（`time` 累计各阶段），输出 Markdown 表 `| 阶段 | slow | fast |`；结果追加 `docs/BENCH.md`（首次创建）。
- 验收：产出含两列完整数据的表。

**T4.3 全链路验收卡**
- fast path 起栈后依次：`core pytest tests/test_sync.py -q`（≥9 passed）、`python /tmp/e2e_real.py`（17/17）、
  `AP_E2E_CDP=launch pnpm -C e2e exec playwright test`（7/7）；任一失败即修即重跑。
- 验收：三段结果原文贴回。

**T4.4 RUNBOOK §13 文档**
- 增补「AP-Checkpoint 快速启动」：用法、三路径决策、降级矩阵、deps_key 变更处理、
  ap-state 分支维护（孤儿重建）、FAQ（venv shebang/端口 9100/重置语义）。
- 验收：`grep -c "AP-Checkpoint" docs/RUNBOOK.md` ≥1。

---

## 5. 依赖顺序与建议执行批次

```
T0.1 ─┬─ T0.2 ─ T0.3
      └─ T1.1 ─ T1.2 ─ T1.3 ─ T1.4 ─ T2.1 ─ T2.2 ─ T2.3
T2.2 ─ T3.1 ─ T3.2 ─ T3.3 ─ T4.1 ─ T4.2 ─ T4.3 ─ T4.4
```

- 批次①：T0.1-T0.3（半天内可全部完成，纯脚本无风险）
- 批次②：T1.x → T2.x（当前系统恰好处于重置后的「干净基线」，正是打 L0 镜像的最佳时机）
- 批次③：T3.x → T4.x
- **时机提示**：当前三件套已停（重置），执行 T1.x 前需先按旧流程重建一次运行环境，
  随后立即 build L0/L1 并 push——此后本沙箱的每次重置都能吃到本方案收益。
