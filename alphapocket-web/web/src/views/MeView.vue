<script setup lang="ts">
// 「我的」占位（H5 tabbar 第 4 项；账户/偏好后续任务实现）。
import { ApCard } from '../components/ui';
</script>

<template>
  <main class="ap-page">
    <div class="ap-page__head">
      <h1 class="ap-title">我的</h1>
      <p class="ap-page__desc">账户与偏好（占位，后续任务实现）。</p>
    </div>
    <ApCard>
      <div class="ap-todo">待实现：账户信息、登出、2FA 扩展位、偏好设置。</div>
    </ApCard>
  </main>
</template>
