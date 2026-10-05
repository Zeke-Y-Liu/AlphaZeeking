-- AlphaPocket V4-β · PostgreSQL 初始化脚本（T0.2）
-- ---------------------------------------------------------------------------
-- 本目录挂载到容器 /docker-entrypoint-initdb.d/，仅在 pg_data 卷“首次初始化”
-- （空数据目录）时按文件名字典序执行一次；已有卷不会重复执行。
-- 后续新增初始化脚本请另建 NN-xxx.sql（例如 10-schema.sql），避免改动本文件。
-- ---------------------------------------------------------------------------

-- 标的模糊检索：支撑 GET /securities/search（名称/代码的前缀与相似度匹配）
CREATE EXTENSION IF NOT EXISTS pg_trgm;

-- UUID 生成：预留（若后续审计/实体改用 UUID 主键）
CREATE EXTENSION IF NOT EXISTS "uuid-ossp";
