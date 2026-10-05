import { describe, expect, it } from 'vitest';

import { DASH, fmtCny, fmtDate, fmtNum, fmtPct } from '../format';

describe('fmtPct', () => {
  it('小数比率 → 百分数（默认 1 位）', () => {
    expect(fmtPct(0.1234)).toBe('12.3%');
    expect(fmtPct(0.1234, 2)).toBe('12.34%');
    expect(fmtPct(-0.05)).toBe('-5.0%');
    expect(fmtPct(0)).toBe('0.0%');
  });

  it('null / undefined / NaN → —', () => {
    expect(fmtPct(null)).toBe(DASH);
    expect(fmtPct(undefined)).toBe(DASH);
    expect(fmtPct(Number.NaN)).toBe(DASH);
  });
});

describe('fmtCny', () => {
  it('千分位（默认 0 位小数）', () => {
    expect(fmtCny(1000000)).toBe('¥1,000,000');
    expect(fmtCny(18000)).toBe('¥18,000');
  });

  it('空值 → —', () => {
    expect(fmtCny(null)).toBe(DASH);
    expect(fmtCny(undefined)).toBe(DASH);
  });
});

describe('fmtNum', () => {
  it('固定小数位（默认 2）', () => {
    expect(fmtNum(1.2345)).toBe('1.23');
    expect(fmtNum(3, 0)).toBe('3');
  });

  it('空值 → —', () => {
    expect(fmtNum(null)).toBe(DASH);
    expect(fmtNum(undefined)).toBe(DASH);
  });
});

describe('fmtDate', () => {
  it('ISO / 日期字符串 → YYYY-MM-DD', () => {
    expect(fmtDate('2026-10-05')).toBe('2026-10-05');
    expect(fmtDate('2026-10-05T12:34:56Z')).toBe('2026-10-05');
  });

  it('Date 对象 → YYYY-MM-DD', () => {
    expect(fmtDate(new Date(2026, 9, 5))).toBe('2026-10-05');
  });

  it('空值 / 非法 → —', () => {
    expect(fmtDate(null)).toBe(DASH);
    expect(fmtDate(undefined)).toBe(DASH);
    expect(fmtDate('not-a-date')).toBe(DASH);
  });
});
