import { defineConfig, presetUno } from 'unocss';

// UnoCSS 主题：把 design tokens（src/styles/tokens.css）映射为原子类。
// 例：text-stamp / bg-paper / text-dim / border-line / rounded-card。
export default defineConfig({
  presets: [presetUno()],
  theme: {
    colors: {
      paper: 'var(--ap-paper)',
      card: 'var(--ap-card)',
      card2: 'var(--ap-card2)',
      desk: 'var(--ap-desk)',
      ink: 'var(--ap-ink)',
      dim: 'var(--ap-dim)',
      mute: 'var(--ap-mute)',
      line: 'var(--ap-line)',
      hair: 'var(--ap-hair)',
      // 品牌印章红：text-stamp / bg-stamp / border-stamp
      stamp: 'var(--ap-seal)',
      seal: 'var(--ap-seal)',
      success: 'var(--ap-success)',
      warn: 'var(--ap-warn)',
      danger: 'var(--ap-danger)',
      info: 'var(--ap-info)',
      up: 'var(--ap-up)',
      down: 'var(--ap-down)',
    },
    fontSize: {
      xs: ['10px', '1.5'],
      sm: ['12px', '1.55'],
      base: ['13px', '1.6'],
      md: ['15px', '1.5'],
      lg: ['20px', '1.3'],
      xl: ['26px', '1.25'],
      '2xl': ['34px', '1.2'],
      hero: ['44px', '1.08'],
    },
    borderRadius: {
      xs: '3px',
      sm: '6px',
      DEFAULT: '8px',
      card: '11px',
      lg: '16px',
      pill: '99px',
    },
  },
});
