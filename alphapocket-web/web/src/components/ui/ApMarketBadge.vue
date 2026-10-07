<script setup lang="ts">
// ApMarketBadge（V4-β）：市场徽标（A股/港股/美股）+ 可选港股通标记。
// A股=default 灰、港股=warn 金、美股=info 蓝；港股通以「通」角标叠加在市场徽标右侧。
import { computed } from 'vue';
import ApBadge from './ApBadge.vue';
import type { BadgeTone } from './types';

const props = defineProps<{
  market: string | null | undefined;
  /** 是否显示港股通标记（默认自动：market=HK 且 hk_connect 非 null 时显示）。 */
  hkConnect?: boolean | null;
  /** 只显示港股通标记（不含市场徽标）。 */
  connectOnly?: boolean;
}>();

const MARKET_META: Record<string, { label: string; tone: BadgeTone }> = {
  A: { label: 'A股', tone: 'default' },
  HK: { label: '港股', tone: 'warn' },
  US: { label: '美股', tone: 'info' },
};

const meta = computed(() => MARKET_META[props.market ?? ''] ?? { label: props.market || '—', tone: 'default' as BadgeTone });
const showConnect = computed(() => props.market === 'HK' && props.hkConnect === true);
</script>

<template>
  <span class="ap-market-badge">
    <ApBadge v-if="!connectOnly" :tone="meta.tone">{{ meta.label }}</ApBadge>
    <ApBadge v-if="showConnect" tone="success" class="ap-market-badge__connect">通</ApBadge>
  </span>
</template>

<style scoped>
.ap-market-badge {
  display: inline-flex;
  align-items: center;
  gap: 4px;
}
.ap-market-badge__connect {
  padding: 2px 5px;
}
</style>
