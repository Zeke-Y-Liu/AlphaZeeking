<script setup lang="ts">
// 单公司分析详情（M10）：四模块 tab（基本面/行业趋势/短线超卖/最新消息）
// + 运行分析（job 轮询）+ 新闻刷新 + 影子组合回测入口 + 模块开关。
import { computed, onMounted, ref } from 'vue';
import { useRoute } from 'vue-router';

import {
  fetchRunArtifact,
  getAnalysis,
  getAnalysisNews,
  getJob,
  listRuns,
  refreshAnalysisNews,
  runAnalysis,
  syncSecid,
  updateAnalysis,
} from '../api';
import type { AnalysisNews, AnalysisProfile } from '../api/types';
import { ApBadge, ApButton, ApCard, ApTabs, useToast } from '../components/ui';
import type { ApTabItem } from '../components/ui';
import { DASH, fmtDate, fmtNum, fmtPct, fmtPctVal } from '../utils/format';

const route = useRoute();
const aid = computed(() => String(route.params.aid ?? ''));

const profile = ref<AnalysisProfile | null>(null);
const report = ref<Record<string, any> | null>(null);
const runUid = ref<string>('');
const news = ref<AnalysisNews | null>(null);
const loading = ref(false);
const error = ref('');
const running = ref(false);
const progress = ref(0);
const refreshing = ref(false);
const syncingSec = ref(false);
const tab = ref('fundamental');
const toast = useToast();

const tabs: ApTabItem[] = [
  { key: 'fundamental', label: '基本面' },
  { key: 'industry', label: '行业趋势' },
  { key: 'technical', label: '短线超卖' },
  { key: 'news', label: '最新消息' },
];

const modules = computed<Record<string, any>>(() => (report.value?.modules ?? {}) as Record<string, any>);
const fundamental = computed(() => modules.value.fundamental ?? {});
const industry = computed(() => modules.value.industry ?? {});
const technical = computed(() => modules.value.technical ?? {});
const newsModule = computed(() => modules.value.news ?? {});

const moduleToggles = [
  { key: 'fundamental', label: '基本面' },
  { key: 'industry', label: '行业' },
  { key: 'technical', label: '技术' },
  { key: 'news', label: '消息' },
];

const enabled = computed<Record<string, boolean>>(() => profile.value?.modules ?? {});

function sleep(ms: number): Promise<void> {
  return new Promise((r) => setTimeout(r, ms));
}

async function pollJob(jobId: string, timeout = 300): Promise<void> {
  for (let i = 0; i < timeout; i += 1) {
    const job = await getJob(jobId);
    progress.value = typeof job.progress === 'number' ? job.progress : 0;
    if (job.status === 'done') return;
    if (job.status === 'failed') throw new Error(job.error || '任务失败');
    await sleep(1000);
  }
  throw new Error('任务超时，请稍后在运行历史查看');
}

async function loadReport(): Promise<void> {
  const runs = await listRuns({ type: 'analysis', limit: 100 });
  const hit = runs.find((r) => (r.summary as Record<string, any> | null)?.aid === aid.value);
  if (!hit) {
    report.value = null;
    runUid.value = '';
    return;
  }
  runUid.value = hit.run_uid;
  try {
    report.value = await fetchRunArtifact<Record<string, any>>(hit.run_uid, 'analysis_report.json', 'json');
  } catch {
    report.value = null;                      // 产物尚不可取（MinIO 延迟/清理）
  }
}

async function loadNews(): Promise<void> {
  try {
    news.value = await getAnalysisNews(aid.value, 10);
  } catch {
    news.value = null;
  }
}

async function load(): Promise<void> {
  if (!aid.value) return;
  loading.value = true;
  error.value = '';
  try {
    profile.value = await getAnalysis(aid.value);
    await Promise.all([loadReport(), loadNews()]);
  } catch (err) {
    error.value = err instanceof Error ? err.message : '档案加载失败';
  } finally {
    loading.value = false;
  }
}

async function runNow(): Promise<void> {
  if (running.value) return;
  running.value = true;
  progress.value = 0;
  try {
    const { job_id } = await runAnalysis(aid.value);
    await pollJob(job_id);
    await Promise.all([loadReport(), loadNews(), getAnalysis(aid.value).then((p) => (profile.value = p))]);
    toast.success('分析完成');
  } catch (err) {
    toast.error(err instanceof Error ? err.message : '分析失败');
  } finally {
    running.value = false;
  }
}

