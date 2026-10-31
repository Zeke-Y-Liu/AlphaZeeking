<script setup lang="ts">
// 登录页（T6.2）：用户名/密码表单 + auth store。视觉沿用「纸质档案 + 印章红」。
import { computed, ref } from 'vue';
import { useRoute, useRouter } from 'vue-router';

import { ApButton, ApCard } from '../components/ui';
import { currentApiBase, RUNTIME_API_BASE_KEY } from '../api/client';
import { useAuthStore } from '../stores/auth';

const auth = useAuthStore();
const route = useRoute();
const router = useRouter();

const username = ref('');
const password = ref('');
const errorMsg = ref(''); // 401 或其它错误
const lockedMsg = ref(''); // 429 账号锁定

/* ── 连接设置（静态托管指向后端隧道用） ─────────────── */
const showSettings = ref(false);
const apiBaseDraft = ref('');

function toggleSettings(): void {
  showSettings.value = !showSettings.value;
  if (showSettings.value) apiBaseDraft.value = currentApiBase();
}

/** 保存 API base 到 localStorage 并整页刷新（axios baseURL 仅模块初始化时解析）。 */
function saveApiBase(): void {
  const v = apiBaseDraft.value.trim().replace(/\/+$/, '');
  if (v) localStorage.setItem(RUNTIME_API_BASE_KEY, v);
  else localStorage.removeItem(RUNTIME_API_BASE_KEY);
  window.location.reload();
}

/** 清除覆盖，恢复构建期默认（VITE_API_BASE / '/api'）。 */
function resetApiBase(): void {
  localStorage.removeItem(RUNTIME_API_BASE_KEY);
  window.location.reload();
}

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
    } else if (apiErr?.status === 405) {
      // 静态托管（如 GitHub Pages）只允许 GET/HEAD，POST 被边缘 CDN 以 405 拒绝：
      // 说明当前站点未连接后端 API（ap.api-base / VITE_API_BASE）。
      errorMsg.value =
        '登录服务不可用（405）：当前站点为静态托管，未连接后端 API。请在下方「连接设置」中填写可达的后端地址再登录';
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

        <div class="login__settings">
          <button class="login__settings-toggle" type="button" data-test="settings-toggle" @click="toggleSettings">
            连接设置
          </button>
          <div v-if="showSettings" class="login__settings-body">
            <label class="login__field">
              <span class="login__label">后端地址（API Base）</span>
              <input
                v-model="apiBaseDraft"
                class="login__input"
                type="text"
                placeholder="https://example.lhr.life/api"
                data-test="api-base-input"
              />
            </label>
            <div class="login__settings-actions">
              <ApButton variant="ghost" data-test="api-base-save" @click="saveApiBase">保存并刷新</ApButton>
              <ApButton variant="ghost" data-test="api-base-reset" @click="resetApiBase">恢复默认</ApButton>
            </div>
            <p class="login__settings-hint">
              静态托管（如 GitHub Pages）默认指向构建时地址；后端隧道地址变化时可在此覆盖，保存后立即生效。
            </p>
          </div>
        </div>
      </ApCard>
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

/* ── 连接设置 ─────────────────────────────── */
.login__settings {
  margin-top: var(--ap-sp-5);
  border-top: 1px solid var(--ap-hair);
  padding-top: var(--ap-sp-3);
}
.login__settings-toggle {
  display: block;
  margin: 0 auto;
  background: none;
  border: none;
  cursor: pointer;
  font-family: var(--ap-font-mono);
  font-size: var(--ap-fs-xs);
  letter-spacing: 0.18em;
  color: var(--ap-mute);
  transition: color 0.2s;
}
.login__settings-toggle:hover {
  color: var(--ap-dim);
}
.login__settings-body {
  margin-top: var(--ap-sp-3);
  display: flex;
  flex-direction: column;
  gap: var(--ap-sp-3);
}
.login__settings-actions {
  display: flex;
  justify-content: flex-end;
  gap: var(--ap-sp-2);
}
.login__settings-hint {
  font-size: var(--ap-fs-xs);
  line-height: 1.5;
  color: var(--ap-mute);
}

</style>
