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
