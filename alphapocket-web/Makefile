# AlphaPocket V4-β · monorepo 顶层 Makefile（T0.1）
# 用法：make help
MAKEFLAGS += --no-print-directory
SHELL := /bin/bash

COMPOSE_FILE ?= deploy/docker-compose.dev.yml
COMPOSE      := docker compose -f $(COMPOSE_FILE)

CORE_VENV    ?= core/.venv
# 绝对路径：recipe 内会 cd 到 core/，相对路径会失效
CORE_RUFF    := $(abspath $(CORE_VENV)/bin/ruff)
CORE_PYTEST  := $(abspath $(CORE_VENV)/bin/pytest)

.PHONY: help up down logs psql migrate seed \
        lint lint-core lint-bff lint-web \
        test test-core test-bff test-web test-e2e \
        install-core ensure-compose

## help: 列出可用目标
help:
	@grep -E '^## ' $(MAKEFILE_LIST) | sed 's/^## /  /'

# ---------------- 基础设施（compose 由 T0.2 交付） ----------------
## ensure-compose: 校验 compose 文件存在（缺失时清晰报错）
ensure-compose:
	@test -f $(COMPOSE_FILE) || { \
		echo "❌ 缺少 $(COMPOSE_FILE)：该文件由 T0.2（docker-compose 与基础设施）交付，T0.1 未生成。"; \
		echo "   期望内容：pg16 / redis7 / minio / core / bff / web 六服务 + healthcheck。"; \
		exit 1; \
	}

## up: 启动开发全栈（需 T0.2 的 deploy/docker-compose.dev.yml 与 docker）
up: ensure-compose
	$(COMPOSE) up -d --build

## down: 停止并移除开发全栈
down: ensure-compose
	$(COMPOSE) down

## logs: 跟随查看全栈日志
logs: ensure-compose
	$(COMPOSE) logs -f --tail=100

## psql: 进入 pg 容器的 psql 会话
psql: ensure-compose
	$(COMPOSE) exec pg psql -U ap -d ap

## migrate: 执行 alembic 迁移（core 容器内；脚本由 T2.1 交付）
migrate: ensure-compose
	$(COMPOSE) exec -T core alembic upgrade head

## seed: 灌入演示数据（demo 用户 + CN4/MULTI5；由 T2.1 交付）
seed: ensure-compose
	$(COMPOSE) exec -T core python -m app.seed

# ---------------- lint ----------------
## lint: core(ruff) + bff(eslint) + web(eslint)
lint: lint-core lint-bff lint-web

lint-core:
	@if [ -x "$(CORE_RUFF)" ]; then R="$(CORE_RUFF)"; else R="ruff"; fi; \
	cd core && $$R check app tests

lint-bff:
	pnpm -C bff lint

lint-web:
	pnpm -C web lint

# ---------------- test ----------------
## test: core(pytest) + bff(jest) + web(vitest)
test: test-core test-bff test-web

# 说明：通过 PYTHONPATH 注入 V4-α 内核（不安装 alphapocket 本体）
test-core:
	@if [ -x "$(CORE_PYTEST)" ]; then P="$(CORE_PYTEST)"; else P="pytest"; fi; \
	cd core && PYTHONPATH=/workspace/alphapocket $$P -q

test-bff:
	pnpm -C bff test

test-web:
	pnpm -C web test -- --run

## test-e2e: Playwright 端到端（用例由 T7.1 交付；当前占位直接跳过）
test-e2e:
	@if [ ! -f e2e/package.json ]; then \
		echo "ℹ️  e2e/ 尚未安装 Playwright（T7.1 交付）；当前仅占位，跳过。"; \
	else \
		cd e2e && npx playwright test; \
	fi

# ---------------- 开发环境准备 ----------------
## install-core: 创建 core/.venv 并安装开发依赖（不安装 alphapocket 本体）
install-core:
	python3.12 -m venv $(CORE_VENV)
	$(CORE_VENV)/bin/pip install -U pip
	$(CORE_VENV)/bin/pip install fastapi "uvicorn[standard]" pydantic pydantic-settings pytest pytest-asyncio httpx ruff

# ---------------- 本地直连（本机无 docker 时使用；T2.1 新增） ----------------
# 上面的 migrate/seed/psql 走 compose（需 docker）；本机已直跑 PG(127.0.0.1:5432) 时用 *-local。
.PHONY: migrate-local seed-local psql-local

## migrate-local: 本地 alembic 迁移到 head（无 docker）
migrate-local:
	cd core && $(abspath $(CORE_VENV)/bin/alembic) upgrade head

## seed-local: 本地灌入演示数据（demo/CN4/MULTI5；无 docker，幂等）
seed-local:
	cd core && $(abspath $(CORE_VENV)/bin/python) -m app.seed

## psql-local: 本地连接 PG（无 docker）
psql-local:
	PGPASSWORD=$${PGPASSWORD:-ap} psql -h 127.0.0.1 -U ap -d ap

# ---------------- AP-Checkpoint（docs/CHECKPOINT-PLAN.md）----------------
.PHONY: ck-build ck-restore ck-push ck-pull ck-probe

## ck-build: 构建 L0/L1 checkpoint 镜像（T1.4/T3.1 实现）
ck-build:
	@echo "[ck] not implemented — 见 docs/CHECKPOINT-PLAN.md T1.4/T3.1"; exit 0

## ck-restore: 从 checkpoint 恢复 L0/L1（T2.1/T3.2 实现）
ck-restore:
	@echo "[ck] not implemented — 见 docs/CHECKPOINT-PLAN.md T2.1/T3.2"; exit 0

## ck-push: 推送 L0 到 GitHub Release / L1 到 ap-state 分支（T2.3/T3.1 实现）
ck-push:
	@echo "[ck] not implemented — 见 docs/CHECKPOINT-PLAN.md T2.3/T3.1"; exit 0

## ck-pull: 从 Release 拉取 L0 并恢复（T2.3 实现）
ck-pull:
	@echo "[ck] not implemented — 见 docs/CHECKPOINT-PLAN.md T2.3"; exit 0

## ck-probe: 重置语义哨兵（T0.3；make ck-probe mark / make ck-probe report）
ifeq ($(filter ck-probe,$(MAKECMDGOALS)),ck-probe)
CK_PROBE_SUBCMD := $(filter-out ck-probe,$(MAKECMDGOALS))
# 让子命令（mark/report）不被 make 当作缺失目标
.DEFAULT:
	@:
endif
ck-probe:
	@bash deploy/ck/ck-probe.sh $(CK_PROBE_SUBCMD)