async function refreshNews(): Promise<void> {
  if (refreshing.value) return;
  refreshing.value = true;
  try {
    const { job_id } = await refreshAnalysisNews(aid.value);
    await pollJob(job_id);
    await loadNews();
    toast.success('新闻已刷新');
  } catch (err) {
    toast.error(err instanceof Error ? err.message : '刷新失败');
  } finally {
    refreshing.value = false;
  }
}

async function toggleModule(key: string): Promise<void> {
  if (!profile.value) return;
  const next = { ...enabled.value, [key]: !enabled.value[key] };
  try {
    profile.value = await updateAnalysis(aid.value, { modules: next });
    toast.success('模块设置已更新');
  } catch (err) {
    toast.error(err instanceof Error ? err.message : '更新失败');
  }
}

async function syncNow(): Promise<void> {
  if (syncingSec.value || !profile.value) return;
  syncingSec.value = true;
  try {
    const { job_id } = await syncSecid(profile.value.secid);
    await pollJob(job_id, 120);
    await Promise.all([loadReport(), loadNews()]);
    toast.success(`${profile.value.secid} 行情已更新`);
  } catch (err) {
    if (err instanceof Error && err.message.includes('已有')) {
      toast.info('已有同步任务在运行，完成后数据自动覆盖该标的');
    } else {
      toast.error(err instanceof Error ? err.message : '拉取失败');
    }
  } finally {
    syncingSec.value = false;
  }
}

function fundRows(): Record<string, any>[] {
  return (fundamental.value.reports ?? []) as Record<string, any>[];
}

onMounted(load);
</script>

