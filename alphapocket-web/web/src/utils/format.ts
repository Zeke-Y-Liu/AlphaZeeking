/** 数值/日期格式化工具（口径与 V4-α 报告一致：空值统一 `—`）。 */

/** 空值占位符（与 α 报告 _pct/_num 的 "—" 一致）。 */
export const DASH = '—';

function missing(v: unknown): boolean {
  return v === null || v === undefined || v === '' || (typeof v === 'number' && !Number.isFinite(v));
}

/** 百分数：v 为小数比率（如 0.1234 → "12.3%"）；空值 → "—"。 */
export function fmtPct(v: number | null | undefined, digits = 1): string {
  if (missing(v)) return DASH;
  return `${((v as number) * 100).toFixed(digits)}%`;
}

/** 人民币金额：千分位（如 1000000 → "¥1,000,000"）；空值 → "—"。 */
export function fmtCny(v: number | null | undefined, digits = 0): string {
  if (missing(v)) return DASH;
  const s = (v as number).toLocaleString('en-US', {
    minimumFractionDigits: digits,
    maximumFractionDigits: digits,
  });
  return `¥${s}`;
}

/** 数字：固定小数位（默认 2）；空值 → "—"。 */
export function fmtNum(v: number | null | undefined, digits = 2): string {
  if (missing(v)) return DASH;
  return (v as number).toFixed(digits);
}

/** 日期：YYYY-MM-DD；空值/非法 → "—"。 */
export function fmtDate(v: string | number | Date | null | undefined): string {
  if (missing(v)) return DASH;
  if (v instanceof Date) {
    return Number.isNaN(v.getTime()) ? DASH : ymd(v);
  }
  if (typeof v === 'number') {
    const d = new Date(v);
    return Number.isNaN(d.getTime()) ? DASH : ymd(d);
  }
  const m = String(v).match(/^(\d{4})-(\d{2})-(\d{2})/);
  if (m) return `${m[1]}-${m[2]}-${m[3]}`;
  const d = new Date(String(v));
  return Number.isNaN(d.getTime()) ? DASH : ymd(d);
}

function ymd(d: Date): string {
  const mm = String(d.getMonth() + 1).padStart(2, '0');
  const dd = String(d.getDate()).padStart(2, '0');
  return `${d.getFullYear()}-${mm}-${dd}`;
}
