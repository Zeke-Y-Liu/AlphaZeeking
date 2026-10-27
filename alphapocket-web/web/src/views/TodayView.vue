<script setup lang="ts">
// 今日结论（2026-10-26）：快速获取组合操作结论 —— 顶部实时时钟 + 收盘自动评估
// 状态横幅 + 各组合 highlight 卡片（服务端冻结中文短句，前端零计算）。
// 数据源 GET /v1/today（只读聚合：评估已由收盘后调度器跑完落 runs）；
// 60s 自动刷新 + 手动刷新；server_time 用于服务器时钟展示与校准。
import { computed, onMounted, onUnmounted, ref } from 'vue';

import { getToday } from '../api';
import type { TodayPortfolioItem } from '../api/types';
import { ApBadge, ApCard } from '../components/ui';
import type { BadgeTone } from '../components/ui/types';

const payload = ref<null | Awaited<ReturnType<typeof getToday>>>(null);
const loading = ref(false);
const error = ref('');

/** 本地实时时钟（每秒跳动；页面核心信息之一：当前时间）。 */
const clock = ref('');
let clockTimer: ReturnType<typeof setInterval> | undefined;
/** 60s 自动刷新（收盘后评估陆续落库，页面随之收敛）。 */
const REFRESH_MS = 60_000;
let refreshTimer: ReturnType<typeof setInterval> | undefined;

const LEVEL_META: Record<TodayPortfolioItem['level'], { label: string; tone: BadgeTone }> = {
  good: { label: '偏多', tone: 'success' },
  bad: { label: '偏空', tone: 'danger' },
  warn: { label: '数据风险', tone: 'warn' },
  flat: { label: '中性', tone: 'info' },
  stale: { label: '暂无评估', tone: 'default' },
};

const SYNC_LABEL: Record<string, string> = {
  queued: '排队中',
  running: '同步中',
  done: '已完成',
  failed: '失败',
};

const levelMeta = (lv: TodayPortfolioItem['level']) => LEVEL_META[lv] ?? LEVEL_META.stale;

const generatedAt = computed(() =>
  payload.value ? (payload.value.generated_at || '').replace('T', ' ').slice(0, 19) : '',
);

/** 自动评估横幅文案（enabled=false 提示开启方式；否则给触发时点 + 今日进度）。 */
const autoText = computed(() => {
  const a = payload.value?.auto_review;
  if (!a || !a.enabled) return '收盘自动评估未开启（服务端 AP_AUTO_REVIEW=1 启用后，交易日收盘自动跑评估）';
  const sync = a.sync ? `数据同步${SYNC_LABEL[a.sync] ?? a.sync}` : '今日尚未同步';
  const done = a.review_done ?? 0;
  const failed = a.review_failed ?? 0;
  const total = a.review_submitted ?? 0;
  const evalPart = failed > 0 ? `评估 ${done}/${total}（失败 ${failed}）` : `评估 ${done}/${total}`;
  return `收盘自动评估：交易日 ${a.fire_time ?? '—'} 触发 · ${sync} · ${evalPart}`;
});

/** 收益展示（day_ret/ret_20d 小数 → 百分比；与后端 fmt_pct 同口径）。 */
function pct(v?: number | null): string {
  if (v === null || v === undefined || Number.isNaN(Number(v))) return '—';
  return `${(Number(v) * 100).toFixed(2)}%`;
}

async function load() {
  loading.value = true;
  error.value = '';
  try {
    payload.value = await getToday();
  } catch (e) {
    error.value = e instanceof Error ? e.message : String(e);
  } finally {
    loading.value = false;
  }
}

function tickClock() {
  const now = new Date();
  clock.value = now.toLocaleTimeString('zh-CN', { hour12: false });
}

onMounted(() => {
  tickClock();
  clockTimer = setInterval(tickClock, 1000);
  void load();
  refreshTimer = setInterval(() => void load(), REFRESH_MS);
});

onUnmounted(() => {
  if (clockTimer) clearInterval(clockTimer);
  if (refreshTimer) clearInterval(refreshTimer);
});
</script>