<template>
  <main class="ap-page">
    <p v-if="loading" class="ap-muted">加载中…</p>

    <ApCard v-else-if="error" title="加载失败">
      <p role="alert">{{ error }}</p>
      <ApButton variant="ghost" @click="load">重试</ApButton>
    </ApCard>

    <template v-else-if="profile">
      <div class="ap-page__head cd-head">
        <div>
          <h1 class="ap-title">{{ profile.name }}</h1>
          <p class="ap-page__desc">
            <span class="ap-mono">{{ profile.aid }} · {{ profile.secid }}</span>
            <span class="ap-muted"> 影子组合 </span>
            <span class="ap-mono">{{ profile.shadow_pid }}</span>
          </p>
        </div>
        <div class="cd-actions">
          <ApButton variant="ghost" :loading="syncingSec" @click="syncNow" data-test="cd-sync-secid">
            拉取行情
          </ApButton>
          <ApButton variant="primary" :loading="running" @click="runNow" data-test="cd-run">
            运行分析
          </ApButton>
          <RouterLink :to="`/portfolios/${profile.shadow_pid}/backtest`">
            <ApButton variant="ghost" data-test="cd-backtest">回测</ApButton>
          </RouterLink>
        </div>
      </div>

      <div v-if="running" class="cd-progress">
        <div class="cd-progress__bar" :style="{ width: `${progress}%` }" />
        <span class="ap-muted">分析中… {{ progress }}%</span>
      </div>

      <ApCard title="模块开关">
        <div class="cd-mods">
          <label v-for="m in moduleToggles" :key="m.key" class="cd-mod">
            <input type="checkbox" :checked="enabled[m.key]" @change="toggleModule(m.key)" />
            <span>{{ m.label }}</span>
          </label>
        </div>
        <p class="ap-muted cd-meta">
          最近运行：{{ report ? fmtDate(report.as_of) : '尚未运行' }}
          <span v-if="runUid" class="ap-mono"> · {{ runUid }}</span>
        </p>
      </ApCard>

      <ApCard v-if="!report" title="分析结果">
        <p class="ap-muted" data-test="cd-no-report">尚无分析结果，点击「运行分析」生成。</p>
      </ApCard>

      <ApTabs v-else v-model="tab" :tabs="tabs">
        <div v-if="tab === 'fundamental'" class="cd-panel" data-test="tab-fundamental">
          <p v-if="!fundamental.available" class="ap-muted">{{ fundamental.note || '暂无财报数据' }}</p>
          <template v-else>
            <div class="cd-kpis">
              <div class="cd-kpi">
                <span class="ap-kicker">ROE</span>
                <strong>{{ fmtPctVal(fundamental.latest?.roe) }}</strong>
              </div>
              <div class="cd-kpi">
                <span class="ap-kicker">营收同比</span>
                <strong>{{ fmtPctVal(fundamental.latest?.rev_growth) }}</strong>
              </div>
              <div class="cd-kpi">
                <span class="ap-kicker">净利同比</span>
                <strong>{{ fmtPctVal(fundamental.latest?.np_growth) }}</strong>
              </div>
              <div class="cd-kpi">
                <span class="ap-kicker">主营利润率</span>
                <strong>{{ fmtPctVal(fundamental.latest?.margin) }}</strong>
              </div>
            </div>
            <table class="cd-table">
              <thead>
                <tr>
                  <th>报告期</th><th>营业总收入</th><th>净利润</th>
                  <th>ROE</th><th>营收同比</th><th>净利同比</th>
                </tr>
              </thead>
              <tbody>
                <tr v-for="r in fundRows()" :key="r.period">
                  <td class="ap-mono">{{ r.period }}</td>
                  <td class="ap-mono">{{ fmtNum(r.revenue, 0) }}</td>
                  <td class="ap-mono">{{ fmtNum(r.net_profit, 0) }}</td>
                  <td class="ap-mono">{{ fmtPctVal(r.roe) }}</td>
                  <td class="ap-mono">{{ fmtPctVal(r.rev_growth) }}</td>
                  <td class="ap-mono">{{ fmtPctVal(r.np_growth) }}</td>
                </tr>
              </tbody>
            </table>
            <p class="ap-muted cd-meta">最近可用报告期 {{ fundamental.latest?.period }}（PIT：按公告日过滤）</p>
          </template>
        </div>

        <div v-else-if="tab === 'industry'" class="cd-panel" data-test="tab-industry">
          <p v-if="!industry.available" class="ap-muted">{{ industry.note || '行业数据不可用' }}</p>
          <template v-else>
            <div class="cd-kpis">
              <div class="cd-kpi">
                <span class="ap-kicker">行业</span>
                <strong>{{ industry.board }}</strong>
              </div>
              <div class="cd-kpi">
                <span class="ap-kicker">行业 20 日</span>
                <strong>{{ fmtPct(industry.ret_20d) }}</strong>
              </div>
              <div class="cd-kpi">
                <span class="ap-kicker">行业 60 日</span>
                <strong>{{ fmtPct(industry.ret_60d) }}</strong>
              </div>
              <div class="cd-kpi">
                <span class="ap-kicker">成交额 5/60 日</span>
                <strong>{{ fmtNum(industry.amount_ratio_5_60, 2) }}</strong>
              </div>
              <div class="cd-kpi">
                <span class="ap-kicker">个股相对行业（20日）</span>
                <strong>{{ fmtPct(industry.relative_strength_20d) }}</strong>
              </div>
              <div class="cd-kpi">
                <span class="ap-kicker">行业指数点位</span>
                <strong>{{ fmtNum(industry.close, 1) }}</strong>
              </div>
            </div>
            <p class="ap-muted cd-meta">
              板块：{{ industry.label || DASH }} · 数据截至 {{ fmtDate(industry.as_of) }}
              · 申万一级指数（成交额单位亿元）
            </p>
          </template>
        </div>

        <div v-else-if="tab === 'technical'" class="cd-panel" data-test="tab-technical">
          <p v-if="!technical.available" class="ap-muted">{{ technical.note || '行情数据不足' }}</p>
          <template v-else>
            <div class="cd-verdict">
              <ApBadge :tone="technical.oversold ? 'danger' : 'success'" data-test="cd-oversold">
                {{ technical.oversold ? '短线超卖' : '未超卖' }}
              </ApBadge>
              <span class="ap-muted">信号强度 {{ technical.score }}/4</span>
            </div>
            <div class="cd-kpis">
              <div class="cd-kpi"><span class="ap-kicker">RSI(14)</span><strong>{{ fmtNum(technical.rsi, 2) }}</strong></div>
              <div class="cd-kpi"><span class="ap-kicker">K</span><strong>{{ fmtNum(technical.k, 2) }}</strong></div>
              <div class="cd-kpi"><span class="ap-kicker">D</span><strong>{{ fmtNum(technical.d, 2) }}</strong></div>
              <div class="cd-kpi"><span class="ap-kicker">J</span><strong>{{ fmtNum(technical.j, 2) }}</strong></div>
              <div class="cd-kpi"><span class="ap-kicker">20 日涨跌</span><strong>{{ fmtPct(technical.rev_20d == null ? null : -technical.rev_20d) }}</strong></div>
              <div class="cd-kpi"><span class="ap-kicker">距 20 日高点</span><strong>{{ fmtPct(technical.dist_high20) }}</strong></div>
            </div>
            <ul class="cd-signals">
              <li v-for="s in technical.signals || []" :key="s">{{ s }}</li>
              <li v-if="!(technical.signals || []).length" class="ap-muted">无触发条件</li>
            </ul>
            <p class="ap-muted cd-meta">行情截至 {{ fmtDate(technical.as_of) }} · 收盘 {{ fmtNum(technical.close, 2) }}</p>
          </template>
        </div>

        <div v-else class="cd-panel" data-test="tab-news">
          <div class="cd-newshead">
            <span class="ap-muted">
              {{ news?.available ? `共 ${news.count ?? news.items.length} 条` : (news?.note || newsModule.note || '暂无新闻') }}
            </span>
            <ApButton variant="ghost" size="sm" :loading="refreshing" @click="refreshNews" data-test="cd-news-refresh">
              刷新
            </ApButton>
          </div>
          <ul class="cd-news">
            <li v-for="(n, i) in (news?.items ?? [])" :key="`${n.dt}-${i}`">
              <a :href="n.url" target="_blank" rel="noopener noreferrer">{{ n.title }}</a>
              <span class="ap-muted ap-mono"> {{ n.dt }} · {{ n.source || DASH }}</span>
            </li>
            <li v-if="!(news?.items ?? []).length" class="ap-muted">暂无新闻数据</li>
          </ul>
        </div>
      </ApTabs>
    </template>
  </main>
