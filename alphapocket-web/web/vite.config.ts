/// <reference types="vitest" />
import vue from '@vitejs/plugin-vue';
import UnoCSS from 'unocss/vite';
import { defineConfig } from 'vite';

export default defineConfig({
  plugins: [vue(), UnoCSS()],
  server: {
    host: true,
    port: 5173,
    // 允许经平台预览网关（run-agent-*.agent-sandbox-*）以外部 Host 访问；
    // 否则 vite 403 Blocked request 会导致网关健康检查失败（no healthy upstream）
    allowedHosts: true,
    // dev: 前端 /api → bff（§3.1）；生产由 nginx 反代
    proxy: {
      '/api': {
        target: 'http://localhost:8081',
        changeOrigin: true,
      },
    },
  },
  test: {
    environment: 'jsdom',
    globals: true,
    include: ['src/**/*.{spec,test}.ts'],
  },
});