<template>
  <main class="ap-page">
    <div class="ap-page__head ap-row">
      <div>
        <h1 class="ap-title">今日结论</h1>
        <p class="ap-page__desc">一屏获取各组合操作建议（收盘后自动评估，此处只读聚合）。</p>
      </div>
      <div class="ap-row today-head__actions">
        <span class="today-clock ap-mono">{{ clock }}</span>
        <button class="ck-link" type="button" :disabled="loading" @click="load()">
          {{ loading ? '刷新中…' : '刷新' }}
        </button>
      </div>
    </div>

    <!-- 当前日期 + 服务器时间 + 最后更新 -->
    <p class="ap-muted today-meta">
      {{ payload?.date ?? '—' }}
      <template v-if="payload">
        · 服务器时间 {{ (payload.server_time || '').replace('T', ' ').slice(0, 19) }}
        · 更新于 {{ generatedAt || '—' }}
      </template>
    </p>

    <ApCard class="today-banner">
      <span class="today-banner__dot" aria-hidden="true"></span>
      <span>{{ autoText }}</span>
    </ApCard>

    <p v-if="error" class="ap-muted">加载失败：{{ error }}</p>
    <p v-else-if="loading && !payload" class="ap-muted">加载中…</p>
    <p v-else-if="!payload?.portfolios.length" class="ap-muted">暂无组合：先到「组合列表」创建并持仓。</p>

    <div v-else class="today-grid">
      <ApCard v-for="p in payload.portfolios" :key="p.pid" class="today-card" :class="`today-card--${p.level}`">
        <div class="ap-row today-card__head">
          <div class="ap-row today-card__title">
            <RouterLink class="ck-link today-card__name" :to="`/portfolios/${p.pid}`">{{ p.name }}</RouterLink>
            <span class="ap-mono ap-muted">{{ p.pid }}</span>
          </div>
          <ApBadge :tone="levelMeta(p.level).tone">{{ levelMeta(p.level).label }}</ApBadge>
        </div>
        <p class="ap-muted today-card__asof">
          <template v-if="p.fresh">今日结论（as_of {{ p.as_of }}）</template>
          <template v-else-if="p.as_of">截至 {{ p.as_of }}（收盘自动评估未跑当日，可手动触发评估）</template>
          <template v-else>尚无评估记录</template>
        </p>
        <ul class="today-highlights">
          <li v-for="(h, i) in p.highlights" :key="i">
            <span class="today-highlights__dot" :class="`today-highlights__dot--${p.level}`" aria-hidden="true"></span>
            <span>{{ h }}</span>
          </li>
        </ul>
        <p v-if="p.metrics" class="ap-muted today-card__metrics ap-mono">
          今日 {{ pct(p.metrics.day_ret) }} · 近20日 {{ pct(p.metrics.ret_20d) }} · 持仓 {{ p.position_count ?? '—' }}
        </p>
      </ApCard>
    </div>
  </main>
</template>

<style scoped>
.today-head__actions { gap: 14px; align-items: baseline; }
.today-clock { font-size: 22px; font-weight: 600; letter-spacing: 0.5px; }
.today-meta { margin: 4px 0 12px; font-size: 13px; }
.today-banner { display: flex; align-items: center; gap: 10px; margin-bottom: 16px; font-size: 13px; }
.today-banner__dot {
  width: 8px; height: 8px; border-radius: 50%;
  background: var(--ap-primary, #2563eb); flex: none;
}
.today-grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(340px, 1fr)); gap: 14px; }
.today-card { border-top: 3px solid var(--ap-border, #d0d7de); }
.today-card--good { border-top-color: #16a34a; }
.today-card--bad { border-top-color: #dc2626; }
.today-card--warn { border-top-color: #d97706; }
.today-card--flat { border-top-color: #2563eb; }
.today-card--stale { border-top-color: #9ca3af; }
.today-card__head { justify-content: space-between; align-items: center; }
.today-card__title { gap: 8px; align-items: baseline; }
.today-card__name { font-size: 15px; font-weight: 600; }
.today-card__asof { font-size: 12px; margin: 2px 0 10px; }
.today-highlights { list-style: none; margin: 0 0 10px; padding: 0; display: flex; flex-direction: column; gap: 8px; }
.today-highlights li { display: flex; gap: 8px; align-items: baseline; font-size: 13px; line-height: 1.55; }
.today-highlights__dot { width: 6px; height: 6px; border-radius: 50%; flex: none; transform: translateY(-2px); background: #9ca3af; }
.today-highlights__dot--good { background: #16a34a; }
.today-highlights__dot--bad { background: #dc2626; }
.today-highlights__dot--warn { background: #d97706; }
.today-highlights__dot--flat { background: #2563eb; }
.today-card__metrics { font-size: 12px; margin: 0; }
.ck-link { background: none; border: none; color: var(--ap-primary, #2563eb); cursor: pointer; padding: 0; font-size: 13px; text-decoration: none; }
.ck-link:disabled { opacity: 0.5; cursor: default; }
</style>
