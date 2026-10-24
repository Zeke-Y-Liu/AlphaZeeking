/**
 * E2E 冒烟（RG-3 · 交互流程完整性 · Chrome 兼容基线）。
 *
 * 设计原则：只依赖 seed 基线（cn4 组合 / 601058.SH），**自建档自清理**——
 * 单公司分析用例通过 API 建临时档案 ca_e2e，结束后删除，沙箱重置后依然可跑。
 *
 * 五校验点（只读 UI，不触发计算任务）：
 *  1 登录闭环；2 数据状态页（universe + 验证徽章）；
 *  3 组合详情（cn4 持仓渲染）；4 单公司分析详情（ca_e2e 四 tab）；
 *  5 回测页可达（shadow_ca_e2e）；全程 console 无红色 error。
 */
import { test, expect, type Page, type APIRequestContext } from '@playwright/test';

const USER = 'demo';
const PASS = 'alphapocket';
/** 每轮唯一 aid：软删（archived）后同 aid 不可重建 → 用时间戳避开（清理后不进默认列表）。 */
const AID = `ca_e2e_${Date.now().toString(36)}`;
/** 数据准备直连 core（E2E 校验的是前端交互；API 准备不经 bff 路径约定干扰）。 */
const CORE = 'http://127.0.0.1:8082';

declare module '@playwright/test' {
  interface Page {
    _consoleErrors?: string[];
  }
}

async function coreToken(api: APIRequestContext): Promise<string> {
  const r = await api.post(`${CORE}/api/v1/auth/login`, {
    data: { username: USER, password: PASS },
  });
  return (await r.json()).access;
}

test.describe.serial('回归冒烟（Chrome 基线）', () => {
  test.beforeAll(async ({ playwright }) => {
    const api = await playwright.request.newContext();
    const H = { Authorization: `Bearer ${await coreToken(api)}` };
    // 幂等：先清后建（前次 E2E 残留）
    await api.delete(`${CORE}/api/v1/analysis/${AID}`, { headers: H }).catch(() => {});
    const r = await api.post(`${CORE}/api/v1/analysis`, {
      headers: H, data: { secid: '601058.SH', aid: AID, name: 'E2E 冒烟档案' },
    });
    expect(r.status(), `建档失败: ${await r.text()}`).toBe(201);

    // 详情页 tabs 在「有报告」时才渲染（v-else）→ 跑一次轻量分析 job（本地计算、
    // 读既有数据面，数秒完成、不触外网），四模块 tab 才可被冒烟覆盖。
    const run = await api.post(`${CORE}/api/v1/analysis/${AID}/run`, { headers: H });
    expect(run.status(), `触发分析失败: ${await run.text()}`).toBe(202);
    const jobId = (await run.json()).job_id;
    for (let i = 0; i < 60; i++) {
      const job = (await (await api.get(`${CORE}/api/v1/jobs/${jobId}`, { headers: H })).json());
      if (job.status === 'done') break;
      if (job.status === 'failed') throw new Error(`分析 job 失败: ${job.error ?? ''}`);
      await new Promise((res) => setTimeout(res, 2000));
    }
    await api.dispose();
  });

  test.afterAll(async ({ playwright }) => {
    const api = await playwright.request.newContext();
    await api.delete(`${CORE}/api/v1/analysis/${AID}`, {
      headers: { Authorization: `Bearer ${await coreToken(api)}` },
    }).catch(() => {});
    await api.dispose();
  });

  test.beforeEach(async ({ page }) => {
    const errors: string[] = [];
    page.on('console', (msg) => {
      if (msg.type() === 'error') errors.push(msg.text());
    });
    await page.goto('/');
    await page.getByLabel('用户名').fill(USER);
    await page.getByLabel('密码').fill(PASS);
    await page.getByRole('button', { name: '登录' }).click();
    // 时序铁证：/login?redirect=/portfolios 也匹配 /portfolios/ 正则——必须等
    // token 真正落地 localStorage 再放行（否则硬导航被守卫弹回）。
    await page.waitForFunction(() => !!localStorage.getItem('ap.access'));
    await expect(page).toHaveURL(/\/portfolios\/?$/);
    page._consoleErrors = errors;
  });

  test('数据状态页：universe 与验证区渲染', async ({ page }) => {
    await page.goto('/data');
    // 套件内顺序执行时后端可能冷启 → 断言超时放宽（单跑默认 5s 易 flaky）
    await expect(page.getByText(/数据截至/)).toBeVisible({ timeout: 20_000 });
    await expect(page.getByText(/universe/)).toBeVisible();
    // 验证徽章只做结构断言（六项验证墙存在），不锁状态值：E2E beforeAll 建档
    // 会触发 M11 自动增量同步，同步进行中徽章短暂 unknown 属预期——验证值语义
    // 由 suite 的 data.r1（六项直读）把关，浏览器层不重复锁值。
    await expect(page.getByText('pit_audit').first()).toBeVisible({ timeout: 15_000 });
    expect(page._consoleErrors, page._consoleErrors?.join('\n')).toEqual([]);
  });

  test('组合详情：cn4 可达 + 手动拉取入口在位', async ({ page }) => {
    await page.goto('/portfolios/cn4');
    // 持仓表数据依赖 review run 产物（计算任务不属冒烟范围）→ 断言页面骨架
    // 与 M11 手动拉取按钮等关键交互组件；组合名来自 seed（cn4 · 赛轮/永新/福耀/分众）。
    await expect(page.getByText('赛轮/永新/福耀/分众').first()).toBeVisible();
    await expect(page.getByRole('button', { name: '拉取持仓数据' })).toBeVisible();
    expect(page._consoleErrors, page._consoleErrors?.join('\n')).toEqual([]);
  });

  test('单公司分析详情：四模块 tab 切换', async ({ page }) => {
    await page.goto(`/companies/${AID}`);
    await expect(page.getByText('601058.SH').first()).toBeVisible();
    for (const label of ['基本面', '行业趋势', '短线超卖', '最新消息']) {
      await page.getByRole('tab', { name: label }).click();
      await expect(page.getByRole('tab', { name: label })).toHaveAttribute(
        'aria-selected', /true|selected/);
    }
    expect(page._consoleErrors, page._consoleErrors?.join('\n')).toEqual([]);
  });

  test('回测页可达（影子组合）', async ({ page }) => {
    await page.goto(`/portfolios/shadow_${AID}/backtest`);
    await expect(page.getByText('回测').first()).toBeVisible();
    expect(page._consoleErrors, page._consoleErrors?.join('\n')).toEqual([]);
  });
});
