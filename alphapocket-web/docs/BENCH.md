# 启动方式计时对比（AP-Checkpoint）

> 生成：bench.sh（FAST 为实测，SLOW 为 （2026-10-16/17 两次全量重建实测汇总））

| 阶段 | SLOW（全量安装） | FAST-LOCAL（checkpoint 恢复） |
|---|---|---|
| 系统依赖（apt / 二进制下载） | ~120s | 0s（ap-bin 随 L0 解压） |
| Python venv + pip 全家 | ~240s | ~5s（tar 解压，sha 校验） |
| node_modules ×3（pnpm） | ~60s | ~10s（tar 解压） |
| Playwright chromium | ~90s（按需） | 按需（--with-chromium 独立 asset） |
| 基础设施起服（PG/Redis/MinIO） | ~20s | ~3s（embedded，幂等跳过） |
| alembic + seed | ~30s | ~2s（pg_restore 状态快照） |
| 基线数据 | ~30s（git 内拷贝） | ~2s（同） |
| 数据同步 | ~240s（bootstrap 全量外网） | ~300s（incremental；纯增量窗口时 ~60s） |
| **三件套就绪时刻** | **~500s** | **28s（实测）** |
| **总耗时（含数据就绪）** | **~840s ≈ 14min** | **348s ≈ 5min（实测）** |

- FAST-LOCAL「三件套就绪」实测 28s；「数据增量完成」实测 339s（含港股 qfq 全序列外网拉取）。
- 冷启动（FAST-PULL）额外 +Release 下载 ~180-240s（253MB，不含 chromium）。
- 提速关键：L0 依赖镜像消除 apt/pip/pnpm（~7min）；L1 状态快照消除 bootstrap 外网全量（~4min）。

## 冷启动实录（2026-10-18 · FAST-PULL 全路径）

模拟沙箱重置：停服务 → 清空 gitignored 运行时（venv/node_modules×3/ap-bin/ap-data）+ L0 本地缓存（.ck）+ Playwright 浏览器 → 哨兵标记 T0=13:27:42 → 一键启动。

| 阶段 | 累计耗时 | 说明 |
|---|---|---|
| 路径决策 | 28s | 无本地缓存 → FAST-PULL；Release manifest + 256MB L0 下载 |
| L0 恢复 | 53s | venv/node/infra 解压 + sha256 校验 + 自检 |
| infra 就绪 | 58s | embedded PG16 initdb + redis + minio（零 apt）|
| L1 恢复 | 63s | ap-state pg_restore + alembic 对齐 + 行数校验 |
| 三件套就绪 | **69s** | core :8082 / bff :8081 / web :5173 |
| 数据增量完成 | **377s（6.3min）** | warm-repo 基线就位 + incremental；六项验证 6/6 |

对比：SLOW 全量安装路径 ~14min；FAST-PULL 全冷 6.3min（提速 ~55%）；FAST-LOCAL（缓存幸存）1.3min。
L0 Release 体积 256M（缓存实际 256M + ap-bin 169M 解压后）。