</template>

<style scoped>
.cd-head { display: flex; justify-content: space-between; align-items: flex-start; gap: var(--ap-sp-3); }
.cd-actions { display: flex; gap: var(--ap-sp-2); }
.cd-progress { position: relative; margin: var(--ap-sp-2) 0; }
.cd-progress__bar { height: 4px; background: var(--ap-seal); border-radius: 2px; transition: width 0.3s; }
.cd-progress span { font-size: var(--ap-fs-sm); }
.cd-mods { display: flex; gap: var(--ap-sp-3); flex-wrap: wrap; }
.cd-mod { display: flex; align-items: center; gap: var(--ap-sp-1); cursor: pointer; }
.cd-meta { margin-top: var(--ap-sp-2); font-size: var(--ap-fs-sm); }
.cd-panel { padding-top: var(--ap-sp-3); }
.cd-kpis { display: grid; grid-template-columns: repeat(auto-fill, minmax(160px, 1fr)); gap: var(--ap-sp-2); }
.cd-kpi { background: var(--ap-card2); border-radius: var(--ap-radius); padding: var(--ap-sp-2) var(--ap-sp-3); }
.cd-kpi strong { display: block; margin-top: 2px; font-size: var(--ap-fs-lg); font-family: var(--ap-font-mono); }
.cd-table { width: 100%; border-collapse: collapse; margin-top: var(--ap-sp-3); font-size: var(--ap-fs-sm); }
.cd-table th, .cd-table td { padding: 6px 8px; border-bottom: 1px solid var(--ap-line); text-align: right; }
.cd-table th:first-child, .cd-table td:first-child { text-align: left; }
.cd-verdict { display: flex; align-items: center; gap: var(--ap-sp-2); margin-bottom: var(--ap-sp-2); }
.cd-signals { margin-top: var(--ap-sp-2); padding-left: 18px; font-size: var(--ap-fs-sm); }
.cd-newshead { display: flex; justify-content: space-between; align-items: center; }
.cd-news { list-style: none; padding: 0; margin: var(--ap-sp-2) 0 0; }
.cd-news li { padding: 8px 0; border-bottom: 1px solid var(--ap-line); font-size: var(--ap-fs-sm); }
.cd-news a { color: var(--ap-seal); }
</style>
