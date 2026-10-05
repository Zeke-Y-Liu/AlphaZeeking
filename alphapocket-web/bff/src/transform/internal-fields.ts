/**
 * 响应「内部字段」剔除清单（T5.3）。
 *
 * 依据 §5.1 PG Schema 与 §6.2 端点契约定义。仅对以下两个只读端点生效：
 * `GET /api/v1/portfolios`、`GET /api/v1/data/status`。
 *
 * 剔除语义：对 JSON 响应递归遍历对象/数组，删除下列键名。
 *
 * | 字段 | 依据 | 说明 |
 * | --- | --- | --- |
 * | id | §5.1 | ORM 自增主键（portfolios/positions/runs/verify_results 等的 BIGINT id），对外用 pid/run_uid 等业务键 |
 * | portfolio_id | §5.1 | positions/runs 内部的数值外键，不应外泄 |
 * | run_id | §5.1 | rebalance_plans 等内部的数值外键 |
 * | plan_id | §5.1 | exec_orders 内部的数值外键 |
 * | artifact_keys | §5.1 | MinIO 对象 key 列表，属存储内部实现，不下发前端 |
 * | strategy | §5.1/§5.2 | 预留策略参数 JSONB，当前为内部字段 |
 * | password_hash | §5.1 | 用户口令哈希（纵深防御，即便聚合误带也不外泄） |
 * | totp_secret | §5.1 | 二次验证密钥（纵深防御） |
 * | _sa_instance_state | §5.1 | SQLAlchemy ORM 运行态，非业务数据 |
 *
 * 说明：`job_id`（jobs.id 的 TEXT 业务键，供 /jobs/{id} 轮询使用）不在剔除之列。
 */
export const INTERNAL_FIELDS: readonly string[] = [
  'id',
  'portfolio_id',
  'run_id',
  'plan_id',
  'artifact_keys',
  'strategy',
  'password_hash',
  'totp_secret',
  '_sa_instance_state',
];

const INTERNAL_FIELD_SET = new Set(INTERNAL_FIELDS);

/** 递归剔除内部字段并返回新对象（不修改入参）。 */
export function stripInternalFields<T>(value: T): T {
  if (Array.isArray(value)) {
    return value.map((item) => stripInternalFields(item)) as unknown as T;
  }
  if (typeof value === 'object' && value !== null) {
    const source = value as Record<string, unknown>;
    const out: Record<string, unknown> = {};
    for (const [key, val] of Object.entries(source)) {
      if (INTERNAL_FIELD_SET.has(key)) continue;
      out[key] = stripInternalFields(val);
    }
    return out as unknown as T;
  }
  return value;
}
