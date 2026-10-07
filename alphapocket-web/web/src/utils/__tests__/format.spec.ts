import { describe, expect, it } from 'vitest';

import { CCY_SYMBOL, DASH, fmtCny, fmtDate, fmtMoney, fmtNum, fmtPct } from '../format';

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

describe('fmtMoney（V4-β 多币种）', () => {
  it('CNY/HKD/USD 各自符号', () => {
    expect(fmtMoney(13.24, 'CNY')).toBe('¥13.24');
    expect(fmtMoney(428.2, 'HKD')).toBe('HK$428.20');
    expect(fmtMoney(333.41, 'USD')).toBe('$333.41');
  });

  it('缺省/未知币种回退人民币符号（兼容旧行为）', () => {
    expect(fmtMoney(1000, undefined)).toBe('¥1,000.00');
    expect(fmtMoney(1000, 'XXX')).toBe('¥1,000.00');
  });

  it('空值 → —；千分位', () => {
    expect(fmtMoney(null, 'HKD')).toBe(DASH);
    expect(fmtMoney(1234567.8, 'USD', 2)).toBe('$1,234,567.80');
  });

  it('CCY_SYMBOL 币种符号表', () => {
    expect(CCY_SYMBOL.HKD).toBe('HK$');
    expect(CCY_SYMBOL.USD).toBe('$');
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
