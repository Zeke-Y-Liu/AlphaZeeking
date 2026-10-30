<script setup lang="ts">
// 登录页（T6.2）：用户名/密码表单 + auth store。视觉沿用「纸质档案 + 印章红」。
import { computed, ref } from 'vue';
import { useRoute, useRouter } from 'vue-router';

import { ApButton, ApCard } from '../components/ui';
import { useAuthStore } from '../stores/auth';

const auth = useAuthStore();
const route = useRoute();
const router = useRouter();

const username = ref('');
const password = ref('');
const errorMsg = ref(''); // 401 或其它错误
const lockedMsg = ref(''); // 429 账号锁定

const submitting = computed(() => auth.loading);

/** 只接受站内绝对路径，防开放重定向。 */
function resolveRedirect(): string {
  const redirect = route.query.redirect;
  if (typeof redirect === 'string' && redirect.startsWith('/') && !redirect.startsWith('//')) {
    return redirect;
  }
  return '/portfolios';
}

async function onSubmit(): Promise<void> {
  errorMsg.value = '';
  lockedMsg.value = '';
  if (!username.value || !password.value) {
    errorMsg.value = '请输入用户名和密码';
    return;
  }
  try {
    await auth.login(username.value, password.value);
    await router.push(resolveRedirect());
  } catch {
    const apiErr = auth.error;
    if (apiErr?.status === 429 || apiErr?.code === 'ACCOUNT_LOCKED') {
      lockedMsg.value = apiErr.message || '登录失败次数过多，账号已被临时锁定';
    } else if (apiErr?.status === 401) {
      errorMsg.value = '用户名或密码错误';
    } else {
      errorMsg.value = apiErr?.message || '登录失败，请稍后重试';
    }
    password.value = '';
  }
}
</script>

<template>
  <div class="login">
    <div class="login__wrap">
      <div class="ap-kicker login__kicker">AlphaPocket · V4-β</div>

      <ApCard class="login__card">
        <div class="login__head">
          <h1 class="ap-title login__title">登录</h1>
          <p class="ap-sub login__sub">组合研究 · 归档管理</p>
        </div>

        <form class="login__form ap-stack" @submit.prevent="onSubmit">
          <label class="login__field">
            <span class="login__label">用户名</span>
            <input
              v-model="username"
              class="login__input"
              type="text"
              autocomplete="username"
              :disabled="submitting"
              data-test="username"
            />
          </label>

          <label class="login__field">
            <span class="login__label">密码</span>
            <input
              v-model="password"
              class="login__input"
              type="password"
              autocomplete="current-password"
              :disabled="submitting"
              data-test="password"
            />
          </label>

          <p v-if="errorMsg" class="login__msg login__msg--error" role="alert" data-test="error">
            {{ errorMsg }}
          </p>
          <p v-if="lockedMsg" class="login__msg login__msg--locked" role="alert" data-test="locked">
            {{ lockedMsg }}
          </p>

          <ApButton
            class="login__submit"
            type="submit"
            size="lg"
            :loading="submitting"
            :disabled="submitting"
            data-test="submit"
          >
            登录
          </ApButton>
        </form>
      </ApCard>

      <p
        class="login__note ap-muted"
        title="BFF 未提供 httpOnly Cookie，token 暂存 localStorage；同源 XSS 可读取，后续将切换 Cookie 方案。"
      >
        凭证暂存于浏览器 localStorage（无 httpOnly Cookie 时的降级方案，存在 XSS 读取风险）。
      </p>
    </div>
  </div>
</template>

<style scoped>
.login {
  min-height: 100vh;
  display: flex;
  align-items: center;
  justify-content: center;
  padding: var(--ap-sp-6) var(--ap-sp-4) calc(var(--ap-disclaimer-h) + var(--ap-sp-6));
  background:
    radial-gradient(1200px 560px at 50% -12%, var(--ap-seal-soft), transparent 62%),
    var(--ap-paper);
}
.login__wrap {
  width: 100%;
  max-width: 380px;
}
.login__kicker {
  display: block;
  text-align: center;
  margin-bottom: var(--ap-sp-3);
}
.login__card {
  border-top: 3px solid var(--ap-seal);
  box-shadow: var(--ap-shadow);
}
.login__head {
  text-align: center;
  margin-bottom: var(--ap-sp-5);
}
.login__title {
  font-size: var(--ap-fs-2xl);
}
.login__sub {
  margin-top: var(--ap-sp-1);
  font-size: var(--ap-fs-sm);
}
.login__field {
  display: flex;
  flex-direction: column;
  gap: var(--ap-sp-1);
}
.login__label {
  font-family: var(--ap-font-mono);
  font-size: var(--ap-fs-xs);
  letter-spacing: 0.18em;
  text-transform: uppercase;
  color: var(--ap-mute);
}
.login__input {
  height: 38px;
  padding: 0 var(--ap-sp-3);
  background: var(--ap-card2);
  border: 1px solid var(--ap-line);
  border-radius: var(--ap-radius);
  color: var(--ap-ink);
  font-family: var(--ap-font-sans);
  font-size: var(--ap-fs-base);
  transition: 0.2s;
}
.login__input:focus {
  outline: none;
  border-color: var(--ap-seal);
  background: var(--ap-card);
  box-shadow: 0 0 0 3px var(--ap-seal-soft);
}
.login__input:disabled {
  opacity: 0.6;
}
.login__msg {
  font-size: var(--ap-fs-sm);
  border-radius: var(--ap-radius-sm);
  padding: 7px 10px;
}
.login__msg--error {
  color: var(--ap-danger);
  background: var(--ap-seal-soft);
  border: 1px solid var(--ap-seal-line);
}
.login__msg--locked {
  color: var(--ap-warn);
  background: rgba(176, 140, 60, 0.1);
  border: 1px solid rgba(176, 140, 60, 0.35);
}
.login__submit {
  width: 100%;
  margin-top: var(--ap-sp-2);
}
.login__demo {
  margin-top: var(--ap-sp-4);
  text-align: center;
  font-size: var(--ap-fs-sm);
}
.login__note {
  margin-top: var(--ap-sp-3);
  text-align: center;
  font-size: 11px;
  line-height: 1.5;
}
</style>
