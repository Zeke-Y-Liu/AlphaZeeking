import { AxiosError } from 'axios';
import type { AxiosResponse } from 'axios';
import { describe, expect, it } from 'vitest';

import { newRequestId, toApiError } from '../client';

function axiosErrorWithResponse(status: number, data: unknown): AxiosError {
  const response = { status, data, statusText: '', headers: {}, config: {} } as unknown as AxiosResponse;
  return new AxiosError('request failed', 'ERR_BAD_RESPONSE', undefined, undefined, response);
}

describe('toApiError 错误解包', () => {
  it('解包后端 {code,message,detail}', () => {
    const err = toApiError(
      axiosErrorWithResponse(409, { code: 'JOB_LOCKED', message: '任务冲突', detail: { job: 'x' } }),
    );
    expect(err.code).toBe('JOB_LOCKED');
    expect(err.message).toBe('任务冲突');
    expect(err.status).toBe(409);
    expect(err.detail).toEqual({ job: 'x' });
    expect(err).toBeInstanceOf(Error);
  });

  it('502 无 code 时回退 BAD_GATEWAY', () => {
    const err = toApiError(axiosErrorWithResponse(502, {}));
    expect(err.code).toBe('BAD_GATEWAY');
    expect(err.status).toBe(502);
  });

  it('422 回退 UNPROCESSABLE_ENTITY', () => {
    const err = toApiError(axiosErrorWithResponse(422, { message: '权重未归一' }));
    expect(err.code).toBe('UNPROCESSABLE_ENTITY');
    expect(err.message).toBe('权重未归一');
  });

  it('网络错误（无响应）→ NETWORK_ERROR / status 0', () => {
    const err = toApiError(new AxiosError('Network Error', 'ERR_NETWORK'));
    expect(err.code).toBe('NETWORK_ERROR');
    expect(err.status).toBe(0);
  });

  it('超时 → TIMEOUT', () => {
    const err = toApiError(new AxiosError('timeout', 'ECONNABORTED'));
    expect(err.code).toBe('TIMEOUT');
  });
});

describe('newRequestId', () => {
  it('返回非空字符串且每次不同', () => {
    const a = newRequestId();
    const b = newRequestId();
    expect(a).toBeTruthy();
    expect(a).not.toBe(b);
  });
});
