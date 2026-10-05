/// <reference types="vitest" />
import vue from '@vitejs/plugin-vue';
import UnoCSS from 'unocss/vite';
import { defineConfig } from 'vite';

export default defineConfig({
  plugins: [vue(), UnoCSS()],
  server: {
    host: true,
    port: 5173,
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
