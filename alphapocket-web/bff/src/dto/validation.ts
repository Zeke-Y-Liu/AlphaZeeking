/**
 * 最小自研 DTO 校验引擎（T5.3）。
 *
 * 背景：bff 未安装 class-validator / class-transformer（pnpm 依赖清单中没有），
 * 为避免引入新依赖，这里实现一个仅覆盖本任务所需能力的白名单校验器：
 * - 白名单：只保留 schema 声明的字段，其余字段一律剥离（防 core schema 漂移）；
 * - 类型非法 / 枚举越界 → 产出字段级错误（field + message）。
 */

export interface ValidationIssue {
  /** 字段路径，嵌套/数组项形如 `positions[0].secid`。 */
  field: string;
  message: string;
}

export type Rule =
  | { kind: 'string'; required?: boolean; nonEmpty?: boolean }
  | { kind: 'number'; required?: boolean }
  | { kind: 'boolean'; required?: boolean }
  | { kind: 'enum'; values: readonly string[]; required?: boolean }
  | { kind: 'array'; required?: boolean; item: ObjectSchema };

export type ObjectSchema = Record<string, Rule>;

export interface ValidationResult {
  /** 剥离多余字段后的干净对象。 */
  value: Record<string, unknown>;
  errors: ValidationIssue[];
}

function isPlainObject(value: unknown): value is Record<string, unknown> {
  return typeof value === 'object' && value !== null && !Array.isArray(value);
}

function fieldName(path: string): string {
  const segments = path.split('.');
  const tail = segments[segments.length - 1] ?? path;
  return tail.replace(/\[\d+\]$/, '');
}

function validateValue(
  raw: unknown,
  rule: Rule,
  path: string,
  errors: ValidationIssue[],
): unknown {
  const name = fieldName(path);
  switch (rule.kind) {
    case 'string': {
      if (typeof raw !== 'string') {
        errors.push({ field: path, message: `${name} 必须为字符串` });
        return undefined;
      }
      if (rule.nonEmpty && raw.trim() === '') {
        errors.push({ field: path, message: `${name} 不能为空` });
        return undefined;
      }
      return raw;
    }
    case 'number': {
      if (typeof raw !== 'number' || !Number.isFinite(raw)) {
        errors.push({ field: path, message: `${name} 必须为数字` });
        return undefined;
      }
      return raw;
    }
    case 'boolean': {
      if (typeof raw !== 'boolean') {
        errors.push({ field: path, message: `${name} 必须为布尔值` });
        return undefined;
      }
      return raw;
    }
    case 'enum': {
      if (typeof raw !== 'string' || !rule.values.includes(raw)) {
        errors.push({
          field: path,
          message: `${name} 取值非法，允许：${rule.values.join(' | ')}`,
        });
        return undefined;
      }
      return raw;
    }
    case 'array': {
      if (!Array.isArray(raw)) {
        errors.push({ field: path, message: `${name} 必须为数组` });
        return undefined;
      }
      const items: unknown[] = [];
      raw.forEach((item, index) => {
        const itemPath = `${path}[${index}]`;
        if (!isPlainObject(item)) {
          errors.push({ field: itemPath, message: '数组项必须为对象' });
          return;
        }
        const nested = validateObject(item, rule.item, itemPath);
        errors.push(...nested.errors);
        items.push(nested.value);
      });
      return items;
    }
  }
}

/**
 * 按 schema 校验并裁剪一个对象。
 * @param input 待校验原始值（非对象按空对象处理，便于统一产出「必填缺失」）。
 * @param schema 白名单字段定义。
 * @param prefix 字段路径前缀（递归内部使用）。
 */
export function validateObject(
  input: unknown,
  schema: ObjectSchema,
  prefix = '',
): ValidationResult {
  const errors: ValidationIssue[] = [];
  const value: Record<string, unknown> = {};
  const source = isPlainObject(input) ? input : {};

  for (const [field, rule] of Object.entries(schema)) {
    const path = prefix ? `${prefix}.${field}` : field;
    const raw = source[field];
    if (raw === undefined || raw === null) {
      if (rule.required) errors.push({ field: path, message: `${field} 为必填字段` });
      continue;
    }
    const validated = validateValue(raw, rule, path, errors);
    if (validated !== undefined) value[field] = validated;
  }

  return { value, errors };
}
